"""Upload use cases; immutable source publication precedes database registration."""

from hashlib import sha256
from http import HTTPStatus
from io import BytesIO
from typing import Annotated, BinaryIO, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from article_graph.blob_store import BlobStore
from article_graph.document_format import DocumentFormat
from article_graph.domain import DTO, ContentHash, ErrorCode, ProcessingResponse

Opaque = Annotated[str, StringConstraints(min_length=1)]


class RegistrationError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code
        self.status_code = (
            HTTPStatus.NOT_FOUND if code == ErrorCode.NOT_FOUND else HTTPStatus.CONFLICT
        )


class UploadRequest(DTO):
    app_id: Opaque
    article_id: UUID | None = None
    idempotency_key: Opaque | None = None
    method: Literal["text", "file"]
    content_type: Literal["text/plain", "text/markdown", "application/pdf"]
    content_hash: ContentHash
    size: int = Field(gt=0)
    format: DocumentFormat

    @model_validator(mode="after")
    def valid_metadata(self) -> Self:
        if not self.app_id.strip() or (
            self.idempotency_key is not None and not self.idempotency_key.strip()
        ):
            raise ValueError("application and idempotency key must not be blank")
        if self.method == "text" and self.content_type != "text/markdown":
            raise ValueError("text input must be Markdown")
        expected = (
            DocumentFormat.PDF
            if self.content_type == "application/pdf"
            else DocumentFormat.MARKDOWN
        )
        if self.format != expected:
            raise ValueError("source format does not match content type")
        return self

    def fingerprint(self) -> str:
        # Size, storage locator and client filename are not request identity.
        canonical = self.model_dump_json(
            include={"article_id", "method", "content_type", "content_hash"}
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


class RegistrationResult(DTO):
    status_code: HTTPStatus
    body: ProcessingResponse


class RegistrationRepository(Protocol):
    def register(self, request: UploadRequest) -> RegistrationResult: ...

    def retry(self, article_id: UUID) -> RegistrationResult: ...


class RegistrationService:
    def __init__(self, blobs: BlobStore, repository: RegistrationRepository) -> None:
        self.blobs = blobs
        self.repository = repository

    def register_text(
        self,
        text: str,
        *,
        app_id: str,
        article_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> RegistrationResult:
        return self._register(
            BytesIO(text.encode("utf-8")),
            app_id=app_id,
            article_id=article_id,
            idempotency_key=idempotency_key,
            method="text",
            content_type="text/markdown",
        )

    def register_file(
        self,
        stream: BinaryIO,
        *,
        app_id: str,
        content_type: str,
        article_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> RegistrationResult:
        return self._register(
            stream,
            app_id=app_id,
            article_id=article_id,
            idempotency_key=idempotency_key,
            method="file",
            content_type=content_type,
        )

    def _register(
        self,
        stream: BinaryIO,
        *,
        app_id: str,
        article_id: UUID | None,
        idempotency_key: str | None,
        method: Literal["text", "file"],
        content_type: str,
    ) -> RegistrationResult:
        blob = self.blobs.put_source(stream, content_type=content_type)
        request = UploadRequest.model_validate(
            {
                "app_id": app_id,
                "article_id": article_id,
                "idempotency_key": idempotency_key,
                "method": method,
                "content_type": content_type.partition(";")[0].strip().lower(),
                "content_hash": blob.content_hash,
                "size": blob.size,
                "format": blob.format,
            }
        )
        return self.repository.register(request)

    def retry(self, article_id: UUID) -> RegistrationResult:
        return self.repository.retry(article_id)
