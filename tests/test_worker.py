import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from subprocess import run
from threading import Barrier, Event
from typing import LiteralString, override
from uuid import UUID, uuid4

import httpx
import pytest
from celery.contrib.testing.worker import start_worker
from celery.signals import task_success
from fastapi import status as http_status
from pydantic import SecretStr, ValidationError
lazy from neo4j import Driver

import article_graph.worker as worker_module
from article_graph.article_repository import ArticleRepository
from article_graph.blob_store import LocalBlobStore
from article_graph.domain import ErrorCode
from article_graph.embeddings import EmbeddingError, OllamaEmbeddings
from article_graph.llm import ChatCompletionsLLM
from article_graph.pipeline_checkpoints import Attempt, PipelineError, Stage
from article_graph.processing_lease import LeaseRepository
from article_graph.publication import PublicationResult
from article_graph.registration import RegistrationService
from article_graph.settings import Limits, LLMSettings, Neo4jSettings, OllamaSettings
from article_graph.worker import (
    TASK_NAME,
    PipelineRunner,
    WorkerConfig,
    WorkerSettings,
    create_app,
    execute_job,
)


@pytest.fixture
def settings(tmp_path: Path, llm_settings: LLMSettings) -> WorkerSettings:
    return WorkerSettings(
        _env_file=None,
        worker=WorkerConfig(broker_url=SecretStr("memory://"), blob_root=tmp_path),
        llm=llm_settings,
        ollama=OllamaSettings(
            base_url="http://ollama.test",
            model="worker-test:v1",
            digest="f" * 64,
            dimensions=1024,
        ),
    )


class Harness:
    def __init__(self, driver: Driver, database: Neo4jSettings, root: Path) -> None:
        self.driver = driver
        self.database = database
        self.service = RegistrationService(
            LocalBlobStore(root, max_bytes=10000), ArticleRepository(driver, database)
        )
        self.leases = LeaseRepository(driver, database, Limits(), 120)
        self.ids: list[str] = []

    def register(self, article_id: UUID | None = None) -> UUID:
        result = self.service.register_text(
            f"Worker test {uuid4()}",
            app_id="p15-test",
            article_id=article_id,
        ).body
        self.ids.append(str(result.article_id))
        return result.job_id

    def query(self, query: LiteralString, **params: object) -> list[dict[str, object]]:
        with self.driver.session(database=self.database.database) as session:
            return [dict(row.data()) for row in session.run(query, parameters=params)]

    def state(self, job: UUID) -> dict[str, object]:
        return self.query(
            "MATCH (j:Job {job_id: $job}) RETURN j.status AS status, "
            "j.attempt AS attempt, j.error_code AS error, "
            "j.heartbeat_at AS heartbeat, j.lease_expires_at AS expires, "
            "COUNT { (j)-[:HAS_OUTBOX]->() } AS outboxes",
            job=str(job),
        )[0]

    def ready(self, job: UUID) -> None:
        self.query("MATCH (j:Job {job_id: $job}) SET j.available_at = 0", job=str(job))

    def expire(self, job: UUID) -> None:
        self.query(
            "MATCH (j:Job {job_id: $job}) SET j.lease_expires_at = 0", job=str(job)
        )


@pytest.fixture
def h(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, tmp_path: Path
) -> Iterator[Harness]:
    harness = Harness(neo4j_driver, neo4j_settings, tmp_path)
    try:
        yield harness
    finally:
        harness.query(
            "MATCH (n) WHERE n.article_id IN $ids OR (n:Outbox AND EXISTS { "
            "MATCH (j:Job)-[:HAS_OUTBOX]->(n) WHERE j.article_id IN $ids }) "
            "DETACH DELETE n",
            ids=harness.ids,
        )


class Runner:
    def __init__(self, action: Callable[[Attempt], None], delay: float = 0) -> None:
        self.action = action
        self.delay = delay
        self.calls = 0
        self.closed = False
        self.started: asyncio.Event | None = None

    async def run(self, attempt: Attempt) -> PublicationResult:
        self.calls += 1
        if self.started is not None:
            self.started.set()
        await asyncio.sleep(self.delay)
        self.action(attempt)
        return PublicationResult.COMPLETED

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[PipelineRunner]:
        try:
            yield self
        finally:
            self.closed = True


