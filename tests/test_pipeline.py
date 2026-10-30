import asyncio
import json
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from subprocess import run
from time import time
from typing import Literal, LiteralString, override
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
lazy from neo4j import Driver
lazy from neo4j.exceptions import ServiceUnavailable

from article_graph.article_repository import ArticleRepository
from article_graph.blob_store import BlobNamespace, LocalBlobStore
from article_graph.candidate_repository import Neo4jCandidates
from article_graph.domain import Character, ErrorCode, Extraction
from article_graph.embeddings import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingMetadata,
    bind_embedding_space,
)
from article_graph.llm import GenerationRequest, GenerationResult, ImageBlock, LLMError
from article_graph.pipeline import RevisionPipeline, Versions
from article_graph.pipeline_checkpoints import (
    Attempt,
    CheckpointRepository,
    PipelineError,
    Pointer,
    Stage,
)
from article_graph.publication import GraphPublisher, Publication, PublicationResult
from article_graph.registration import UploadRequest
from article_graph.resolution import Candidate
from article_graph.settings import Limits, LLMSettings, Neo4jSettings, OllamaSettings

META = EmbeddingMetadata(model="pipeline-test:v1", digest="f" * 64, dimensions=1024)
VECTOR = (1.0,) + (0.0,) * 1023


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.failure: LLMError | None = None
        self.before_response: Callable[[], None] | None = None
        self.images = 0
        self.delay = 0.0
        self.wait_on_response: asyncio.Event | None = None

    async def generate_with_metadata[T: BaseModel](
        self, request: GenerationRequest, response_model: type[T]
    ) -> GenerationResult[T]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        if self.before_response:
            self.before_response()
        if self.wait_on_response is not None:
            await self.wait_on_response.wait()
        self.images += sum(isinstance(b, ImageBlock) for b in request.content)
        if response_model is Extraction:
            block = request.content[0]
            assert not isinstance(block, ImageBlock)
            payload = json.loads(block.text.rsplit("\n", 1)[-1])
            result: dict[str, object] = {
                "characters": [
                    {"local_id": name, "name": name, "description": "Test context."}
                    for name in ("Alice", "Bob")
                ],
                "relations": [
                    {
                        "local_id": "friend",
                        "subject_id": "Alice",
                        "object_id": "Bob",
                        "kind": "FRIEND_OF",
                        "directed": False,
                        "polarity": "affirmed",
                        "evidence": [
                            {
                                "kind": "text",
                                "fragment_id": payload["fragment_id"],
                                "quote": "Alice met Bob.",
                                "page": payload["page"],
                            }
                        ],
                    }
                ],
            }
        else:
            result = {
                "decision": "no_match",
                "candidate_id": None,
                "source_context": None,
                "candidate_context": None,
                "reason": "Independent people.",
            }
        return GenerationResult(
            result=response_model.model_validate(result), requested_model="fake-text"
        )

    async def generate[T: BaseModel](
        self, request: GenerationRequest, response_model: type[T]
    ) -> T:
        return (await self.generate_with_metadata(request, response_model)).result

    async def extract(self, content: Sequence[object]) -> Extraction:
        raise AssertionError("Pipeline uses metadata extraction")


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls = 0
        self.failure: EmbeddingError | None = None

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.calls += 1
        if self.failure:
            raise self.failure
        return EmbeddingBatch(metadata=META, vectors=tuple(VECTOR for _ in texts))


class NoCandidates:
    def search(
        self, vector: tuple[float, ...], metadata: EmbeddingMetadata, top_k: int
    ) -> tuple[Candidate, ...]:
        return ()

    def current(self, character_id: UUID) -> Character | None:
        return None


class Publisher(GraphPublisher):
    fail = False
    before_publish: Callable[[], None] | None = None

    @override
    def publish(self, request: Publication) -> PublicationResult:
        if self.fail:
            raise ServiceUnavailable("test failure")
        if self.before_publish:
            self.before_publish()
        return super().publish(request)


