"""Document-local extraction; global identity resolution belongs to P12."""

import asyncio
import json
from dataclasses import dataclass
from importlib.resources import files
from io import BytesIO
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from article_graph.blob_store import Blob, BlobStore
from article_graph.domain import (
    DTO,
    Evidence,
    ExtractedCharacter,
    ExtractedRelation,
    Extraction,
    TextEvidence,
)
from article_graph.llm import (
    GenerationRequest,
    GenerationResult,
    ImageBlock,
    LLMError,
    TextBlock,
    Usage,
)
from article_graph.pdf_preparation import PreparedPdf
from article_graph.preparation import PreparedDocument, TextChunk
from article_graph.settings import Limits

PROMPT_VERSION = "document-v1"


class ExtractionProvider(Protocol):
    async def generate_with_metadata[T: BaseModel](
        self, request: GenerationRequest, response_model: type[T]
    ) -> GenerationResult[T]: ...


class CallRecord(DTO):
    fragment_id: str
    page: int | None
    requested_model: str
    model: str | None
    usage: Usage | None
    extraction: Extraction = Field(repr=False)


class ExtractionArtifact(DTO):
    version: str = "extraction-v1"
    prompt_version: str = PROMPT_VERSION
    preparation_version: str
    revision_id: UUID
    extraction: Extraction = Field(repr=False)
    calls: tuple[CallRecord, ...]


@dataclass(frozen=True)
class _Input:
    chunk: TextChunk
    request: GenerationRequest
    image_fragment_id: str | None