def failure(attempt: Attempt) -> None:
    raise PipelineError(
        ErrorCode.PROVIDER_UNAVAILABLE, Stage.EXTRACTION, retryable=True
    )


def execute(h: Harness, job: UUID, runner: Runner, *, seconds: int = 10) -> int | None:
    return asyncio.run(
        execute_job(
            job,
            h.leases,
            runner.open,
            heartbeat_seconds=0.05,
            processing_seconds=seconds,
        )
    )


def test_eager_registration_and_configuration(settings: WorkerSettings) -> None:
    app = create_app(settings)
    try:
        assert TASK_NAME in app.tasks
        assert app.tasks[TASK_NAME].max_retries == settings.limits.job_attempts - 1
        assert app.conf.task_acks_late
        assert app.conf.task_reject_on_worker_lost
        assert app.conf.worker_prefetch_multiplier == 1
        assert (
            settings.limits.processing_seconds
            < app.conf.task_soft_time_limit
            < app.conf.task_time_limit
        )
        assert (
            app.conf.broker_transport_options["visibility_timeout"]
            > app.conf.task_time_limit
        )
        assert app.conf.task_ignore_result
        assert app.conf.accept_content == ["json"]
    finally:
        app.close()


def test_startup_validation_and_lazy_runtime() -> None:
    code = "\n".join(
        [
            "import sys",
            "from article_graph.worker import WorkerSettings, create_app, TASK_NAME",
            "settings = WorkerSettings(_env_file=None)",
            "app = create_app(settings)",
            "assert TASK_NAME in app.tasks",
            "assert not {'neo4j', 'httpx', 'pypdfium2'} & sys.modules.keys()",
            "import article_graph.worker as w",
            "assert w.ServiceUnavailable.__name__ == 'ServiceUnavailable'",
            "assert 'neo4j' in sys.modules",
            "assert 'httpx' not in sys.modules",
            "app.close()",
        ]
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARTICLE_GRAPH_")}
    env.update(
        {
            "PYTHONPATH": "src",
            "ARTICLE_GRAPH_WORKER__BROKER_URL": "memory://",
            "ARTICLE_GRAPH_WORKER__BLOB_ROOT": ".cache/worker-test",
            "ARTICLE_GRAPH_LLM__BASE_URL": "https://provider.test/v1",
            "ARTICLE_GRAPH_LLM__API_KEY": "test-only",
            "ARTICLE_GRAPH_LLM__TEXT_MODEL": "test-model",
            "ARTICLE_GRAPH_LLM__OUTPUT_MODE": "json_object",
            "ARTICLE_GRAPH_OLLAMA__BASE_URL": "http://ollama.test",
            "ARTICLE_GRAPH_OLLAMA__MODEL": "worker-test:v1",
            "ARTICLE_GRAPH_OLLAMA__DIGEST": "f" * 64,
            "ARTICLE_GRAPH_OLLAMA__DIMENSIONS": "1024",
        }
    )
    result = run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    missing = run(
        [sys.executable, "-c", "from article_graph.celery_app import app"],
        env={k: v for k, v in env.items() if k != "ARTICLE_GRAPH_LLM__TEXT_MODEL"},
        cwd=Path(__file__).resolve().parents[1] / "tests",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert missing.returncode != 0
    assert "text_model" in missing.stderr


def test_invalid_lease_settings(settings: WorkerSettings) -> None:
    with pytest.raises(ValidationError):
        WorkerConfig(
            broker_url=SecretStr("memory://"), blob_root=Path("."), lease_seconds=10
        )
    with pytest.raises(ValidationError):
        WorkerSettings(
            _env_file=None,
            **{
                **settings.model_dump(),
                "limits": Limits(processing_seconds=1),
            },
        )


@pytest.mark.integration
def test_concurrent_duplicate_and_busy_lease(h: Harness) -> None:
    job = h.register()
    barrier = Barrier(2)

    def claim(index: int) -> Attempt | None:
        barrier.wait(timeout=10)
        return h.leases.claim(job)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(result is not None for result in results) == 1
    assert h.state(job)["attempt"] == 1
    runner = Runner(failure)
    assert execute(h, job, runner) is None
    assert runner.calls == 0


@pytest.mark.integration
def test_expired_owner_cannot_heartbeat_or_fail(h: Harness) -> None:
    job = h.register()
    first = h.leases.claim(job)
    assert first is not None
    assert h.leases.heartbeat(first)
    h.expire(job)
    assert not h.leases.heartbeat(first)
    assert (
        h.leases.fail(first, PipelineError(ErrorCode.INTERNAL_ERROR, Stage.EXTRACTION))
        is None
    )
    second = h.leases.claim(job)
    assert second is not None and second.attempt == 2
    assert second.attempt_token != first.attempt_token
    before = h.state(job)
    assert not h.leases.heartbeat(first)
    assert (
        h.leases.fail(first, PipelineError(ErrorCode.INTERNAL_ERROR, Stage.EXTRACTION))
        is None
    )
    assert h.state(job) == before


@pytest.mark.integration
def test_permanent_failure_and_duplicate(h: Harness) -> None:
    job = h.register()

    def permanent(attempt: Attempt) -> None:
        raise PipelineError(ErrorCode.EXTRACTION_INVALID, Stage.EXTRACTION)

    runner = Runner(permanent)
    assert execute(h, job, runner) is None
    assert runner.closed
    assert h.state(job)["status"] == "failed"
    assert h.state(job)["error"] == "extraction_invalid"
    assert h.state(job)["outboxes"] == 1
    assert execute(h, job, runner) is None
    assert runner.calls == 1


@pytest.mark.integration
def test_transient_bounded_by_durable_attempts(h: Harness) -> None:
    job = h.register()
    runner = Runner(failure)
    assert execute(h, job, runner) == 2
    assert h.state(job)["status"] == "queued"
    assert h.state(job)["outboxes"] == 2
    assert h.leases.claim(job) is None  # Backoff cannot be bypassed by duplicates.
    h.ready(job)
    assert execute(h, job, runner) == 4
    h.ready(job)
    assert execute(h, job, runner) is None
    assert h.state(job)["status"] == "failed"
    assert h.state(job)["attempt"] == 3
    assert h.state(job)["outboxes"] == 3
    assert execute(h, job, runner) is None
    assert runner.calls == 3


@pytest.mark.integration
def test_crashed_attempts_also_exhaust_budget(h: Harness) -> None:
    job = h.register()
    for number in range(1, 4):
        attempt = h.leases.claim(job)
        assert attempt is not None and attempt.attempt == number
        h.expire(job)
    assert h.leases.claim(job) is None
    assert h.state(job)["status"] == "failed"


@pytest.mark.integration
@pytest.mark.parametrize("status", ["completed", "superseded"])
def test_terminal_jobs_skip_pipeline(h: Harness, status: str) -> None:
    job = h.register()
    h.query(
        "MATCH (j:Job {job_id: $job}) SET j.status = $status",
        job=str(job),
        status=status,
    )
    runner = Runner(failure)
    assert execute(h, job, runner) is None
    assert runner.calls == 0
    assert h.state(job)["attempt"] == 0


@pytest.mark.integration
def test_new_revision_supersedes_old_job(h: Harness) -> None:
    job = h.register()
    article = h.query(
        "MATCH (j:Job {job_id: $job}) RETURN j.article_id AS id", job=str(job)
    )[0]["id"]
    assert isinstance(article, str)
    h.register(UUID(article))
    assert h.leases.claim(job) is None
    assert h.state(job)["status"] == "superseded"


@pytest.mark.integration
def test_heartbeat_survives_long_call(h: Harness) -> None:
    job = h.register()
    h.leases.seconds = 1
    before: list[object] = []

    def done(attempt: Attempt) -> None:
        before.append(h.state(job)["heartbeat"])
        assert h.leases.heartbeat(attempt)
        assert h.leases.claim(job) is None
        raise PipelineError(ErrorCode.EXTRACTION_INVALID, Stage.EXTRACTION)

    runner = Runner(done, delay=1.5)
    assert execute(h, job, runner) is None
    assert before and runner.closed
    assert h.state(job)["status"] == "failed"


@pytest.mark.integration
def test_timeout_closes_resources(h: Harness) -> None:
    job = h.register()
    runner = Runner(failure, delay=20)
    assert execute(h, job, runner, seconds=1) == 2
    assert runner.closed
    assert h.state(job)["error"] == "processing_timeout"


@pytest.mark.integration
def test_lease_loss_cancels_pipeline(h: Harness) -> None:
    job = h.register()
    runner = Runner(failure, delay=20)

    async def scenario() -> None:
        runner.started = asyncio.Event()
        task = asyncio.create_task(
            execute_job(
                job,
                h.leases,
                runner.open,
                heartbeat_seconds=0.05,
                processing_seconds=10,
            )
        )
        await asyncio.wait_for(runner.started.wait(), 5)
        await asyncio.to_thread(h.expire, job)
        assert await asyncio.wait_for(task, 5) is None

    asyncio.run(scenario())
    assert runner.closed
    assert h.state(job)["status"] == "processing"


@pytest.mark.integration
@pytest.mark.parametrize("retryable", [False, True])
def test_real_worker_consumes_registered_task(
    h: Harness,
    settings: WorkerSettings,
    monkeypatch: pytest.MonkeyPatch,
    retryable: bool,
) -> None:
    settings = settings.model_copy(
        update={
            "neo4j": h.database,
            "limits": Limits(job_attempts=2, retry_backoff_seconds=1),
        }
    )
    job = h.register()

    def fail(attempt: Attempt) -> None:
        raise PipelineError(
            ErrorCode.EXTRACTION_INVALID, Stage.EXTRACTION, retryable=retryable
        )

    runner = Runner(fail)

    def factory(settings: WorkerSettings, leases: LeaseRepository):
        return runner.open()

    monkeypatch.setattr(worker_module, "open_pipeline", factory)
    app = create_app(settings)
    event = Event()

    def success(sender: object = None, **kwargs: object) -> None:
        event.set()

    task_success.connect(success, weak=False)
    try:
        with start_worker(
            app,
            pool="solo",
            concurrency=1,
            perform_ping_check=False,
            loglevel="ERROR",
            shutdown_timeout=15,
        ):
            app.send_task(TASK_NAME, args=[str(job)])
            assert event.wait(15)
            assert h.state(job)["status"] == "failed"
            event.clear()
            app.send_task(TASK_NAME, args=[str(job)])
            assert event.wait(15)
            assert runner.calls == (2 if retryable else 1)
            assert runner.closed
    finally:
        task_success.disconnect(success)
        app.close()


class TrackingTransport(httpx.MockTransport):
    closed = False

    @override
    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


@pytest.mark.integration
@pytest.mark.parametrize("cancelled", [False, True])
def test_actual_clients_close_on_initialization_failure(
    h: Harness,
    settings: WorkerSettings,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    import article_graph.embeddings as embeddings_module

    async def scenario() -> None:
        started = asyncio.Event()

        async def unavailable(request: httpx.Request) -> httpx.Response:
            started.set()
            if cancelled:
                await asyncio.Event().wait()
            return httpx.Response(http_status.HTTP_503_SERVICE_UNAVAILABLE)

        llm_transport = TrackingTransport(unavailable)
        embedding_transport = TrackingTransport(unavailable)
        llm = ChatCompletionsLLM(settings.llm, settings.limits, transport=llm_transport)
        adapter = OllamaEmbeddings(settings.ollama, transport=embedding_transport)

        def llm_factory(settings: LLMSettings, limits: Limits) -> ChatCompletionsLLM:
            return llm

        def embedding_factory(
            settings: OllamaSettings, **kwargs: object
        ) -> OllamaEmbeddings:
            return adapter

        monkeypatch.setattr(worker_module, "ChatCompletionsLLM", llm_factory)
        monkeypatch.setattr(embeddings_module, "OllamaEmbeddings", embedding_factory)

        async def initialize() -> None:
            async with worker_module.open_pipeline(settings, h.leases):
                pytest.fail(
                    "Unavailable embedding provider must fail during initialization"
                )

        if cancelled:
            task = asyncio.create_task(initialize())
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(EmbeddingError):
                await initialize()
        assert llm_transport.closed
        assert embedding_transport.closed

    asyncio.run(scenario())