class Harness:
    def __init__(
        self,
        driver: Driver,
        settings: Neo4jSettings,
        root: Path,
        llm_settings: LLMSettings,
    ) -> None:
        self.driver = driver
        self.settings = settings
        self.store = LocalBlobStore(root, max_bytes=64_000_000)
        self.llm = FakeLLM()
        self.embeddings = FakeEmbeddings()
        self.checkpoints = CheckpointRepository(driver, settings)
        self.publisher = Publisher(driver, settings, Limits())
        self.articles: list[str] = []
        self.llm_settings = llm_settings
        self.embedding_settings = OllamaSettings(
            base_url="http://ollama.test",
            model=META.model,
            digest=META.digest,
            dimensions=META.dimensions,
        )
        self.pipeline = self.make_pipeline()

    def make_pipeline(self) -> RevisionPipeline:
        return RevisionPipeline(
            checkpoints=self.checkpoints,
            publisher=self.publisher,
            store=self.store,
            llm=self.llm,
            embeddings=self.embeddings,
            candidates=NoCandidates(),
            llm_settings=self.llm_settings,
            embedding_settings=self.embedding_settings,
            limits=Limits(),
        )

    def query(self, query: LiteralString, **params: object) -> list[dict[str, object]]:
        with self.driver.session(database=self.settings.database) as session:
            return [dict(row.data()) for row in session.run(query, parameters=params)]

    def register(
        self,
        article_id: UUID | None = None,
        *,
        data: bytes | None = None,
        content_type: Literal["text/markdown", "application/pdf"] = "text/markdown",
    ) -> Attempt:
        blob = self.store.put_source(
            BytesIO(data if data is not None else f"Alice met Bob. {uuid4()}".encode()),
            content_type=content_type,
        )
        assert blob.format is not None
        registered = (
            ArticleRepository(self.driver, self.settings)
            .register(
                UploadRequest(
                    app_id="p14-test",
                    article_id=article_id,
                    method="file" if content_type == "application/pdf" else "text",
                    content_type=content_type,
                    format=blob.format,
                    content_hash=blob.content_hash,
                    size=blob.size,
                )
            )
            .body
        )
        self.articles.append(str(registered.article_id))
        attempt = Attempt(
            article_id=registered.article_id,
            revision_id=registered.revision_id,
            job_id=registered.job_id,
            attempt=1,
            attempt_token=uuid4(),
        )
        self.claim(attempt)
        return attempt

    def claim(self, attempt: Attempt) -> None:
        # P15 will implement claim/heartbeat; only the fixture simulates them.
        self.query(
            "MATCH (j:Job {job_id: $job}) SET j.status = 'processing', "
            "j.attempt = $attempt, j.attempt_token = $token, j.lease_expires_at = $expires",
            job=str(attempt.job_id),
            attempt=attempt.attempt,
            token=str(attempt.attempt_token),
            expires=time() + 180,
        )

    def retry(self, attempt: Attempt) -> Attempt:
        result = attempt.model_copy(
            update={
                "attempt": attempt.attempt + 1,
                "attempt_token": uuid4(),
            }
        )
        self.claim(result)
        return result

    def execute(self, attempt: Attempt) -> PublicationResult:
        return asyncio.run(self.pipeline.run(attempt))

    def state(self, attempt: Attempt) -> dict[str, object]:
        return self.query(
            "MATCH (a:Article {article_id: $article}), "
            "(r:ArticleRevision {revision_id: $revision}), (j:Job {job_id: $job}) "
            "RETURN a.active_revision_id AS active, j.status AS status, "
            "r.pipeline_checkpoints AS checkpoints, "
            "COUNT { (r)-[:ASSERTS]->() } AS assertions",
            article=str(attempt.article_id),
            revision=str(attempt.revision_id),
            job=str(attempt.job_id),
        )[0]


