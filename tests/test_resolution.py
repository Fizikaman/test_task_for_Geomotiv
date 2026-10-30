import asyncio
import json
import os
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from time import monotonic, sleep
from typing import override
from uuid import UUID, uuid4
lazy from subprocess import run

import httpx
import pytest
from fastapi import status as http_status
from pydantic import ValidationError
lazy from neo4j import Driver

from article_graph.blob_store import BlobNamespace, LocalBlobStore
from article_graph.candidate_repository import Neo4jCandidates
from article_graph.domain import (
    Character,
    ExtractedCharacter,
    ExtractedRelation,
    Extraction,
    RelationType,
    TextEvidence,
)
from article_graph.embeddings import (
    EmbeddingBatch,
    EmbeddingMetadata,
    bind_embedding_space,
)
from article_graph.extraction import ExtractionArtifact
from article_graph.llm import ChatCompletionsLLM, LLMError
from article_graph.resolution import (
    Candidate,
    CandidateStore,
    IdentityVerdict,
    normalize_relations,
    resolve,
)
from article_graph.settings import Limits, LLMSettings, Neo4jSettings

CONTEXT = "Captain of the Aurora expedition in 1912."
OTHER_CONTEXT = "Violinist in the Berlin orchestra in 1985."
META = EmbeddingMetadata(model="test:latest", digest="c" * 64, dimensions=1024)
VECTOR = (1.0,) + (0.0,) * 1023
ARTICLE = UUID("00000000-0000-0000-0000-000000000012")
REVISION = UUID("00000000-0000-0000-0000-000000000013")


def source(
    name: str = "Alex", description: str = CONTEXT, local_id: str = "a"
) -> ExtractedCharacter:
    return ExtractedCharacter(local_id=local_id, name=name, description=description)


def candidate(
    name: str = "Alexander",
    description: str = CONTEXT,
    aliases: tuple[str, ...] = ("Alex",),
) -> Candidate:
    return Candidate(
        character=Character(
            character_id=uuid4(), name=name, description=description, aliases=aliases
        ),
        score=1.0,
    )


def artifact(
    characters: tuple[ExtractedCharacter, ...] | None = None,
    relations: tuple[ExtractedRelation, ...] = (),
) -> ExtractionArtifact:
    return ExtractionArtifact(
        revision_id=REVISION,
        preparation_version="text-v1",
        extraction=Extraction(
            characters=characters if characters is not None else (source(),),
            relations=relations,
        ),
        calls=(),
    )


class FakeEmbeddings:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.inputs.extend(texts)
        return EmbeddingBatch(metadata=META, vectors=tuple(VECTOR for _ in texts))


class FakeCandidates:
    def __init__(self, candidates: tuple[Candidate, ...] = ()) -> None:
        self.candidates = candidates
        self.changed = False

    def search(
        self, vector: tuple[float, ...], metadata: EmbeddingMetadata, top_k: int
    ) -> tuple[Candidate, ...]:
        assert vector == VECTOR and metadata == META
        return self.candidates[:top_k]

    def current(self, character_id: UUID) -> Character | None:
        if self.changed:
            return None
        return next(
            (
                c.character
                for c in self.candidates
                if c.character.character_id == character_id
            ),
            None,
        )


class Harness:
    def __init__(self, tmp_path: Path, settings: LLMSettings) -> None:
        self.store = LocalBlobStore(tmp_path, max_bytes=4_000_000)
        self.settings = settings
        self.embeddings = FakeEmbeddings()
        self.requests: list[dict[str, object]] = []
        self.reply: dict[str, object] = {
            "decision": "uncertain",
            "candidate_id": None,
            "source_context": None,
            "candidate_context": None,
            "reason": "Ambiguous context",
        }
        self.limits = Limits()

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = json.loads(request.content)
        self.requests.append(payload)
        return httpx.Response(
            http_status.HTTP_200_OK,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(self.reply),
                        },
                    }
                ]
            },
        )

    def execute(
        self, repository: CandidateStore, extraction: ExtractionArtifact | None = None
    ):
        async def scenario():
            llm = ChatCompletionsLLM(
                self.settings, self.limits, transport=httpx.MockTransport(self.handler)
            )
            try:
                return await resolve(
                    extraction or artifact(),
                    article_id=ARTICLE,
                    embeddings=self.embeddings,
                    repository=repository,
                    llm=llm,
                    store=self.store,
                    limits=self.limits,
                )
            finally:
                await llm.aclose()

        return asyncio.run(scenario())


@pytest.fixture
def harness(tmp_path: Path, llm_settings: LLMSettings) -> Harness:
    return Harness(tmp_path, llm_settings)


