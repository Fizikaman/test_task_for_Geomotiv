"""Content validation only: no text extraction, rendering or provider calls."""

import codecs
import sys
from enum import IntEnum, StrEnum
from pathlib import Path
lazy import subprocess

from article_graph.domain import ErrorCode
from article_graph.settings import Limits


class DocumentError(Exception):
    """A safe code, without input bytes or client metadata."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class DocumentFormat(StrEnum):
    MARKDOWN = "text/markdown"
    PDF = "application/pdf"


class PdfValidationExit(IntEnum):
    SUCCESS = 0
    INVALID = 1
    LIMIT = 2


class FormatValidator:
    def __init__(self, limits: Limits | None = None) -> None:
        self._limits = limits or Limits()

    def validate(self, path: Path, content_type: str) -> DocumentFormat:
        declared = content_type.partition(";")[0].strip().lower()
        if declared not in {"text/plain", "text/markdown", "application/pdf"}:
            raise DocumentError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)
        with path.open("rb") as stream:
            is_pdf = stream.read(5) == b"%PDF-"
            stream.seek(0)
            if is_pdf:
                if declared != "application/pdf":
                    raise DocumentError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)
                # Keep even ingestion validation out of this process. Call this
                # synchronous storage interface off the API event loop.
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "article_graph.documents.pdf_worker",
                            "--validate",
                            str(path.resolve()),
                            self._limits.model_dump_json(),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=self._limits.pdf_timeout_seconds,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    raise DocumentError(ErrorCode.PROCESSING_TIMEOUT) from None
                if result.returncode != PdfValidationExit.SUCCESS:
                    raise DocumentError(
                        ErrorCode.LIMIT_EXCEEDED
                        if result.returncode == PdfValidationExit.LIMIT
                        else ErrorCode.INVALID_DOCUMENT
                    )
                return DocumentFormat.PDF
            if declared == "application/pdf":
                raise DocumentError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            has_text = False
            try:
                while block := stream.read(64 * 1024):
                    text = decoder.decode(block)
                    # Markdown is plain Unicode text; binary control characters
                    # are not accepted as Markdown even when valid UTF-8.
                    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                        raise DocumentError(ErrorCode.INVALID_DOCUMENT)
                    has_text = has_text or bool(text.strip("\ufeff \t\r\n"))
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                raise DocumentError(ErrorCode.INVALID_DOCUMENT) from None
            if not has_text:
                raise DocumentError(ErrorCode.VALIDATION_ERROR)
        return DocumentFormat.MARKDOWN