@pytest.fixture
def h(
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
    tmp_path: Path,
    llm_settings: LLMSettings,
) -> Iterator[Harness]:
    bind_embedding_space(neo4j_driver, neo4j_settings, META)
    harness = Harness(neo4j_driver, neo4j_settings, tmp_path, llm_settings)
    try:
        yield harness
    finally:
        harness.query(
            "MATCH (n) WHERE n.article_id IN $ids "
            "OR (n:Outbox AND EXISTS { MATCH (j:Job)-[:HAS_OUTBOX]->(n) "
            "WHERE j.article_id IN $ids }) DETACH DELETE n",
            ids=harness.articles,
        )
        harness.query(
            "MATCH (c:Character {embedding_digest: $digest}) "
            "WHERE NOT EXISTS { MATCH ()-[:MENTIONS]->(c) } DETACH DELETE c",
            digest=META.digest,
        )
        harness.query(
            "MATCH (s:EmbeddingSpace {digest: $digest}) DELETE s", digest=META.digest
        )
        harness.query(
            "MATCH (t:RelationType {kind: 'FRIEND_OF'}) "
            "WHERE NOT EXISTS { MATCH (r:RelationAssertion) WHERE r.kind = t.kind } DELETE t"
        )


def test_pipeline_imports_are_lazy() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import sys",
                    "import article_graph.pipeline as p",
                    "assert not {'neo4j', 'httpx', 'pypdfium2', 'celery'} & sys.modules.keys()",
                    "assert p.ServiceUnavailable.__name__ == 'ServiceUnavailable'",
                    "assert 'neo4j' in sys.modules",
                ]
            ),
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.integration
def test_happy_path_and_completed_redelivery(h: Harness) -> None:
    attempt = h.register()
    assert h.execute(attempt) == PublicationResult.COMPLETED
    state = h.state(attempt)
    assert state["active"] == str(attempt.revision_id)
    assert state["assertions"] == 1
    assert state["status"] == "completed"
    assert set(json.loads(str(state["checkpoints"]))) == {
        "preparation",
        "extraction",
        "resolution",
    }
    assert (h.llm.calls, h.embeddings.calls) == (1, 2)
    assert h.execute(attempt) == PublicationResult.COMPLETED
    assert (h.llm.calls, h.embeddings.calls) == (1, 2)


