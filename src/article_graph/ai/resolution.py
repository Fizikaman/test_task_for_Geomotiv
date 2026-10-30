"""Conservative identity decisions; publication and leases belong to P13."""

import asyncio
import hashlib
import json
import unicodedata
from importlib.resources import files
from io import BytesIO
from typing import Literal, Protocol, Self
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from article_graph.ai.embeddings import EmbeddingMetadata, Embeddings
from article_graph.ai.extraction import ExtractionArtifact
from article_graph.ai.llm import LLM, GenerationRequest, TextBlock
from article_graph.db.schema import validate_embedding
from article_graph.documents.blob_store import Blob, BlobStore
from article_graph.domain import (
    DTO,
    Character,
    ContentHash,
    ExtractedCharacter,
    Relation,
    RelationType,
    Text,
)
from article_graph.settings import Limits

RESOLUTION_NAMESPACE = UUID("6c4f3bc8-07c7-4a23-ad26-e54d9037bba5")


class Candidate(DTO):
    character: Character
    score: float = Field(ge=0, le=1, allow_inf_nan=False)


class CandidateStore(Protocol):
    def search(
        self, vector: tuple[float, ...], metadata: EmbeddingMetadata, top_k: int
    ) -> tuple[Candidate, ...]: ...

    def current(self, character_id: UUID) -> Character | None: ...


class IdentityVerdict(DTO):
    decision: Literal["match", "no_match", "uncertain"]
    candidate_id: UUID | None
    # Exact quotations from both descriptions, required for a proposed match.
    source_context: Text | None
    candidate_context: Text | None
    reason: Text

    @model_validator(mode="after")
    def match_evidence(self) -> Self:
        if self.decision == "match":
            if (
                self.candidate_id is None
                or not self.source_context
                or not self.candidate_context
            ):
                raise ValueError("match requires candidate and context evidence")
        elif any(
            value is not None
            for value in (
                self.candidate_id,
                self.source_context,
                self.candidate_context,
            )
        ):
            raise ValueError("non-match cannot select a candidate")
        return self


class IdentityMapping(DTO):
    local_id: str
    character_id: UUID
    decision: Literal["match", "no_match", "uncertain"]
    reason: Literal[
        "alias_and_context",
        "llm_match",
        "llm_no_match",
        "llm_uncertain",
        "no_candidates",
        "insufficient_evidence",
        "candidate_changed",
    ]
    candidates: tuple[Candidate, ...]
    verdict: IdentityVerdict | None = None


class ResolvedCharacter(DTO):
    source: ExtractedCharacter
    character_id: UUID
    embedding: tuple[float, ...] = Field(repr=False)


class ResolutionArtifact(DTO):
    version: Literal["resolution-v1"] = "resolution-v1"
    prompt_version: Literal["identity-v1"] = "identity-v1"
    extraction_hash: ContentHash
    revision_id: UUID
    embedding_metadata: EmbeddingMetadata | None
    mappings: tuple[IdentityMapping, ...]
    characters: tuple[ResolvedCharacter, ...]
    relations: tuple[Relation, ...]


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _names(item: ExtractedCharacter | Character) -> set[str]:
    return {normalized(value) for value in (item.name, *item.aliases)}


def _shared_name(source: ExtractedCharacter, candidate: Character) -> bool:
    return bool(_names(source) & _names(candidate))


# All names below are property values, never interpolated relationship names.
# inverse forms swap endpoints before canonical symmetric UUID ordering.
_SYNONYMS: dict[str, tuple[str, bool, bool]] = {
    "FRIEND_OF": ("FRIEND_OF", False, False),
    "FRIENDS_WITH": ("FRIEND_OF", False, False),
    "PARENT_OF": ("PARENT_OF", True, False),
    "FATHER_OF": ("PARENT_OF", True, False),
    "MOTHER_OF": ("PARENT_OF", True, False),
    "CHILD_OF": ("PARENT_OF", True, True),
}


def normalize_relations(
    artifact: ExtractionArtifact,
    mapping: dict[str, UUID],
    article_id: UUID,
    extraction_hash: str,
    registered: tuple[RelationType, ...] = (),
) -> tuple[Relation, ...]:
    directions = {kind: directed for kind, directed, _ in _SYNONYMS.values()}
    for item in registered:
        if item.kind in directions and directions[item.kind] != item.directed:
            raise ValueError("relation direction conflict")
        directions[item.kind] = item.directed
    result: list[Relation] = []
    for item in artifact.extraction.relations:
        kind, directed, inverse = _SYNONYMS.get(
            item.kind, (item.kind, item.directed, False)
        )
        if directed != item.directed or (
            kind in directions and directions[kind] != directed
        ):
            raise ValueError("relation direction conflict")
        directions[kind] = directed
        subject, obj = mapping[item.subject_id], mapping[item.object_id]
        if inverse:
            subject, obj = obj, subject
        if not directed and str(subject) > str(obj):
            subject, obj = obj, subject
        result.append(
            Relation(
                relation_id=uuid5(
                    RESOLUTION_NAMESPACE, f"relation:{extraction_hash}:{item.local_id}"
                ),
                article_id=article_id,
                revision_id=artifact.revision_id,
                subject_id=subject,
                object_id=obj,
                kind=kind,
                directed=directed,
                evidence=item.evidence,
                confidence=item.confidence,
            )
        )
    return tuple(result)


