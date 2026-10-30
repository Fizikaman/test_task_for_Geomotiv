from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from article_graph.domain import (
    Article,
    Character,
    ExtractedRelation,
    Extraction,
    Job,
    Page,
    PageRequest,
    ProcessingResponse,
    Relation,
    Revision,
    SafeError,
    Status,
)
from article_graph.settings import Limits, Settings

ARTICLE = UUID(int=1)
REVISION = UUID(int=2)
JOB = UUID(int=3)


def character(local_id: str = "alice") -> dict[str, object]:
    return {"local_id": local_id, "name": "Alice", "description": "A researcher"}


def relation(**changes: object) -> dict[str, object]:
    return {
        "local_id": "r1",
        "subject_id": "alice",
        "object_id": "bob",
        "kind": "PARENT_OF",
        "directed": True,
        "polarity": "affirmed",
        "evidence": [
            {"kind": "text", "fragment_id": "chunk-1", "quote": "Alice is Bob's parent"}
        ],
        **changes,
    }


def extraction(*relations: dict[str, object]) -> Extraction:
    return Extraction.model_validate(
        {"characters": [character(), character("bob")], "relations": relations}
    )


def test_valid_extraction_and_public_provenance_roundtrip() -> None:
    result = extraction(
        relation(),
        relation(
            local_id="r2",
            kind="FRIEND_OF",
            directed=False,
            confidence=0.8,
            evidence=[
                {
                    "kind": "visual",
                    "fragment_id": "page-1",
                    "page": 1,
                    "description": "Friendship arrow between Alice and Bob",
                }
            ],
        ),
    ).check_limits(Limits())
    assert Extraction.model_validate_json(result.model_dump_json()) == result
    public = Relation(
        relation_id=UUID(int=4),
        article_id=ARTICLE,
        revision_id=REVISION,
        subject_id=UUID(int=5),
        object_id=UUID(int=6),
        kind="PARENT_OF",
        directed=True,
        evidence=result.relations[0].evidence,
    )
    assert Relation.model_validate_json(public.model_dump_json()) == public
    assert "discriminator" in str(Extraction.model_json_schema())


@pytest.mark.parametrize("endpoint", ["subject_id", "object_id"])
def test_dangling_references(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="unknown character"):
        extraction(relation(**{endpoint: "missing"}))