def test_alias_and_full_context_without_llm(harness: Harness) -> None:
    target = candidate()
    result, blob = harness.execute(FakeCandidates((target,)))
    assert result.mappings[0].character_id == target.character.character_id
    assert result.mappings[0].reason == "alias_and_context"
    assert harness.embeddings.inputs == ["Alex\n" + CONTEXT]
    assert not harness.requests
    with harness.store.open(BlobNamespace.ARTIFACTS, blob.content_hash) as stream:
        assert json.load(stream)["mappings"][0]["reason"] == "alias_and_context"


@pytest.mark.parametrize(
    "name,description", [("Alex", OTHER_CONTEXT), ("Alex Smith", OTHER_CONTEXT)]
)
def test_homonyms_and_shared_surnames_do_not_merge(
    harness: Harness, name: str, description: str
) -> None:
    target = candidate(name=name, description=description, aliases=())
    harness.reply["decision"] = "no_match"
    result, _ = harness.execute(FakeCandidates((target,)))
    assert result.mappings[0].character_id != target.character.character_id
    assert result.mappings[0].reason == "llm_no_match"
    assert len(harness.requests) == 1


def test_uncertain_and_empty_candidates_have_stable_distinct_ids(
    harness: Harness,
) -> None:
    target = candidate(description=OTHER_CONTEXT)
    extraction = artifact((source(), source(local_id="b")))
    first, blob = harness.execute(FakeCandidates((target,)), extraction)
    again, same_blob = harness.execute(FakeCandidates((target,)), extraction)
    absent, _ = harness.execute(FakeCandidates(), extraction)
    assert first == again and blob == same_blob
    assert first.mappings[0].reason == "llm_uncertain"
    ids = [m.character_id for m in first.mappings]
    assert len(set(ids)) == 2 and target.character.character_id not in ids
    assert ids == [m.character_id for m in absent.mappings]
    assert all(m.reason == "no_candidates" for m in absent.mappings)


def test_multiple_exact_candidates_require_verification(harness: Harness) -> None:
    targets = (candidate(), candidate())
    result, _ = harness.execute(FakeCandidates(targets))
    assert result.mappings[0].decision == "uncertain"
    assert result.mappings[0].character_id not in {
        c.character.character_id for c in targets
    }
    assert len(harness.requests) == 1


def match_reply(harness: Harness, target: Candidate) -> None:
    harness.reply = {
        "decision": "match",
        "candidate_id": str(target.character.character_id),
        "source_context": CONTEXT,
        "candidate_context": target.character.description,
        "reason": "Alias and specific expedition biography agree",
    }


def test_llm_match_uses_selected_candidate_and_context(harness: Harness) -> None:
    target = candidate(
        description="Captain of the Aurora expedition in 1912. Also called Alex."
    )
    match_reply(harness, target)
    result, _ = harness.execute(FakeCandidates((target,)))
    assert result.mappings[0].character_id == target.character.character_id
    assert result.mappings[0].reason == "llm_match"
    assert result.mappings[0].verdict is not None


@pytest.mark.parametrize(
    "problem", ["unknown_id", "invented_quote", "name_only", "short_quote"]
)
def test_llm_match_requires_grounded_evidence(harness: Harness, problem: str) -> None:
    target = candidate(description=CONTEXT + " Also called Alex.")
    if problem == "name_only":
        target = candidate(
            name="Unknown", aliases=(), description=target.character.description
        )
    match_reply(harness, target)
    if problem == "unknown_id":
        harness.reply["candidate_id"] = str(uuid4())
    if problem == "invented_quote":
        harness.reply["candidate_context"] = "An invented context quotation"
    if problem == "short_quote":
        harness.reply["source_context"] = "Captain"
    result, _ = harness.execute(FakeCandidates((target,)))
    assert result.mappings[0].reason == "insufficient_evidence"
    assert result.mappings[0].decision == "uncertain"
    assert result.mappings[0].character_id != target.character.character_id


def test_short_generic_context_requires_llm(harness: Harness) -> None:
    result, _ = harness.execute(
        FakeCandidates((candidate(description="A person"),)),
        artifact((source(description="A person"),)),
    )
    assert result.mappings[0].decision == "uncertain" and harness.requests


def test_candidate_disappears_after_decision(harness: Harness) -> None:
    repository = FakeCandidates((candidate(),))
    repository.changed = True
    result, _ = harness.execute(repository)
    assert result.mappings[0].reason == "candidate_changed"
    assert result.mappings[0].decision == "uncertain"


@pytest.mark.parametrize(
    "reply",
    [
        {"decision": "yes"},
        {
            "decision": "match",
            "candidate_id": None,
            "source_context": None,
            "candidate_context": None,
            "reason": "bad",
        },
        {
            "decision": "no_match",
            "candidate_id": str(uuid4()),
            "source_context": None,
            "candidate_context": None,
            "reason": "bad",
        },
    ],
)
def test_provider_verdict_validation(
    harness: Harness, reply: dict[str, object]
) -> None:
    harness.reply = reply
    with pytest.raises(LLMError, match="invalid"):
        harness.execute(FakeCandidates((candidate(description=OTHER_CONTEXT),)))


