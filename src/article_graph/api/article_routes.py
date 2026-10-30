"""Authenticated ingestion and polling; all processing belongs to the worker."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, override
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi import status as http_status
from pydantic import StringConstraints
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
lazy from starlette.concurrency import run_in_threadpool

from article_graph.api.auth_routes import Access, Configuration
from article_graph.db.article_repository import ArticleRepository
from article_graph.documents.blob_store import LocalBlobStore
from article_graph.documents.document_format import DocumentError
from article_graph.domain import Article, ErrorCode, ProcessingResponse, SafeError
from article_graph.processing.registration import RegistrationService

router = APIRouter(prefix="/articles", tags=["articles"])
MULTIPART_OVERHEAD_BYTES = 64 * 1024
IdempotencyKey = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class UploadParser(MultiPartParser):
    """Reject incomplete multipart and lossy decoding of Markdown text fields."""

    complete: bool = False

    @override
    def on_part_end(self) -> None:
        if self._current_part.file is None:
            try:
                self._current_part.data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise MultiPartException("Invalid UTF-8 field") from None
            if self._charset != "utf-8":
                raise MultiPartException("Expected UTF-8 fields")
        super().on_part_end()

    @override
    def on_end(self) -> None:
        self.complete = True
        super().on_end()


def repository(request: Request, settings: Configuration) -> ArticleRepository:
    if request.app.state.driver is None:
        raise DocumentError(ErrorCode.SERVICE_UNAVAILABLE)
    return ArticleRepository(request.app.state.driver, settings.neo4j)


def service(request: Request, settings: Configuration) -> RegistrationService:
    repo = repository(request, settings)
    return RegistrationService(
        LocalBlobStore(
            settings.blob_root,
            max_bytes=settings.limits.upload_bytes,
            limits=settings.limits,
        ),
        repo,
    )


@asynccontextmanager
async def upload_form(request: Request, max_bytes: int) -> AsyncIterator[FormData]:
    if request.headers.get("content-type", "").partition(";")[0].lower() != (
        "multipart/form-data"
    ):
        raise DocumentError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)
    # Bound the entire wire body, including headers/unknown parts, before parsing.
    # Do not trust Content-Length; this also handles chunked requests.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes + MULTIPART_OVERHEAD_BYTES:
            raise DocumentError(ErrorCode.PAYLOAD_TOO_LARGE)
        body.extend(chunk)

    async def stream() -> AsyncGenerator[bytes]:
        yield bytes(body)

    parser = UploadParser(
        request.headers,
        stream(),
        max_files=1,
        max_fields=2,
        max_part_size=max_bytes + MULTIPART_OVERHEAD_BYTES,
    )
    try:
        form = await parser.parse()
    except MultiPartException:
        raise DocumentError(ErrorCode.VALIDATION_ERROR) from None
    try:
        keys = [key for key, _ in form.multi_items()]
        if (
            not parser.complete
            or len(keys) != len(set(keys))
            or set(keys) - {"text", "file", "article_id"}
            or (("text" in form) == ("file" in form))
        ):
            raise DocumentError(ErrorCode.VALIDATION_ERROR)
        yield form
    finally:
        await form.close()


@router.post(
    "/upload",
    response_model=ProcessingResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
    responses={
        http_status.HTTP_200_OK: {
            "model": ProcessingResponse,
            "description": "Unchanged input",
        }
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "file": {"type": "string", "format": "binary"},
                            "article_id": {"type": "string", "format": "uuid"},
                        },
                        "oneOf": [{"required": ["text"]}, {"required": ["file"]}],
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
async def upload(
    request: Request,
    response: Response,
    access: Access,
    settings: Configuration,
    idempotency_key: Annotated[IdempotencyKey | None, Header()] = None,
) -> ProcessingResponse:
    async with upload_form(request, settings.limits.upload_bytes) as form:
        article = form.get("article_id")
        try:
            if article is not None and not isinstance(article, str):
                raise ValueError
            article_id = UUID(article) if article is not None else None
        except ValueError:
            raise DocumentError(ErrorCode.VALIDATION_ERROR) from None
        text, file = form.get("text"), form.get("file")
        if "text" in form:
            if not isinstance(text, str) or not text.strip("\ufeff \t\r\n"):
                raise DocumentError(ErrorCode.VALIDATION_ERROR)
        elif not isinstance(file, UploadFile):
            raise DocumentError(ErrorCode.VALIDATION_ERROR)

        def register() -> ProcessingResponse:
            registration = service(request, settings)
            if isinstance(text, str):
                result = registration.register_text(
                    text,
                    app_id=access.sub,
                    article_id=article_id,
                    idempotency_key=idempotency_key,
                )
            elif isinstance(file, UploadFile):
                result = registration.register_file(
                    file.file,
                    content_type=file.content_type or "",
                    app_id=access.sub,
                    article_id=article_id,
                    idempotency_key=idempotency_key,
                )
            else:
                raise DocumentError(ErrorCode.VALIDATION_ERROR)
            response.status_code = result.status_code
            response.headers["Cache-Control"] = "no-store"
            return result.body

        return await run_in_threadpool(register)


@router.get("/{article_id}", response_model=Article)
def article_status(
    article_id: UUID,
    request: Request,
    response: Response,
    access: Access,
    settings: Configuration,
) -> Article:
    article = repository(request, settings).get(article_id)
    response.headers["Cache-Control"] = "no-store"
    if article.error is not None:
        article = article.model_copy(
            update={
                "error": SafeError(code=article.error.code, message="Processing failed")
            }
        )
    return article


@router.post(
    "/{article_id}/retry",
    response_model=ProcessingResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def retry(
    article_id: UUID,
    request: Request,
    response: Response,
    access: Access,
    settings: Configuration,
) -> ProcessingResponse:
    result = repository(request, settings).retry(article_id)
    response.status_code = result.status_code
    response.headers["Cache-Control"] = "no-store"
    return result.body