@pytest.mark.parametrize("collection", ["characters", "relations"])
def test_duplicate_local_ids(collection: str) -> None:
    payload: dict[str, object] = {
        "characters": [character(), character("bob")],
        "relations": [relation()],
    }
    payload[collection] = (
        [character(), character()]
        if collection == "characters"
        else [relation(), relation()]
    )
    with pytest.raises(ValidationError, match="duplicate"):
        Extraction.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence": []},
        {"evidence": [{"kind": "text", "fragment_id": "c", "quote": "  "}]},
        {"evidence": [{"kind": "text", "fragment_id": " ", "quote": "citation"}]},
        {"evidence": [{"kind": "text", "fragment_id": "c", "quote": "x", "page": 0}]},
        {"evidence": [{"kind": "visual", "fragment_id": "p", "description": "x"}]},
        {"evidence": [{"kind": "visual", "fragment_id": "p", "page": 1, "quote": "x"}]},
        {
            "evidence": [
                {"kind": "visual", "fragment_id": "p", "page": 1, "description": " "}
            ]
        },
        {"confidence": -0.01},
        {"confidence": 1.01},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"kind": "FRIEND_OF", "directed": True},
        {"kind": "PARENT_OF", "directed": False},
        {"kind": "parent of"},
        {"local_id": ""},
        {"local_id": "a b"},
        {"unexpected": "field"},
    ],
)
def test_invalid_relation(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        extraction(relation(**changes))


@pytest.mark.parametrize("confidence", [0.0, 1.0, None])
def test_confidence_boundaries(confidence: float | None) -> None:
    assert (
        extraction(relation(confidence=confidence)).relations[0].confidence
        == confidence
    )


@pytest.mark.parametrize("polarity", ["negated", "uncertain", "unknown", None])
def test_nonaffirmative_statement_cannot_become_relation(polarity: str | None) -> None:
    with pytest.raises(ValidationError):
        extraction(relation(polarity=polarity, confidence=1.0))


def test_unknown_or_negated_relations_are_omitted_not_inferred() -> None:
    # A provider seeing only "Alice is not Bob's parent" or "possibly" must
    # return no assertion. DTO validation cannot itself understand source text.
    assert extraction().relations == ()
    assert Extraction(characters=(), relations=()).characters == ()
    payload = relation()
    del payload["polarity"]
    with pytest.raises(ValidationError):
        ExtractedRelation.model_validate(payload)


@pytest.mark.parametrize("field", ["local_id", "name", "description"])
def test_character_requires_nonblank_identity_and_context(field: str) -> None:
    item = character()
    item[field] = " "
    with pytest.raises(ValidationError):
        Extraction.model_validate({"characters": [item], "relations": []})


def test_local_ids_are_scoped_by_collection_and_names_are_not_unique() -> None:
    assert extraction(relation(local_id="alice")).relations[0].local_id == "alice"


@pytest.mark.parametrize(
    "limits",
    [
        Limits(characters=1),
        Limits(relations=1),
        Limits(evidence_per_relation=1),
    ],
)
def test_configured_extraction_budgets(limits: Limits) -> None:
    item = relation()
    result = extraction(
        relation(
            evidence=[
                {"kind": "text", "fragment_id": "c1", "quote": "x"},
                {"kind": "text", "fragment_id": "c2", "quote": "y"},
            ]
        ),
        {**item, "local_id": "r2"},
    )
    with pytest.raises(ValueError, match="limit exceeded"):
        result.check_limits(limits)
    assert (
        result.check_limits(Limits(characters=2, relations=2, evidence_per_relation=2))
        is result
    )


def test_failed_update_keeps_previous_active_revision() -> None:
    error = SafeError(code="extraction_invalid", message="Invalid extraction")
    article = Article(
        article_id=ARTICLE,
        latest_revision_id=REVISION,
        active_revision_id=UUID(int=9),
        job_id=JOB,
        status="failed",
        error=error,
    )
    assert article.active_revision_id != article.latest_revision_id
    job = Job(
        article_id=ARTICLE,
        revision_id=REVISION,
        job_id=JOB,
        status=Status.FAILED,
        error=error,
    )
    assert Job.model_validate_json(job.model_dump_json()) == job
    with pytest.raises(ValidationError):
        Job(article_id=ARTICLE, revision_id=REVISION, job_id=JOB, status=Status.FAILED)
    with pytest.raises(ValidationError):
        Article.model_validate({**article.model_dump(), "status": "processing"})
    with pytest.raises(ValidationError):
        Article.model_validate(
            {**article.model_dump(), "status": "completed", "error": None}
        )
    with pytest.raises(ValidationError):
        Article.model_validate(
            {**article.model_dump(), "status": "superseded", "error": None}
        )


def test_revision_and_processing_response_validation() -> None:
    revision = Revision(
        article_id=ARTICLE, revision_id=REVISION, number=1, content_hash="a" * 64
    )
    for changes in (
        {"number": 0},
        {"content_hash": "not-a-hash"},
        {"article_id": "not-a-uuid"},
    ):
        with pytest.raises(ValidationError):
            Revision.model_validate({**revision.model_dump(), **changes})
    response = ProcessingResponse(
        article_id=ARTICLE,
        revision_id=REVISION,
        job_id=JOB,
        status="queued",
        status_url=f"/articles/{ARTICLE}",
    )
    assert not response.unchanged
    with pytest.raises(ValidationError):
        ProcessingResponse.model_validate(
            {**response.model_dump(), "status_url": f"/articles/{REVISION}"}
        )


def test_pagination_bounds_and_serialization() -> None:
    limits = Limits(page_default=2, page_max=3)
    assert PageRequest(limit=3).check_limits(limits).offset == 0
    with pytest.raises(ValueError):
        PageRequest(limit=4).check_limits(limits)
    for payload in ({"limit": 0}, {"limit": 1, "offset": -1}):
        with pytest.raises(ValidationError):
            PageRequest.model_validate(payload)
    page = Page[Character](items=(), limit=2, offset=0)
    assert page.model_dump(mode="json") == {
        "items": [],
        "limit": 2,
        "offset": 0,
        "next_offset": None,
    }


def test_limit_settings_override_and_invalid_budgets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARTICLE_GRAPH_APP_TOKEN", "test-token-" * 4)
    monkeypatch.setenv("ARTICLE_GRAPH_JWT_ACCESS_KEY", "test-access-key-" * 4)
    monkeypatch.setenv("ARTICLE_GRAPH_JWT_REFRESH_KEY", "test-refresh-key-" * 4)
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__API_KEY", "test-key-only")
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__TEXT_MODEL", "test-text-model")
    monkeypatch.setenv("ARTICLE_GRAPH_LLM__OUTPUT_MODE", "json_object")
    monkeypatch.setenv("ARTICLE_GRAPH_LIMITS__CHARACTERS", "7")
    assert Settings().limits.characters == 7
    for field in Limits.model_fields:
        if field == "chunk_overlap_chars":
            assert Limits(chunk_overlap_chars=0).chunk_overlap_chars == 0
            continue
        with pytest.raises(ValidationError):
            Limits.model_validate({field: 0})
    with pytest.raises(ValidationError):
        Limits(page_default=3, page_max=2)
