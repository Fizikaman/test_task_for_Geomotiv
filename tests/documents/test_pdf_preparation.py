"""Synthetic PDFs, real renderer/subprocess; no network or provider calls."""

import asyncio
import hashlib
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from uuid import UUID

import pytest

from article_graph.documents.blob_store import LocalBlobStore
from article_graph.documents.document_format import DocumentError, FormatValidator
from article_graph.documents.pdf_preparation import PreparedPdf, prepare_pdf
from article_graph.domain import ErrorCode
from article_graph.settings import Limits

REVISION = UUID(int=1)


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0
    result = bytearray()
    for value in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        result.append(value ^ state[(state[i] + state[j]) % 256])
    return bytes(result)


def make_pdf(
    path: Path,
    *,
    kind: str = "text",
    pages: int = 1,
    size: float = 100,
    rotate: int = 0,
    encrypted: bool = False,
) -> Path:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Count {pages} /Kids [".encode()
        + b" ".join(f"{5 + i} 0 R".encode() for i in range(pages))
        + b"] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    content = b"BT /F1 9 Tf 5 70 Td (Alice knows Bob.) Tj ET"
    if kind == "vector":
        content += b" 1 0 0 RG 3 w 10 20 m 90 20 l S 80 15 m 90 20 l 80 25 l S"
    elif kind == "scan":
        content = b"q 80 0 0 80 10 10 cm /Im1 Do Q"
    elif kind == "blank":
        content = b""
    trailer_extra = b""
    encryption_object = b""
    if encrypted:
        # Standard security handler R2, empty user password; valid and openable.
        padding = bytes.fromhex(
            "28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a"
        )
        owner = _rc4(hashlib.md5(b"owner" + padding[:27]).digest()[:5], padding)
        file_id = b"synthetic-pdf-id"
        permissions = struct.pack("<i", -4)
        key = hashlib.md5(padding + owner + permissions + file_id).digest()[:5]
        user = _rc4(key, padding)
        object_key = hashlib.md5(key + b"\x04\x00\x00\x00\x00").digest()[:10]
        content = _rc4(object_key, content)
        encryption_object = (
            b"<< /Filter /Standard /V 1 /R 2 /P -4 /O <"
            + owner.hex().encode()
            + b"> /U <"
            + user.hex().encode()
            + b"> >>"
        )
        trailer_extra = (
            f" /Encrypt {5 + pages} 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>]"
        ).encode()
    objects.append(
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
    )
    for _ in range(pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size} {size}] "
            f"/Rotate {rotate} ".encode()
            + b"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 "
            + f"{5 + pages} 0 R".encode()
            + b" >> >> /Contents 4 0 R >>"
        )
    if encrypted:
        objects.append(encryption_object)
    else:
        pixels = b"\x00\x00\x00\xff\xff\xff\xff\xff\xff\x00\x00\x00"
        compressed = zlib.compress(pixels)
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            + f"/Length {len(compressed)} >>\nstream\n".encode()
            + compressed
            + b"\nendstream"
        )
    data = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R".encode()
        + trailer_extra
        + f" >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(data)
    return path


def prepare(path: Path, tmp_path: Path, limits: Limits | None = None) -> PreparedPdf:
    return asyncio.run(
        prepare_pdf(
            path,
            revision_id=REVISION,
            limits=limits or Limits(),
            store=LocalBlobStore(tmp_path / "blobs", max_bytes=2_000_000),
        )
    )


