"""P07: bounded streams, content validation and atomic immutable publication."""

import hashlib
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import override
lazy from subprocess import run

import pytest
lazy import pypdfium2 as pdfium

from article_graph.blob_store import BlobNamespace, LocalBlobStore
from article_graph.document_format import DocumentError, DocumentFormat, FormatValidator
from article_graph.domain import ErrorCode


@pytest.fixture
def store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs", max_bytes=200_000)


@pytest.fixture
def pdf_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "fixture.pdf"
    with pdfium.PdfDocument.new() as document:
        document.new_page(72, 72).close()
        document.save(path)
    return path.read_bytes()


def test_source_hashes_original_bytes_and_separates_artifacts(
    store: LocalBlobStore, tmp_path: Path
) -> None:
    data = "# Статья\r\nА и Б\n".encode()
    source = store.put_source(
        io.BytesIO(data), content_type="text/plain; charset=utf-8"
    )
    artifact = store.put_artifact(io.BytesIO(data))
    assert source.content_hash == hashlib.sha256(data).hexdigest()
    assert source.size == len(data)
    assert source.format == DocumentFormat.MARKDOWN
    assert artifact.format is None
    for namespace in BlobNamespace:
        with store.open(namespace, source.content_hash) as stream:
            assert stream.read() == data
            assert not stream.writable()
        assert list((tmp_path / "blobs" / namespace.value).iterdir()) == [
            tmp_path / "blobs" / namespace.value / source.content_hash
        ]


def test_size_limit_stops_before_full_upload(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path, max_bytes=10)
    stream = io.BytesIO(b"a" * 1_000_000)
    with pytest.raises(DocumentError) as caught:
        store.put_source(stream, content_type="text/markdown")
    assert caught.value.code == ErrorCode.PAYLOAD_TOO_LARGE
    assert stream.tell() == 11
    assert list((tmp_path / "sources").iterdir()) == []
    blob = store.put_source(io.BytesIO(b"a" * 10), content_type="text/markdown")
    assert blob.size == 10


@pytest.mark.parametrize(
    ("data", "content_type", "code"),
    [
        (b"hello\xff", "text/markdown", ErrorCode.INVALID_DOCUMENT),
        (b"hello\xe2\x82", "text/plain", ErrorCode.INVALID_DOCUMENT),
        (b"hello\x00", "text/markdown", ErrorCode.INVALID_DOCUMENT),
        (b"", "text/markdown", ErrorCode.VALIDATION_ERROR),
        (b" \r\n\t", "text/plain", ErrorCode.VALIDATION_ERROR),
        (b"\xef\xbb\xbf", "text/plain", ErrorCode.VALIDATION_ERROR),
        (b"# Markdown", "application/pdf", ErrorCode.UNSUPPORTED_MEDIA_TYPE),
        (b"%PDF-1.7\nnot a PDF", "text/plain", ErrorCode.UNSUPPORTED_MEDIA_TYPE),
        (b"%PDF-1.7\nnot a PDF", "application/pdf", ErrorCode.INVALID_DOCUMENT),
        (b"# Markdown", "image/png", ErrorCode.UNSUPPORTED_MEDIA_TYPE),
    ],
)
def test_invalid_sources_leave_no_files(
    store: LocalBlobStore,
    tmp_path: Path,
    data: bytes,
    content_type: str,
    code: ErrorCode,
) -> None:
    with pytest.raises(DocumentError) as caught:
        store.put_source(io.BytesIO(data), content_type=content_type)
    assert caught.value.code == code
    assert str(caught.value) == code.value
    assert list((tmp_path / "blobs" / "sources").iterdir()) == []


def test_pdf_is_parsed_and_type_mismatch_is_rejected(
    store: LocalBlobStore, pdf_bytes: bytes
) -> None:
    blob = store.put_source(io.BytesIO(pdf_bytes), content_type="application/pdf")
    assert blob.format == DocumentFormat.PDF
    with pytest.raises(DocumentError) as caught:
        store.put_source(io.BytesIO(pdf_bytes), content_type="text/markdown")
    assert caught.value.code == ErrorCode.UNSUPPORTED_MEDIA_TYPE


def test_utf8_decoder_handles_split_codepoint(store: LocalBlobStore) -> None:
    data = b"a" * (64 * 1024 - 1) + "Я".encode()
    assert store.put_source(io.BytesIO(data), content_type="text/plain").size == len(
        data
    )


@pytest.mark.parametrize(
    "content_hash",
    ["../outside", "..\\outside", "/absolute", "C:\\outside", "a" * 63, "A" * 64],
)
def test_traversal_rejected(store: LocalBlobStore, content_hash: str) -> None:
    with pytest.raises(ValueError, match="invalid blob hash"):
        store.open(BlobNamespace.SOURCES, content_hash)


def test_symlink_destination_rejected(store: LocalBlobStore, tmp_path: Path) -> None:
    content_hash = hashlib.sha256(b"hello").hexdigest()
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    try:
        (tmp_path / "blobs" / "sources" / content_hash).symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    with pytest.raises(ValueError):
        store.put_source(io.BytesIO(b"hello"), content_type="text/plain")
    assert outside.read_bytes() == b"untouched"
    assert not list((tmp_path / "blobs").rglob(".pending-*"))