@pytest.mark.integration
@pytest.mark.parametrize(
    "stage", [Stage.EXTRACTION, Stage.RESOLUTION, Stage.PUBLICATION]
)
def test_stage_failure_resume_after_restart_and_old_active(
    h: Harness, stage: Stage
) -> None:
    old = h.register()
    h.execute(old)
    attempt = h.register(old.article_id)
    if stage == Stage.EXTRACTION:
        h.llm.failure = LLMError("provider_unavailable", retryable=True)
    elif stage == Stage.RESOLUTION:
        h.embeddings.failure = EmbeddingError("unavailable", retryable=True)
    else:
        h.publisher.fail = True
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.retryable and failed.value.stage == stage
    assert h.state(attempt)["active"] == str(old.revision_id)
    assert h.state(attempt)["assertions"] == 0
    calls = h.llm.calls, h.embeddings.calls
    h.llm.failure = h.embeddings.failure = None
    h.publisher.fail = False
    h.pipeline = h.make_pipeline()
    assert h.execute(h.retry(attempt)) == PublicationResult.COMPLETED
    assert h.llm.calls == calls[0] + (stage == Stage.EXTRACTION)
    assert h.embeddings.calls == calls[1] + (0 if stage == Stage.PUBLICATION else 2)
    assert h.state(attempt)["active"] == str(attempt.revision_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "model",
        "output",
        "endpoint",
        "preparation",
        "extraction",
        "resolution",
        "embedding",
    ],
)
def test_incompatible_checkpoint(h: Harness, change: str) -> None:
    attempt = h.register()
    h.publisher.fail = True
    with pytest.raises(PipelineError):
        h.execute(attempt)
    calls = h.llm.calls, h.embeddings.calls
    if change in {"model", "output", "endpoint"}:
        field, value = {
            "model": ("text_model", "different-model"),
            "output": ("output_mode", "json_schema"),
            "endpoint": ("base_url", "https://different.test/v1"),
        }[change]
        h.pipeline.llm_settings = h.llm_settings.model_copy(update={field: value})
    elif change == "embedding":
        # Endpoint participates in the config even when model/digest are unchanged.
        h.pipeline.embedding_settings = h.embedding_settings.model_copy(
            update={"base_url": "http://other-ollama.test"}
        )
    else:
        h.pipeline.versions = Versions().model_copy(update={change: "next-version"})
    h.publisher.fail = False
    assert h.execute(h.retry(attempt)) == PublicationResult.COMPLETED
    assert h.llm.calls == calls[0] + (change not in {"resolution", "embedding"})
    assert h.embeddings.calls == calls[1] + 2


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["token", "attempt", "expired", "new_revision"])
def test_fencing_prevents_provider_calls(h: Harness, kind: str) -> None:
    attempt = h.register()
    supplied = attempt
    if kind == "token":
        supplied = attempt.model_copy(update={"attempt_token": uuid4()})
    elif kind == "attempt":
        supplied = attempt.model_copy(update={"attempt": 2})
    elif kind == "expired":
        h.query(
            "MATCH (j:Job {job_id: $id}) SET j.lease_expires_at = 0",
            id=str(attempt.job_id),
        )
    else:
        h.register(attempt.article_id)
    if kind == "new_revision":
        assert h.execute(supplied) == PublicationResult.SUPERSEDED
        assert h.state(attempt)["status"] == "superseded"
    else:
        with pytest.raises(PipelineError) as failed:
            h.execute(supplied)
        assert not failed.value.retryable
    assert (h.llm.calls, h.embeddings.calls) == (0, 0)


@pytest.mark.integration
def test_revision_superseded_during_extraction_stops_resolution(h: Harness) -> None:
    attempt = h.register()

    def supersede() -> None:
        h.register(attempt.article_id)

    h.llm.before_response = supersede
    assert h.execute(attempt) == PublicationResult.SUPERSEDED
    assert (h.llm.calls, h.embeddings.calls) == (1, 0)
    assert h.state(attempt)["active"] is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "version",
        "payload_hash",
        "payload_schema",
        "revision",
        "key",
        "pointer",
    ],
)
def test_corrupt_checkpoint_is_recomputed(h: Harness, kind: str) -> None:
    attempt = h.register()
    h.publisher.fail = True
    with pytest.raises(PipelineError):
        h.execute(attempt)
    source = h.checkpoints.check(attempt)
    pointer = source.checkpoints[Stage.EXTRACTION]
    if kind == "missing":
        h.store._path(BlobNamespace.ARTIFACTS, pointer.blob_hash).unlink()
    else:
        with h.store.open(BlobNamespace.ARTIFACTS, pointer.blob_hash) as stream:
            envelope = json.loads(stream.read())
        if kind == "version":
            envelope["version"] = "checkpoint-future"
        elif kind == "payload_hash":
            envelope["payload_hash"] = "0" * 64
        elif kind == "key":
            envelope["key"] = "0" * 64
        elif kind in {"payload_schema", "revision"}:
            payload = json.loads(envelope["payload"])
            if kind == "revision":
                payload["revision_id"] = str(uuid4())
            else:
                payload["version"] = "extraction-future"
            envelope["payload"] = json.dumps(payload)
            envelope["payload_hash"] = sha256(envelope["payload"].encode()).hexdigest()
        if kind == "pointer":
            h.query(
                "MATCH (r:ArticleRevision {revision_id: $id}) SET r.pipeline_checkpoints = 'broken-json'",
                id=str(attempt.revision_id),
            )
        else:
            blob = h.store.put_artifact(BytesIO(json.dumps(envelope).encode()))
            h.checkpoints.save(
                attempt,
                Stage.EXTRACTION,
                Pointer(
                    key=pointer.key,
                    blob_hash=blob.content_hash,
                ),
            )
    h.publisher.fail = False
    assert h.execute(h.retry(attempt)) == PublicationResult.COMPLETED
    assert h.llm.calls == 2
    assert h.embeddings.calls == (4 if kind == "pointer" else 2)