def _inputs(
    document: PreparedDocument | PreparedPdf, store: BlobStore, limits: Limits
) -> tuple[PreparedDocument, tuple[_Input, ...]]:
    pdf = document if isinstance(document, PreparedPdf) else None
    prepared = pdf.prepared if pdf is not None else document
    assert isinstance(prepared, PreparedDocument)
    sources = {source.source_id: source for source in prepared.sources}
    pages = {page.source.source_id: page for page in pdf.pages} if pdf else {}
    if len(sources) != len(prepared.sources) or (
        pdf is not None
        and (
            len(pages) != len(pdf.pages)
            or set(pages) != set(sources)
            or len({page.page for page in pdf.pages}) != len(pdf.pages)
            or {page.page for page in pdf.pages} != set(range(1, len(pdf.pages) + 1))
            or any(page.page != page.source.page for page in pdf.pages)
            or any(page.source != sources[key] for key, page in pages.items())
        )
    ):
        raise LLMError("prepared_document_invalid")
    chunks = list(prepared.chunks)
    for source in prepared.sources:
        if source.page is not None and source.source_id not in pages:
            raise LLMError("prepared_document_invalid")
        if not source.text and source.source_id in pages:
            chunks.append(
                TextChunk(
                    f"page-{source.page}", source.source_id, source.page, 0, 0, ""
                )
            )
        spans = sorted(
            (chunk.start, chunk.end)
            for chunk in chunks
            if chunk.source_id == source.source_id
        )
        covered = 0
        for start, end in spans:
            if start > covered:
                raise LLMError("prepared_document_invalid")
            covered = max(covered, end)
        if covered != len(source.text):
            raise LLMError("prepared_document_invalid")
    if not chunks or len({c.fragment_id for c in chunks}) != len(chunks):
        raise LLMError("prepared_document_invalid")
    if len(chunks) > min(limits.chunks, limits.llm_calls):
        raise LLMError("limit_exceeded")
    if sum(len(c.text) for c in chunks) > limits.prepared_chars:
        raise LLMError("limit_exceeded")
    system = (
        files("article_graph")
        .joinpath("prompts/document-system.v1.txt")
        .read_text(encoding="utf-8")
    )
    user = (
        files("article_graph")
        .joinpath("prompts/document-user.v1.txt")
        .read_text(encoding="utf-8")
    )
    inputs: list[_Input] = []
    total = 0
    for chunk in chunks:
        source = sources.get(chunk.source_id)
        if (
            source is None
            or chunk.page != source.page
            or not 0 <= chunk.start <= chunk.end <= len(source.text)
            or chunk.text != source.text[chunk.start : chunk.end]
        ):
            raise LLMError("prepared_document_invalid")
        if len(chunk.text) > limits.chunk_chars:
            raise LLMError("limit_exceeded")
        page = pages.get(chunk.source_id)
        image_id = f"page-{page.page}" if page else None
        envelope = json.dumps(
            dict(
                fragment_id=chunk.fragment_id,
                page=chunk.page,
                image_fragment_id=image_id,
                text=chunk.text,
            ),
            ensure_ascii=False,
        )
        blocks: list[TextBlock | ImageBlock] = [TextBlock(text=user + "\n" + envelope)]
        size = len((system + user + envelope).encode("utf-8"))
        if page:
            # Bound the read independently of possibly malformed blob metadata.
            with store.open(page.image.namespace, page.image.content_hash) as stream:
                data = stream.read(limits.llm_input_bytes + 1)
            size += 4 * ((len(data) + 2) // 3)
            if size > limits.llm_input_bytes:
                raise LLMError("limit_exceeded")
            blocks.append(ImageBlock(media_type="image/png", data=data))
        total += size
        if size > limits.llm_input_bytes or total > limits.llm_total_input_bytes:
            raise LLMError("limit_exceeded")
        inputs.append(
            _Input(
                chunk, GenerationRequest(system=system, content=tuple(blocks)), image_id
            )
        )
    return prepared, tuple(inputs)


def _validate(result: Extraction, item: _Input, limits: Limits) -> None:
    try:
        result.check_limits(limits)
    except ValueError:
        raise LLMError("limit_exceeded") from None
    for relation in result.relations:
        for evidence in relation.evidence:
            if isinstance(evidence, TextEvidence):
                valid = (
                    evidence.fragment_id == item.chunk.fragment_id
                    and evidence.page == item.chunk.page
                    and evidence.quote in item.chunk.text
                )
            else:
                valid = (
                    item.image_fragment_id is not None
                    and evidence.fragment_id == item.image_fragment_id
                    and evidence.page == item.chunk.page
                )
            if not valid:
                raise LLMError("evidence_invalid")


def _anchors(
    character: ExtractedCharacter, extraction: Extraction, chunk: TextChunk
) -> frozenset[tuple[str, int, int]]:
    """Only a unique verbatim occurrence can establish an overlap duplicate."""
    anchors: set[tuple[str, int, int]] = set()
    for relation in extraction.relations:
        if character.local_id not in {relation.subject_id, relation.object_id}:
            continue
        for evidence in relation.evidence:
            if not isinstance(evidence, TextEvidence):
                continue
            if not any(
                name in evidence.quote for name in (character.name, *character.aliases)
            ):
                continue
            offset = chunk.text.find(evidence.quote)
            if offset >= 0 and chunk.text.find(evidence.quote, offset + 1) < 0:
                start = chunk.start + offset
                anchors.add((chunk.source_id, start, start + len(evidence.quote)))
    return frozenset(anchors)


def _merge(inputs: tuple[_Input, ...], records: tuple[CallRecord, ...]) -> Extraction:
    characters: list[ExtractedCharacter] = []
    occurrences: list[
        tuple[int, TextChunk, ExtractedCharacter, frozenset[tuple[str, int, int]]]
    ] = []
    relations: list[ExtractedRelation] = []
    relation_keys: dict[tuple[str, str, str, bool], int] = {}
    for item, record in zip(inputs, records, strict=True):
        mapping: dict[str, str] = {}
        for character in record.extraction.characters:
            # Same contextual identity and the same unique source occurrence.
            # Without an anchor, leave identity resolution to P12.
            signature = character.model_dump(exclude={"local_id", "aliases"})
            anchors = _anchors(character, record.extraction, item.chunk)
            unique = (
                sum(
                    candidate.model_dump(exclude={"local_id", "aliases"}) == signature
                    for candidate in record.extraction.characters
                )
                == 1
            )
            matches = {
                index
                for index, chunk, previous, previous_anchors in occurrences
                if unique
                and chunk.fragment_id != item.chunk.fragment_id
                and anchors & previous_anchors
                and previous.model_dump(exclude={"local_id", "aliases"}) == signature
            }
            available = matches - {
                index
                for index, char in enumerate(characters)
                if char.local_id in mapping.values()
            }
            if len(matches) == 1 and len(available) == 1:
                index = available.pop()
                aliases = tuple(
                    dict.fromkeys((*characters[index].aliases, *character.aliases))
                )
                characters[index] = characters[index].model_copy(
                    update={"aliases": aliases}
                )
            else:
                index = len(characters)
                characters.append(
                    character.model_copy(update={"local_id": f"c{index}"})
                )
            mapping[character.local_id] = characters[index].local_id
            occurrences.append((index, item.chunk, character, anchors))
        for relation in record.extraction.relations:
            subject, object_id = (
                mapping[relation.subject_id],
                mapping[relation.object_id],
            )
            if not relation.directed:
                subject, object_id = sorted((subject, object_id))
            key = subject, object_id, relation.kind, relation.directed
            if key in relation_keys:
                index = relation_keys[key]
                previous = relations[index]
                evidence: list[Evidence] = list(previous.evidence)
                for entry in relation.evidence:
                    if entry not in evidence:
                        evidence.append(entry)
                confidence = (
                    min(previous.confidence, relation.confidence)
                    if previous.confidence is not None
                    and relation.confidence is not None
                    else None
                )
                relations[index] = previous.model_copy(
                    update={"evidence": tuple(evidence), "confidence": confidence}
                )
            else:
                index = len(relations)
                relation_keys[key] = index
                relations.append(
                    relation.model_copy(
                        update=dict(
                            local_id=f"r{index}",
                            subject_id=subject,
                            object_id=object_id,
                        )
                    )
                )
    return Extraction(characters=tuple(characters), relations=tuple(relations))


async def extract_document(
    document: PreparedDocument | PreparedPdf,
    *,
    provider: ExtractionProvider,
    store: BlobStore,
    limits: Limits,
) -> tuple[ExtractionArtifact, Blob]:
    """One attempt, no retries. Publish only a complete validated artifact.

    The caller owns the revision lease and retry budget. All calls made here
    count against this attempt's llm_calls. Cancellation cancels in-flight calls.
    """
    prepared, inputs = await asyncio.to_thread(_inputs, document, store, limits)
    pending = iter(enumerate(inputs))
    records: list[CallRecord | None] = [None] * len(inputs)

    async def worker() -> None:
        for index, item in pending:
            response = await provider.generate_with_metadata(item.request, Extraction)
            _validate(response.result, item, limits)
            records[index] = CallRecord(
                fragment_id=item.chunk.fragment_id,
                page=item.chunk.page,
                requested_model=response.requested_model,
                model=response.model,
                usage=response.usage,
                extraction=response.result,
            )

    tasks: list[asyncio.Task[None]] = []
    try:
        async with asyncio.timeout(limits.processing_seconds):
            tasks = [
                asyncio.create_task(worker())
                for _ in range(min(limits.llm_concurrency, len(inputs)))
            ]
            await asyncio.gather(*tasks)
    except TimeoutError:
        raise LLMError("processing_timeout", retryable=True) from None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    completed = tuple(record for record in records if record is not None)
    result = _merge(inputs, completed)
    try:
        result.check_limits(limits)
    except ValueError:
        raise LLMError("limit_exceeded") from None
    artifact = ExtractionArtifact(
        revision_id=prepared.revision_id,
        preparation_version=prepared.version,
        extraction=result,
        calls=completed,
    )
    blob = await asyncio.to_thread(
        store.put_artifact, BytesIO(artifact.model_dump_json().encode("utf-8"))
    )
    return artifact, blob
