"""Lossless, local text preparation. Offsets count Unicode code points, not bytes."""

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

from article_graph.document_format import DocumentError
from article_graph.domain import ErrorCode
from article_graph.settings import Limits

_VERSION = "text-v1"


@dataclass(frozen=True)
class SourceText:
    """One Markdown document or one future PDF page; text is never normalized."""

    source_id: str
    text: str = field(repr=False)
    page: int | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip() or (self.page is not None and self.page < 1):
            raise ValueError("invalid source identity")


@dataclass(frozen=True)
class TextChunk:
    fragment_id: str
    source_id: str
    page: int | None
    start: int
    end: int
    text: str = field(repr=False)


@dataclass(frozen=True)
class PreparedDocument:
    revision_id: UUID
    sources: tuple[SourceText, ...]
    chunks: tuple[TextChunk, ...]
    version: str = _VERSION


def prepare_markdown(
    text: str, *, revision_id: UUID, limits: Limits
) -> PreparedDocument:
    """Preserve markup, alt text, captions and URLs without fetching anything."""
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        raise DocumentError(ErrorCode.INVALID_DOCUMENT)
    return prepare_sources(
        (SourceText("markdown", text),), revision_id=revision_id, limits=limits
    )


def prepare_sources(
    sources: tuple[SourceText, ...], *, revision_id: UUID, limits: Limits
) -> PreparedDocument:
    """Page-local spans, document-wide budgets including repeated overlap.

    Empty PDF page text is retained for future visual extraction. A document
    without any text is rejected here; image-only PDF preparation belongs to P10.
    No partial result escapes if any source or the aggregate exceeds a budget.
    """
    if len({source.source_id for source in sources}) != len(sources):
        raise ValueError("duplicate source identity")
    if sum(len(source.text) for source in sources) > limits.prepared_chars:
        raise DocumentError(ErrorCode.LIMIT_EXCEEDED)
    if not any(source.text.strip("\ufeff \t\r\n") for source in sources):
        raise DocumentError(ErrorCode.VALIDATION_ERROR)
    chunks: list[TextChunk] = []
    total = 0
    for source in sources:
        # Prefer paragraph boundaries, then whole lines (including table rows),
        # then whitespace. Huge unbroken lines have a bounded hard-split fallback.
        boundaries = tuple(
            [match.end() for match in re.finditer(pattern, source.text)]
            for pattern in (r"(?:\r?\n)[ \t]*(?:\r?\n)", r"\n", r"\s+")
        )
        digest = sha256(source.text.encode("utf-8")).hexdigest()
        start = previous_end = 0
        while start < len(source.text):
            end = min(start + limits.chunk_chars, len(source.text))
            if end < len(source.text):
                minimum = max(previous_end + 1, start + limits.chunk_chars // 2)
                for positions in boundaries:
                    index = bisect_right(positions, end) - 1
                    if index >= 0 and positions[index] >= minimum:
                        end = positions[index]
                        break
            total += end - start
            if len(chunks) >= limits.chunks or total > limits.prepared_chars:
                raise DocumentError(ErrorCode.LIMIT_EXCEEDED)
            # Length-prefix the arbitrary source identity to avoid delimiter ambiguity.
            identity = (
                f"{_VERSION}:{revision_id}:{len(source.source_id)}:"
                f"{source.source_id}:{source.page}:{digest}:{start}:{end}"
            )
            chunks.append(
                TextChunk(
                    fragment_id="chunk-" + sha256(identity.encode("utf-8")).hexdigest(),
                    source_id=source.source_id,
                    page=source.page,
                    start=start,
                    end=end,
                    text=source.text[start:end],
                )
            )
            if end == len(source.text):
                break
            start = max(start + 1, end - limits.chunk_overlap_chars)
            # Keep overlap on whitespace boundaries when possible, never enlarge it.
            positions = boundaries[2]
            index = bisect_left(positions, start)
            if index < len(positions) and positions[index] <= end:
                start = positions[index]
            previous_end = end
    return PreparedDocument(revision_id, sources, tuple(chunks))