@pytest.mark.parametrize(
    "field",
    ["decision", "candidate_id", "source_context", "candidate_context", "reason"],
)
def test_verdict_fields_required(field: str) -> None:
    data: dict[str, object] = {
        "decision": "uncertain",
        "candidate_id": None,
        "source_context": None,
        "candidate_context": None,
        "reason": "Unclear",
    }
    del data[field]
    with pytest.raises(ValidationError):
        IdentityVerdict.model_validate(data)


def test_identity_budget_rejects_whole_artifact(harness: Harness) -> None:
    harness.limits = Limits(llm_calls=1)
    with pytest.raises(ValueError, match="call budget"):
        harness.execute(
            FakeCandidates((candidate(description=OTHER_CONTEXT),)),
            artifact((source(), source(local_id="b"))),
        )
    assert len(harness.requests) == 1
    assert not tuple((harness.store._root / "artifacts").iterdir())


def test_empty_extraction_no_provider_calls(harness: Harness) -> None:
    result, _ = harness.execute(FakeCandidates(), artifact(()))
    assert result.characters == result.relations == result.mappings == ()
    assert not harness.requests and not harness.embeddings.inputs


def relation(kind: str, directed: bool, local_id: str = "r") -> ExtractedRelation:
    return ExtractedRelation(
        local_id=local_id,
        subject_id="a",
        object_id="b",
        kind=kind,
        directed=directed,
        polarity="affirmed",
        evidence=(TextEvidence(fragment_id="f", quote="They are related."),),
    )


@pytest.mark.parametrize(
    "kind,directed,expected,inverse",
    [
        ("FRIENDS_WITH", False, "FRIEND_OF", False),
        ("CHILD_OF", True, "PARENT_OF", True),
        ("FATHER_OF", True, "PARENT_OF", False),
        ("COLLEAGUE_OF", False, "COLLEAGUE_OF", False),
    ],
)
def test_relation_normalization(
    kind: str, directed: bool, expected: str, inverse: bool
) -> None:
    first, second = UUID(int=2), UUID(int=1)
    extraction = artifact((source(), source(local_id="b")), (relation(kind, directed),))
    output = normalize_relations(
        extraction, {"a": first, "b": second}, ARTICLE, "d" * 64
    )
    item = output[0]
    assert item.kind == expected and item.directed == directed
    assert (item.subject_id, item.object_id) == (
        (second, first) if inverse or not directed else (first, second)
    )
    assert item.evidence == extraction.extraction.relations[0].evidence
    assert output == normalize_relations(
        extraction, {"a": first, "b": second}, ARTICLE, "d" * 64
    )


def test_direction_conflicts_and_cypher_injection_rejected() -> None:
    with pytest.raises(ValidationError):
        relation("X`) DELETE n //", True)
    for relations, registry in [
        ((relation("FRIENDS_WITH", True),), ()),
        ((relation("CUSTOM", True), relation("CUSTOM", False, "r2")), ()),
        ((relation("CUSTOM", True),), (RelationType(kind="CUSTOM", directed=False),)),
    ]:
        with pytest.raises(ValueError, match="direction conflict"):
            normalize_relations(
                artifact((source(), source(local_id="b")), relations),
                {"a": uuid4(), "b": uuid4()},
                ARTICLE,
                "d" * 64,
                registry,
            )


def test_lazy_runtime_imports() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "import sys\nimport article_graph.resolution\nimport article_graph.candidate_repository\nassert 'neo4j' not in sys.modules\nassert 'httpx' not in sys.modules\nassert 'pypdfium2' not in sys.modules",
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def real_repository(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings
) -> Iterator[Neo4jCandidates]:
    bind_embedding_space(neo4j_driver, neo4j_settings, META)
    try:
        yield Neo4jCandidates(neo4j_driver, neo4j_settings)
    finally:
        with neo4j_driver.session(database=neo4j_settings.database) as session:
            session.run("MATCH (n:_GeomotivP12Test) DETACH DELETE n").consume()
            session.run(
                "MATCH (s:EmbeddingSpace {index_name: 'character_embedding', digest: $digest}) DELETE s",
                digest=META.digest,
            ).consume()


def insert_candidate(
    driver: Driver, settings: Neo4jSettings, target: Candidate
) -> None:
    with driver.session(database=settings.database) as session:
        session.run(
            "CREATE (c:Character:_GeomotivP12Test) SET c += $profile, "
            "c.embedding = $vector, c.embedding_digest = $digest",
            profile=target.character.model_dump(mode="json"),
            vector=list(VECTOR),
            digest=META.digest,
        ).consume()