@pytest.mark.integration
def test_attempt_wide_budget_counts_extraction_and_embeddings(h: Harness) -> None:
    attempt = h.register()
    h.pipeline.limits = Limits(llm_calls=2)
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.code == ErrorCode.LIMIT_EXCEEDED
    assert not failed.value.retryable
    assert h.llm.calls == 1 and h.embeddings.calls == 1
    assert h.state(attempt)["active"] is None


@pytest.mark.integration
def test_permanent_provider_failure(h: Harness) -> None:
    attempt = h.register()
    h.llm.failure = LLMError("response_invalid")
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.code == ErrorCode.EXTRACTION_INVALID
    assert not failed.value.retryable
    assert h.embeddings.calls == 0


@pytest.mark.integration
def test_real_candidate_repository(h: Harness) -> None:
    attempt = h.register()
    h.pipeline.candidates = Neo4jCandidates(h.driver, h.settings)
    assert h.execute(attempt) == PublicationResult.COMPLETED


@pytest.mark.integration
def test_manual_retry_new_job_reuses_revision_checkpoints(h: Harness) -> None:
    attempt = h.register()
    h.publisher.fail = True
    with pytest.raises(PipelineError):
        h.execute(attempt)
    h.query(
        "MATCH (j:Job {job_id: $id}), (r:ArticleRevision {revision_id: $revision}) "
        "SET j.status = 'failed', r.status = 'failed', "
        "j.error_code = 'provider_unavailable', j.error_message = 'Test failure'",
        id=str(attempt.job_id),
        revision=str(attempt.revision_id),
    )
    registered = ArticleRepository(h.driver, h.settings).retry(attempt.article_id).body
    retried = attempt.model_copy(
        update={
            "job_id": registered.job_id,
            "attempt_token": uuid4(),
        }
    )
    h.claim(retried)
    h.publisher.fail = False
    assert registered.job_id != attempt.job_id
    assert h.execute(retried) == PublicationResult.COMPLETED
    assert (h.llm.calls, h.embeddings.calls) == (1, 2)


@pytest.mark.integration
def test_timeout_is_retryable_and_cancels_provider(h: Harness) -> None:
    attempt = h.register()
    h.pipeline.limits = Limits(processing_seconds=1)
    h.llm.delay = 10
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.code == ErrorCode.PROCESSING_TIMEOUT
    assert failed.value.retryable
    assert h.embeddings.calls == 0
    assert h.state(attempt)["active"] is None


@pytest.mark.integration
def test_stolen_lease_after_response_does_not_checkpoint(h: Harness) -> None:
    attempt = h.register()

    def steal() -> None:
        h.claim(attempt.model_copy(update={"attempt_token": uuid4()}))

    h.llm.before_response = steal
    with pytest.raises(PipelineError):
        h.execute(attempt)
    assert (h.llm.calls, h.embeddings.calls) == (1, 0)
    checkpoints = json.loads(str(h.state(attempt)["checkpoints"]))
    assert set(checkpoints) == {"preparation"}


def _pdf() -> bytes:
    content = b"BT /F1 12 Tf 5 70 Td (Alice met Bob.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets: list[int] = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    startxref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return (
        data
        + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode()
    )