async def resolve(
    artifact: ExtractionArtifact,
    *,
    article_id: UUID,
    embeddings: Embeddings,
    repository: CandidateStore,
    llm: LLM,
    store: BlobStore,
    limits: Limits,
    top_k: int = 10,
    registered_types: tuple[RelationType, ...] = (),
) -> tuple[ResolutionArtifact, Blob]:
    """No graph writes, retries or paid calls unless real adapters are supplied.

    The caller must reuse the saved artifact on retry (P14). New IDs depend only
    on extraction content and local ID, never on ANN order, score or LLM prose.
    """
    if not 1 <= top_k <= 100:
        raise ValueError("top_k must be between 1 and 100")
    artifact.extraction.check_limits(limits)
    extraction_hash = hashlib.sha256(artifact.model_dump_json().encode()).hexdigest()
    # Fail invalid relation definitions before any provider call.
    new_ids = {
        source.local_id: uuid5(
            RESOLUTION_NAMESPACE, f"character:{extraction_hash}:{source.local_id}"
        )
        for source in artifact.extraction.characters
    }
    normalize_relations(
        artifact, new_ids, article_id, extraction_hash, registered_types
    )
    mappings: list[IdentityMapping] = []
    characters: list[ResolvedCharacter] = []
    metadata: EmbeddingMetadata | None = None
    calls = 0
    total_input = 0
    async with asyncio.timeout(limits.processing_seconds):
        for source in artifact.extraction.characters:
            batch = await embeddings.embed([f"{source.name}\n{source.description}"])
            if len(batch.vectors) != 1:
                raise ValueError("embedding count mismatch")
            if metadata is not None and metadata != batch.metadata:
                raise ValueError("embedding space changed")
            metadata = batch.metadata
            vector = tuple(
                validate_embedding(list(batch.vectors[0]), metadata.dimensions)
            )
            candidates = await asyncio.to_thread(
                repository.search, vector, metadata, top_k
            )
            if len(candidates) > top_k or len(
                {c.character.character_id for c in candidates}
            ) != len(candidates):
                raise ValueError("invalid candidate set")
            target: Character | None = None
            verdict: IdentityVerdict | None = None
            reason: Literal[
                "alias_and_context",
                "llm_match",
                "llm_no_match",
                "llm_uncertain",
                "no_candidates",
                "insufficient_evidence",
                "candidate_changed",
            ] = "no_candidates"
            decision: Literal["match", "no_match", "uncertain"] = "no_match"
            exact = [
                c.character
                for c in candidates
                if _shared_name(source, c.character)
                and len(normalized(source.description)) >= 24
                and normalized(source.description)
                == normalized(c.character.description)
            ]
            # With multiple ANN candidates, even an exact candidate may have a
            # homonym. Delegate the whole ambiguous set; scores never decide.
            if len(candidates) == 1 and len(exact) == 1:
                target, reason, decision = exact[0], "alias_and_context", "match"
            elif candidates:
                calls += 1
                if calls > limits.llm_calls:
                    raise ValueError("identity call budget exceeded")
                payload = json.dumps(
                    {
                        "source": source.model_dump(mode="json"),
                        "candidates": [
                            c.character.model_dump(mode="json") for c in candidates
                        ],
                    },
                    ensure_ascii=False,
                )
                system = (
                    files("article_graph")
                    .joinpath("prompts/identity.v1.txt")
                    .read_text(encoding="utf-8")
                )
                size = len((system + payload).encode())
                total_input += size
                if (
                    size > limits.llm_input_bytes
                    or total_input > limits.llm_total_input_bytes
                ):
                    raise ValueError("identity input budget exceeded")
                async with asyncio.timeout(limits.provider_timeout_seconds):
                    raw = await llm.generate(
                        GenerationRequest(
                            system=system, content=(TextBlock(text=payload),)
                        ),
                        IdentityVerdict,
                    )
                verdict = IdentityVerdict.model_validate(raw.model_dump())
                decision = verdict.decision
                reason = "llm_no_match" if decision == "no_match" else "llm_uncertain"
                if decision == "match":
                    selected = next(
                        (
                            c.character
                            for c in candidates
                            if c.character.character_id == verdict.candidate_id
                        ),
                        None,
                    )
                    if (
                        len(exact) <= 1
                        and selected is not None
                        and _shared_name(source, selected)
                        and verdict.source_context is not None
                        and verdict.candidate_context is not None
                        and len(verdict.source_context) >= 12
                        and len(verdict.candidate_context) >= 12
                        and verdict.source_context in source.description
                        and verdict.candidate_context in selected.description
                    ):
                        target, reason = selected, "llm_match"
                    else:
                        reason, decision = "insufficient_evidence", "uncertain"
            if target is not None:
                current = await asyncio.to_thread(
                    repository.current, target.character_id
                )
                if current != target:
                    target, reason, decision = None, "candidate_changed", "uncertain"
            character_id = target.character_id if target else new_ids[source.local_id]
            mappings.append(
                IdentityMapping(
                    local_id=source.local_id,
                    character_id=character_id,
                    decision=decision,
                    reason=reason,
                    candidates=candidates,
                    verdict=verdict,
                )
            )
            characters.append(
                ResolvedCharacter(
                    source=source, character_id=character_id, embedding=vector
                )
            )
        result = ResolutionArtifact(
            extraction_hash=extraction_hash,
            revision_id=artifact.revision_id,
            embedding_metadata=metadata,
            mappings=tuple(mappings),
            characters=tuple(characters),
            relations=normalize_relations(
                artifact,
                {m.local_id: m.character_id for m in mappings},
                article_id,
                extraction_hash,
                registered_types,
            ),
        )
        blob = await asyncio.to_thread(
            store.put_artifact, BytesIO(result.model_dump_json().encode())
        )
        return result, blob