def await_candidate(repository: Neo4jCandidates, target: Candidate) -> None:
    deadline = monotonic() + 15
    while True:
        candidates = repository.search(VECTOR, META, 10)
        if target.character.character_id in {
            c.character.character_id for c in candidates
        }:
            return
        assert monotonic() < deadline, "ANN did not expose committed test candidate"
        sleep(0.1)


@pytest.mark.integration
def test_real_vector_resolution_and_replay(
    harness: Harness,
    real_repository: Neo4jCandidates,
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
) -> None:
    empty, _ = harness.execute(real_repository)
    assert empty.mappings[0].reason == "no_candidates"
    target = candidate()
    insert_candidate(neo4j_driver, neo4j_settings, target)
    await_candidate(real_repository, target)
    first, blob = harness.execute(real_repository)
    second, _ = harness.execute(real_repository)
    assert first.mappings == second.mappings
    assert first.mappings[0].character_id == target.character.character_id
    assert blob.size > 0 and not harness.requests
    with pytest.raises(ValueError, match="space mismatch"):
        real_repository.search(VECTOR, META.model_copy(update={"digest": "d" * 64}), 10)


@pytest.mark.integration
def test_stale_ann_embedding_does_not_merge(
    harness: Harness,
    real_repository: Neo4jCandidates,
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
) -> None:
    target = candidate()
    insert_candidate(neo4j_driver, neo4j_settings, target)
    await_candidate(real_repository, target)
    # The vector remains identical (score 1); the CURRENT biography contradicts
    # the old profile. Candidate query must read it, not trust ANN similarity.
    with neo4j_driver.session(database=neo4j_settings.database) as session:
        session.run(
            "MATCH (c:Character {character_id: $id}) SET c.description = $context",
            id=str(target.character.character_id),
            context=OTHER_CONTEXT,
        ).consume()
    harness.reply["decision"] = "no_match"
    output, _ = harness.execute(real_repository)
    assert output.mappings[0].character_id != target.character.character_id
    assert output.mappings[0].reason == "llm_no_match"
    assert output.mappings[0].candidates[0].character.description == OTHER_CONTEXT
    assert harness.requests


def test_multiple_indistinguishable_candidates_reject_arbitrary_llm_match(
    harness: Harness,
) -> None:
    targets = (candidate(), candidate())
    match_reply(harness, targets[0])
    output, _ = harness.execute(FakeCandidates(targets))
    assert output.mappings[0].decision == "uncertain"
    assert output.mappings[0].reason == "insufficient_evidence"


def test_changed_profile_rejects_exact_alias_match(harness: Harness) -> None:
    class ChangedProfile(FakeCandidates):
        @override
        def current(self, character_id: UUID) -> Character | None:
            previous = super().current(character_id)
            assert previous is not None
            return previous.model_copy(update={"description": OTHER_CONTEXT})

    target = candidate()
    result, _ = harness.execute(ChangedProfile((target,)))
    assert result.mappings[0].decision == "uncertain"
    assert result.mappings[0].reason == "candidate_changed"


def test_relation_conflict_fails_before_provider_calls(harness: Harness) -> None:
    extraction = artifact(
        (source(), source(local_id="b")), (relation("FRIENDS_WITH", True),)
    )
    with pytest.raises(ValueError, match="direction conflict"):
        harness.execute(FakeCandidates((candidate(),)), extraction)
    assert not harness.requests and not harness.embeddings.inputs


@pytest.mark.parametrize("budget", ["llm_input_bytes", "llm_total_input_bytes"])
def test_input_budgets(harness: Harness, budget: str) -> None:
    harness.limits = Limits.model_validate({budget: 1})
    with pytest.raises(ValueError, match="input budget"):
        harness.execute(FakeCandidates((candidate(description=OTHER_CONTEXT),)))
    assert not harness.requests


@pytest.mark.integration
def test_profile_changed_during_real_llm_verification(
    harness: Harness,
    real_repository: Neo4jCandidates,
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = candidate(description=CONTEXT + " Also called Alex.")
    insert_candidate(neo4j_driver, neo4j_settings, target)
    await_candidate(real_repository, target)
    match_reply(harness, target)
    original = harness.handler

    def mutate_profile(request: httpx.Request) -> httpx.Response:
        with neo4j_driver.session(database=neo4j_settings.database) as session:
            session.run(
                "MATCH (c:Character {character_id: $id}) SET c.description = $context",
                id=str(target.character.character_id),
                context=OTHER_CONTEXT,
            ).consume()
        return original(request)

    monkeypatch.setattr(harness, "handler", mutate_profile)
    output, _ = harness.execute(real_repository)
    assert output.mappings[0].decision == "uncertain"
    assert output.mappings[0].reason == "candidate_changed"
    assert output.mappings[0].character_id != target.character.character_id