@pytest.mark.integration
def test_pdf_pipeline_checkpoint_includes_images(h: Harness) -> None:
    attempt = h.register(data=_pdf(), content_type="application/pdf")
    h.pipeline.llm_settings = h.llm_settings.model_copy(
        update={"vision_model": "fake-vision"}
    )
    h.publisher.fail = True
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.stage == Stage.PUBLICATION
    assert h.llm.images == 1
    h.publisher.fail = False
    assert h.execute(h.retry(attempt)) == PublicationResult.COMPLETED
    assert (h.llm.calls, h.embeddings.calls) == (1, 2)


@pytest.mark.integration
def test_pdf_requires_explicit_vision_before_provider(h: Harness) -> None:
    attempt = h.register(data=_pdf(), content_type="application/pdf")
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert not failed.value.retryable
    assert (h.llm.calls, h.embeddings.calls) == (0, 0)


@pytest.mark.integration
def test_source_integrity_checked_before_provider(h: Harness) -> None:
    attempt = h.register()
    source = h.checkpoints.check(attempt)
    h.store._path(BlobNamespace.SOURCES, source.source_hash).write_bytes(
        b"tampered test source"
    )
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.code == ErrorCode.INVALID_DOCUMENT
    assert failed.value.stage == Stage.PREPARATION and not failed.value.retryable
    assert (h.llm.calls, h.embeddings.calls) == (0, 0)


@pytest.mark.integration
def test_cancellation_propagates_without_publishing(h: Harness) -> None:
    attempt = h.register()

    async def scenario() -> None:
        started = asyncio.Event()
        h.llm.before_response = started.set
        h.llm.wait_on_response = asyncio.Event()
        task = asyncio.create_task(h.pipeline.run(attempt))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert h.state(attempt)["active"] is None
    assert h.embeddings.calls == 0


@pytest.mark.integration
def test_crash_window_after_paid_response_may_repeat(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = h.register()
    original = h.checkpoints.save

    def crash(attempt: Attempt, stage: Stage, pointer: Pointer | None) -> None:
        if stage == Stage.EXTRACTION:
            raise OSError("Simulated crash before durable checkpoint pointer")
        original(attempt, stage, pointer)

    monkeypatch.setattr(h.checkpoints, "save", crash)
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.retryable and h.llm.calls == 1
    monkeypatch.setattr(h.checkpoints, "save", original)
    assert h.execute(h.retry(attempt)) == PublicationResult.COMPLETED
    assert h.llm.calls == 2


class AmbiguousCandidates(NoCandidates):
    @override
    def search(
        self, vector: tuple[float, ...], metadata: EmbeddingMetadata, top_k: int
    ) -> tuple[Candidate, ...]:
        return (
            Candidate(
                character=Character(
                    character_id=UUID(int=500),
                    name="Alice",
                    description="Other context.",
                ),
                score=1,
            ),
        )


@pytest.mark.integration
def test_identity_calls_share_attempt_budget(h: Harness) -> None:
    attempt = h.register()
    h.pipeline.candidates = AmbiguousCandidates()
    h.pipeline.limits = Limits(llm_calls=3)
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.code == ErrorCode.LIMIT_EXCEEDED
    assert (h.llm.calls, h.embeddings.calls) == (2, 1)


@pytest.mark.integration
def test_publication_conflict_discards_resolution_only(h: Harness) -> None:
    attempt = h.register()
    calls = 0

    def conflict() -> None:
        nonlocal calls
        calls += 1
        from article_graph.publication import PublicationError

        raise PublicationError("candidate_changed")

    h.publisher.before_publish = conflict
    with pytest.raises(PipelineError) as failed:
        h.execute(attempt)
    assert failed.value.retryable and failed.value.stage == Stage.PUBLICATION
    checkpoints = json.loads(str(h.state(attempt)["checkpoints"]))
    assert set(checkpoints) == {"preparation", "extraction"}
    h.publisher.before_publish = None
    assert h.execute(h.retry(attempt)) == PublicationResult.COMPLETED
    assert h.llm.calls == 1 and h.embeddings.calls == 4 and calls == 1