def png_rgb(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position = 8
    compressed = bytearray()
    width = height = 0
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        crc = struct.unpack(">I", data[position + 8 + length : position + 12 + length])[
            0
        ]
        assert crc == zlib.crc32(kind + payload)
        if kind == b"IHDR":
            width, height, depth, color, _, _, _ = struct.unpack(">IIBBBBB", payload)
            assert (depth, color) == (8, 2)
        if kind == b"IDAT":
            compressed.extend(payload)
        position += length + 12
    rows = zlib.decompress(compressed)
    stride = width * 3 + 1
    assert len(rows) == height * stride
    assert all(rows[row * stride] == 0 for row in range(height))
    return (
        width,
        height,
        b"".join(rows[row * stride + 1 : (row + 1) * stride] for row in range(height)),
    )


@pytest.mark.parametrize("kind", ["text", "scan", "vector", "blank"])
def test_full_page_text_and_images(tmp_path: Path, kind: str) -> None:
    path = make_pdf(tmp_path / "input.pdf", kind=kind, pages=2)
    result = prepare(path, tmp_path)
    assert len(result.pages) == len(result.prepared.sources) == 2
    for number, page in enumerate(result.pages, 1):
        assert page.metadata.page == number
        assert page.metadata.dpi == 144
        assert page.metadata.media_type == "image/png"
        assert page.metadata.width_points == page.metadata.height_points == 100
        assert page.artifact_ref == f"artifacts/{page.image.content_hash}"
        image = (tmp_path / "blobs" / page.artifact_ref).read_bytes()
        assert hashlib.sha256(image).hexdigest() == page.image.content_hash
        width, height, pixels = png_rgb(image)
        assert width == page.metadata.width == height == page.metadata.height == 200
        source = result.prepared.sources[number - 1]
        assert source.page == number
        assert page.page == number
        assert page.text == source.text
        if kind in {"text", "vector"}:
            assert "Alice knows Bob." in source.text
        else:
            assert source.text == ""
        if kind == "vector":
            # Real red vector arrow survives full-page render alongside text.
            assert b"\xff\x00\x00" in pixels
        if kind == "scan":
            assert b"\x00\x00\x00" in pixels
        if kind == "blank":
            assert pixels == b"\xff" * (width * height * 3)
    for chunk in result.prepared.chunks:
        assert chunk.page is not None
        source = result.prepared.sources[chunk.page - 1]
        assert chunk.text == source.text[chunk.start : chunk.end]
    assert "Alice" not in repr(result)
    assert prepare(path, tmp_path) == result


@pytest.mark.parametrize(
    "limits",
    [
        Limits(pdf_pages=1),
        Limits(page_pixels=39_999),
        Limits(pdf_total_pixels=79_999),
        Limits(prepared_chars=10),
        Limits(chunks=1),
        Limits(upload_bytes=10),
    ],
)
def test_limits_leave_no_published_images(tmp_path: Path, limits: Limits) -> None:
    path = make_pdf(tmp_path / "input.pdf", pages=2)
    with pytest.raises(DocumentError) as error:
        prepare(path, tmp_path, limits)
    expected = (
        ErrorCode.PAYLOAD_TOO_LARGE
        if limits.upload_bytes == 10
        else ErrorCode.LIMIT_EXCEEDED
    )
    assert error.value.code == expected
    assert list((tmp_path / "blobs" / "artifacts").iterdir()) == []


def test_exact_pixel_budgets(tmp_path: Path) -> None:
    result = prepare(
        make_pdf(tmp_path / "input.pdf", pages=2),
        tmp_path,
        Limits(page_pixels=40_000, pdf_total_pixels=80_000),
    )
    assert len(result.pages) == 2


def test_fractional_rotated_page_and_giant_page(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "input.pdf", size=100.25, rotate=90, kind="vector")
    result = prepare(path, tmp_path)
    page = result.pages[0]
    assert page.metadata.width == page.metadata.height == 201
    _, _, pixels = png_rgb((tmp_path / "blobs" / page.artifact_ref).read_bytes())
    assert b"\xff\x00\x00" in pixels
    make_pdf(path, size=1_000_000)
    with pytest.raises(DocumentError) as error:
        prepare(path, tmp_path)
    assert error.value.code == ErrorCode.LIMIT_EXCEEDED


def test_corrupt_and_encrypted_pdf(tmp_path: Path) -> None:
    path = tmp_path / "input.pdf"
    path.write_bytes(b"%PDF-1.7\ncorrupt secret document")
    with pytest.raises(DocumentError) as error:
        prepare(path, tmp_path)
    assert str(error.value) == "invalid_document"
    make_pdf(path, encrypted=True)
    # Prove the encrypted fixture is valid and opens using the empty password.
    code = (
        "import pypdfium2 as p; import pypdfium2.raw as r; "
        "d=p.PdfDocument(__import__('sys').argv[1]); "
        "assert len(d)==1; assert r.FPDF_GetSecurityHandlerRevision(d.raw)==2; "
        "assert 'Alice' in d[0].get_textpage().get_text_bounded(); d.close()"
    )
    subprocess.run([sys.executable, "-c", code, str(path)], check=True, timeout=20)
    with pytest.raises(DocumentError) as error:
        prepare(path, tmp_path)
    assert error.value.code == ErrorCode.INVALID_DOCUMENT
    with pytest.raises(DocumentError) as error:
        FormatValidator().validate(path, "application/pdf")
    assert error.value.code == ErrorCode.INVALID_DOCUMENT


def test_ingestion_validation_limits_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_pdf(tmp_path / "input.pdf", pages=2)
    with pytest.raises(DocumentError) as error:
        FormatValidator(Limits(pdf_pages=1)).validate(path, "application/pdf")
    assert error.value.code == ErrorCode.LIMIT_EXCEEDED

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("test-only", 0.1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(DocumentError) as error:
        FormatValidator().validate(path, "application/pdf")
    assert error.value.code == ErrorCode.PROCESSING_TIMEOUT


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_kill_and_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    original = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []
    ready = asyncio.Event()

    async def slow_child(
        *args: str, stdout: int, stderr: int
    ) -> asyncio.subprocess.Process:
        process = await original(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=stdout,
            stderr=stderr,
        )
        children.append(process)
        ready.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_child)

    async def exercise() -> None:
        task = asyncio.create_task(
            prepare_pdf(
                tmp_path / "unused.pdf",
                revision_id=REVISION,
                limits=Limits(pdf_timeout_seconds=0.1),
                store=LocalBlobStore(tmp_path / "blobs", max_bytes=2_000_000),
            )
        )
        await asyncio.wait_for(ready.wait(), timeout=10)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(DocumentError) as error:
                await task
            assert error.value.code == ErrorCode.PROCESSING_TIMEOUT
        assert len(children) == 1
        assert children[0].returncode is not None

    asyncio.run(exercise())


def test_event_loop_remains_responsive(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "input.pdf", pages=4)
    store = LocalBlobStore(tmp_path / "blobs", max_bytes=2_000_000)

    async def exercise() -> None:
        task = asyncio.create_task(
            prepare_pdf(path, revision_id=REVISION, limits=Limits(), store=store)
        )
        turns = 0
        async with asyncio.timeout(20):
            while not task.done():
                turns += 1
                await asyncio.sleep(0.001)
        assert len((await task).pages) == 4
        assert turns > 5

    asyncio.run(exercise())


def test_lazy_pdfium_is_not_loaded_by_orchestrator() -> None:
    code = """
import sys
from article_graph.documents.pdf_preparation import prepare_pdf
from article_graph.documents.pdf_worker import restrict_memory
assert not {'pypdfium2', 'pypdfium2.raw'} & sys.modules.keys()
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)


def test_worker_lazy_import_executes_when_used(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "input.pdf")
    code = """
import sys
from pathlib import Path
from article_graph.settings import Limits
from article_graph.documents.pdf_worker import validate_pdf
assert 'pypdfium2' not in sys.modules
assert validate_pdf(Path(sys.argv[1]), Limits()) == 0
assert 'pypdfium2' in sys.modules
"""
    subprocess.run([sys.executable, "-c", code, str(path)], check=True, timeout=20)


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is a Linux contract")
def test_real_linux_memory_limit() -> None:
    code = """
from article_graph.documents.pdf_worker import restrict_memory
import resource
restrict_memory(128 * 1024 * 1024)
assert resource.getrlimit(resource.RLIMIT_AS) == (128 * 1024 * 1024,) * 2
try:
    bytearray(256 * 1024 * 1024)
except MemoryError:
    pass
else:
    raise AssertionError('memory was not restricted')
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)
