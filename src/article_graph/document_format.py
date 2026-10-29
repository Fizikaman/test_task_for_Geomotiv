"""Content validation only: no text extraction, rendering or provider calls."""

import codecs
from enum import StrEnum
from pathlib import Path

lazy import pypdfium2 as pdfium

from article_graph.domain import ErrorCode


class DocumentError(Exception):
    """A safe code, without input bytes or client metadata."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class DocumentFormat(StrEnum):
    MARKDOWN = "text/markdown"
    PDF = "application/pdf"


class FormatValidator:
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
                # Opening the document checks the PDF structure. Full page/content
                # validation and resource budgets belong to the later PDF pipeline.
                try:
                    with pdfium.PdfDocument(path) as document:
                        if len(document) == 0:
                            raise DocumentError(ErrorCode.INVALID_DOCUMENT)
                except pdfium.PdfiumError:
                    raise DocumentError(ErrorCode.INVALID_DOCUMENT) from None
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
