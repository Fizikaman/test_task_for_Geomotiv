"""Bounded PDF subprocess orchestration; no PDF library runs in the caller."""

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from article_graph.documents.blob_store import Blob, BlobStore
from article_graph.documents.document_format import DocumentError
from article_graph.documents.preparation import PreparedDocument, SourceText
from article_graph.domain import ErrorCode
from article_graph.settings import Limits


class PageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    width_points: float = Field(gt=0, allow_inf_nan=False)
    height_points: float = Field(gt=0, allow_inf_nan=False)
    dpi: int = Field(gt=0)
    media_type: str = "image/png"
    renderer: str = "pypdfium2-5.14.0"


class PdfManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pages: tuple[PageMetadata, ...]
    prepared: PreparedDocument = Field(repr=False)


@dataclass(frozen=True)
class PdfPage:
    metadata: PageMetadata
    image: Blob
    source: SourceText

    @property
    def page(self) -> int:
        return self.metadata.page

    @property
    def text(self) -> str:
        return self.source.text

    @property
    def artifact_ref(self) -> str:
        return f"{self.image.namespace.value}/{self.image.content_hash}"


@dataclass(frozen=True)
class PreparedPdf:
    prepared: PreparedDocument
    pages: tuple[PdfPage, ...]


def _publish(directory: Path, store: BlobStore) -> PreparedPdf:
    manifest = PdfManifest.model_validate_json(
        (directory / "manifest.json").read_bytes()
    )
    pages: list[PdfPage] = []
    for page, source in zip(manifest.pages, manifest.prepared.sources, strict=True):
        with (directory / f"{page.page}.png").open("rb") as stream:
            pages.append(PdfPage(page, store.put_artifact(stream), source))
    return PreparedPdf(manifest.prepared, tuple(pages))


def _error_code(directory: Path) -> ErrorCode:
    error_file = directory / "error"
    return (
        ErrorCode(error_file.read_text(encoding="ascii"))
        if error_file.exists()
        else ErrorCode.LIMIT_EXCEEDED
    )


async def prepare_pdf(
    path: Path, *, revision_id: UUID, limits: Limits, store: BlobStore
) -> PreparedPdf:
    """All pages or an error. Timeout/cancellation kills and reaps the child.

    Paths must refer to immutable application-owned sources. The subprocess
    does not read provider configuration; stdout/stderr are discarded.
    Disk work and artifact publication run off the event loop.
    """
    temporary = await asyncio.to_thread(TemporaryDirectory, prefix="article-pdf-")
    try:
        directory = Path(temporary.name)
        source = await asyncio.to_thread(path.resolve)
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "article_graph.documents.pdf_worker",
                str(source),
                str(directory),
                str(revision_id),
                limits.model_dump_json(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        try:
            async with asyncio.timeout(limits.pdf_timeout_seconds):
                result = await process.wait()
        except TimeoutError:
            raise DocumentError(ErrorCode.PROCESSING_TIMEOUT) from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if result:
            # The child writes only a safe code, never a parser exception.
            code = await asyncio.to_thread(_error_code, directory)
            raise DocumentError(code)
        publication = asyncio.create_task(asyncio.to_thread(_publish, directory, store))
        try:
            return await asyncio.shield(publication)
        except asyncio.CancelledError:
            await publication
            raise
    finally:
        await asyncio.to_thread(temporary.cleanup)