def test_repeat_write_preserves_existing_file(
    store: LocalBlobStore, tmp_path: Path
) -> None:
    blob = store.put_source(io.BytesIO(b"hello"), content_type="text/plain")
    path = tmp_path / "blobs" / "sources" / blob.content_hash
    original = path.stat()
    assert store.put_source(io.BytesIO(b"hello"), content_type="text/plain") == blob
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    assert path.stat().st_ino == original.st_ino


def test_concurrent_instances_publish_one_complete_blob(tmp_path: Path) -> None:
    barrier = Barrier(8)
    data = b"# Markdown\n" * 1000

    def write(_: int) -> str:
        store = LocalBlobStore(tmp_path, max_bytes=len(data))
        barrier.wait(timeout=10)
        blob = store.put_source(io.BytesIO(data), content_type="text/markdown")
        with store.open(blob.namespace, blob.content_hash) as stream:
            assert stream.read() == data
        return blob.content_hash

    with ThreadPoolExecutor(max_workers=8) as executor:
        hashes = list(executor.map(write, range(8)))
    assert len(set(hashes)) == 1
    assert [path.name for path in (tmp_path / "sources").iterdir()] == [hashes[0]]


def test_read_failure_cleans_staging(store: LocalBlobStore, tmp_path: Path) -> None:
    class BrokenStream(io.BytesIO):
        @override
        def read(self, size: int | None = -1, /) -> bytes:
            if self.tell():
                raise OSError("test read failure")
            return super().read(3)

    with pytest.raises(OSError, match="test read failure"):
        store.put_artifact(BrokenStream(b"abcdef"))
    assert list((tmp_path / "blobs" / "artifacts").iterdir()) == []


def test_publication_failure_cleans_staging(
    store: LocalBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_link(source: Path, destination: Path) -> None:
        assert source.read_bytes() == b"hello"
        assert not destination.exists()
        raise OSError("test publication failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="test publication failure"):
        store.put_source(io.BytesIO(b"hello"), content_type="text/plain")
    assert list((tmp_path / "blobs" / "sources").iterdir()) == []


def test_validation_occurs_before_publication(
    store: LocalBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def validate(
        self: FormatValidator, path: Path, content_type: str
    ) -> DocumentFormat:
        assert path.name.startswith(".pending-")
        assert path.read_bytes() == b"hello"
        assert len(list(path.parent.iterdir())) == 1
        raise DocumentError(ErrorCode.INVALID_DOCUMENT)

    monkeypatch.setattr(FormatValidator, "validate", validate)
    with pytest.raises(DocumentError):
        store.put_source(io.BytesIO(b"hello"), content_type="text/plain")
    assert list((tmp_path / "blobs" / "sources").iterdir()) == []


def test_pdf_validation_keeps_pdfium_in_child(tmp_path: Path, pdf_bytes: bytes) -> None:
    path = tmp_path / "probe.pdf"
    path.write_bytes(pdf_bytes)
    result = run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from pathlib import Path\n"
            "from article_graph.blob_store import LocalBlobStore\n"
            "from article_graph.document_format import FormatValidator\n"
            "assert 'pypdfium2' not in sys.modules\n"
            "FormatValidator().validate(Path(sys.argv[1]), 'application/pdf')\n"
            "assert 'pypdfium2' not in sys.modules\n",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_invalid_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        LocalBlobStore(tmp_path, max_bytes=0)


@pytest.mark.parametrize("corrupt", [b"corrupt", b"wrong"])
def test_corrupt_existing_blob_is_not_overwritten(
    store: LocalBlobStore, tmp_path: Path, corrupt: bytes
) -> None:
    content_hash = hashlib.sha256(b"hello").hexdigest()
    target = tmp_path / "blobs" / "sources" / content_hash
    target.write_bytes(corrupt)
    with pytest.raises(ValueError, match="integrity"):
        store.put_source(io.BytesIO(b"hello"), content_type="text/plain")
    assert target.read_bytes() == corrupt
    assert not list(target.parent.glob(".pending-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction protection")
def test_junction_directory_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    result = run(
        ["cmd", "/c", "mklink", "/J", str(root / "sources"), str(outside)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    with pytest.raises(ValueError, match="escapes storage root"):
        LocalBlobStore(root, max_bytes=10)
    assert list(outside.iterdir()) == []


def test_concurrent_processes_publish_one_blob(tmp_path: Path) -> None:
    script = (
        "import io, sys\n"
        "from pathlib import Path\n"
        "from article_graph.blob_store import LocalBlobStore\n"
        "store = LocalBlobStore(Path(sys.argv[1]), max_bytes=200000)\n"
        "for _ in range(10):\n"
        "    store.put_source(io.BytesIO(b'# Markdown\\n' * 10000), "
        "content_type='text/markdown')\n"
    )

    def process(_: int) -> None:
        result = run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(process, range(4)))
    data = b"# Markdown\n" * 10000
    assert [path.name for path in (tmp_path / "sources").iterdir()] == [
        hashlib.sha256(data).hexdigest()
    ]
    assert next((tmp_path / "sources").iterdir()).read_bytes() == data


def test_fsync_failure_cleans_staging(
    store: LocalBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_sync(descriptor: int) -> None:
        raise OSError("test sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="test sync failure"):
        store.put_artifact(io.BytesIO(b"artifact"))
    assert list((tmp_path / "blobs" / "artifacts").iterdir()) == []
