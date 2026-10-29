"""Local immutable blobs. The application exclusively owns the storage root."""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol

from article_graph.document_format import DocumentError, DocumentFormat, FormatValidator
from article_graph.domain import ErrorCode
from article_graph.settings import Limits


class BlobNamespace(StrEnum):
    SOURCES = "sources"
    ARTIFACTS = "artifacts"


@dataclass(frozen=True)
class Blob:
    namespace: BlobNamespace
    content_hash: str
    size: int
    format: DocumentFormat | None = None


class BlobStore(Protocol):
    def put_source(self, stream: BinaryIO, *, content_type: str) -> Blob: ...

    def put_artifact(self, stream: BinaryIO) -> Blob: ...

    def open(self, namespace: BlobNamespace, content_hash: str) -> BinaryIO: ...


class LocalBlobStore:
    def __init__(
        self, root: Path, *, max_bytes: int, limits: Limits | None = None
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._root = root.resolve()
        self._max_bytes = max_bytes
        self._validator = FormatValidator(limits)
        self._root.mkdir(parents=True, exist_ok=True)
        for namespace in BlobNamespace:
            self._checked(self._root / namespace.value).mkdir(exist_ok=True)

    def _checked(self, path: Path) -> Path:
        # Check both containment and links, including dangling links/junctions.
        if not path.resolve().is_relative_to(self._root):
            raise ValueError("blob path escapes storage root")
        for item in (path, *path.parents):
            if item == self._root:
                break
            if item.is_symlink() or item.is_junction():
                raise ValueError("blob path contains a link")
        return path

    def _path(self, namespace: BlobNamespace, content_hash: str) -> Path:
        if not isinstance(namespace, BlobNamespace):
            raise ValueError("invalid blob namespace")
        if re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
            raise ValueError("invalid blob hash")
        return self._checked(self._root / namespace.value / content_hash)

    def open(self, namespace: BlobNamespace, content_hash: str) -> BinaryIO:
        """Read-only access; callers close the returned stream."""
        return self._path(namespace, content_hash).open("rb")

    def put_source(self, stream: BinaryIO, *, content_type: str) -> Blob:
        return self._put(stream, BlobNamespace.SOURCES, content_type)

    def put_artifact(self, stream: BinaryIO) -> Blob:
        return self._put(stream, BlobNamespace.ARTIFACTS, None)

    def _put(
        self, stream: BinaryIO, namespace: BlobNamespace, content_type: str | None
    ) -> Blob:
        directory = self._checked(self._root / namespace.value)
        digest = hashlib.sha256()
        size = 0
        # Staging is on the same filesystem as the destination. Final hash names
        # become visible only after close, fsync and (for sources) validation.
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".pending-", delete=False
        ) as temporary:
            pending = Path(temporary.name)
            try:
                while block := stream.read(min(64 * 1024, self._max_bytes - size + 1)):
                    size += len(block)
                    if size > self._max_bytes:
                        raise DocumentError(ErrorCode.PAYLOAD_TOO_LARGE)
                    digest.update(block)
                    temporary.write(block)
                temporary.flush()
                os.fsync(temporary.fileno())
            except BaseException:
                temporary.close()
                pending.unlink(missing_ok=True)
                raise
        try:
            document_format = (
                self._validator.validate(pending, content_type)
                if content_type is not None
                else None
            )
            content_hash = digest.hexdigest()
            target = self._path(namespace, content_hash)
            try:
                # Atomic no-replace publication: unlike POSIX rename/replace this
                # never overwrites an existing blob, even across processes.
                os.link(pending, target)
            except FileExistsError:
                # A repeated/concurrent write must resolve to the same bytes.
                with self.open(namespace, content_hash) as existing:
                    existing_digest = hashlib.sha256()
                    existing_size = 0
                    while block := existing.read(64 * 1024):
                        existing_size += len(block)
                        if existing_size > size:
                            raise ValueError("stored blob integrity failure") from None
                        existing_digest.update(block)
                    if existing_digest.hexdigest() != content_hash:
                        raise ValueError("stored blob integrity failure") from None
            return Blob(namespace, content_hash, size, document_format)
        finally:
            pending.unlink(missing_ok=True)
