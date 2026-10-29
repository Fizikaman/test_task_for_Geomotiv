"""Private subprocess entry point. Apply Linux limits BEFORE importing PDFium."""

import math
import struct
import sys
import zlib
from contextlib import closing
from pathlib import Path
from uuid import UUID

lazy import pypdfium2 as pdfium
lazy import pypdfium2.raw as raw

from article_graph.document_format import DocumentError, PdfValidationExit
from article_graph.domain import ErrorCode
from article_graph.pdf_preparation import PageMetadata, PdfManifest
from article_graph.preparation import SourceText, prepare_sources
from article_graph.settings import Limits


def restrict_memory(memory_bytes: int) -> None:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _check_document(document: pdfium.PdfDocument, limits: Limits) -> None:
    if raw.FPDF_GetSecurityHandlerRevision(document.raw) != -1 or not len(document):
        raise DocumentError(ErrorCode.INVALID_DOCUMENT)
    if len(document) > limits.pdf_pages:
        raise DocumentError(ErrorCode.LIMIT_EXCEEDED)


def validate_pdf(path: Path, limits: Limits) -> int:
    try:
        restrict_memory(limits.pdf_memory_bytes)
        if path.stat().st_size > limits.upload_bytes:
            return PdfValidationExit.LIMIT
        with pdfium.PdfDocument(path) as document:
            _check_document(document, limits)
    except DocumentError as error:
        return (
            PdfValidationExit.LIMIT
            if error.code == ErrorCode.LIMIT_EXCEEDED
            else PdfValidationExit.INVALID
        )
    except MemoryError:
        return PdfValidationExit.LIMIT
    except Exception:
        return PdfValidationExit.INVALID
    return PdfValidationExit.SUCCESS


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def _write_png(bitmap: pdfium.PdfBitmap, path: Path) -> None:
    # RGB, no Pillow/NumPy dependency. Encode rows without stride padding.
    compressor = zlib.compressobj()
    with path.open("wb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")
        stream.write(
            _chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", bitmap.width, bitmap.height, 8, 2, 0, 0, 0),
            )
        )
        for row in range(bitmap.height):
            start = row * bitmap.stride
            data = compressor.compress(
                b"\0" + bytes(bitmap.buffer[start : start + bitmap.width * 3])
            )
            if data:
                stream.write(_chunk(b"IDAT", data))
        stream.write(_chunk(b"IDAT", compressor.flush()))
        stream.write(_chunk(b"IEND", b""))


def process_pdf(path: Path, directory: Path, revision_id: UUID, limits: Limits) -> None:
    if path.stat().st_size > limits.upload_bytes:
        raise DocumentError(ErrorCode.PAYLOAD_TOO_LARGE)
    sources: list[SourceText] = []
    metadata: list[PageMetadata] = []
    total_pixels = total_chars = 0
    with pdfium.PdfDocument(path) as document:
        document.init_forms()
        # Even PDFs accepting an empty password are rejected if encrypted.
        _check_document(document, limits)
        for index in range(len(document)):
            with closing(document[index]) as page:
                width_points, height_points = page.get_size()
                scale = limits.pdf_dpi / 72
                if not all(
                    math.isfinite(value) and value > 0
                    for value in (width_points, height_points)
                ):
                    raise DocumentError(ErrorCode.INVALID_DOCUMENT)
                width = math.ceil(width_points * scale)
                height = math.ceil(height_points * scale)
                pixels = width * height
                total_pixels += pixels
                if (
                    pixels > limits.page_pixels
                    or total_pixels > limits.pdf_total_pixels
                ):
                    raise DocumentError(ErrorCode.LIMIT_EXCEEDED)
                with closing(page.get_textpage()) as textpage:
                    if textpage.count_chars() > limits.prepared_chars - total_chars:
                        raise DocumentError(ErrorCode.LIMIT_EXCEEDED)
                    text = textpage.get_text_bounded(errors="strict")
                total_chars += len(text)
                if total_chars > limits.prepared_chars:
                    raise DocumentError(ErrorCode.LIMIT_EXCEEDED)
                sources.append(SourceText(f"page-{index + 1}", text, index + 1))
                with closing(
                    page.render(
                        scale=scale,
                        rev_byteorder=True,
                        force_bitmap_format=raw.FPDFBitmap_BGR,
                        draw_annots=True,
                    )
                ) as bitmap:
                    _write_png(bitmap, directory / f"{index + 1}.png")
                metadata.append(
                    PageMetadata(
                        page=index + 1,
                        width=width,
                        height=height,
                        width_points=width_points,
                        height_points=height_points,
                        dpi=limits.pdf_dpi,
                    )
                )
    prepared = prepare_sources(
        tuple(sources), revision_id=revision_id, limits=limits, allow_empty=True
    )
    (directory / "manifest.json").write_text(
        PdfManifest(pages=tuple(metadata), prepared=prepared).model_dump_json(),
        encoding="utf-8",
    )


def main() -> int:
    if sys.argv[1] == "--validate":
        return validate_pdf(Path(sys.argv[2]), Limits.model_validate_json(sys.argv[3]))
    path, directory = Path(sys.argv[1]), Path(sys.argv[2])
    limits = Limits.model_validate_json(sys.argv[4])
    try:
        restrict_memory(limits.pdf_memory_bytes)
        process_pdf(path, directory, UUID(sys.argv[3]), limits)
    except DocumentError as error:
        (directory / "error").write_text(error.code.value, encoding="ascii")
        return 1
    except MemoryError:
        (directory / "error").write_text(
            ErrorCode.LIMIT_EXCEEDED.value, encoding="ascii"
        )
        return 1
    except Exception:
        (directory / "error").write_text(
            ErrorCode.INVALID_DOCUMENT.value, encoding="ascii"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
