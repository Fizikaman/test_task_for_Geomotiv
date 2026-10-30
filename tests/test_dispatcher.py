import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from subprocess import run
from threading import Barrier
from typing import LiteralString
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError
lazy from neo4j import Driver

from article_graph.article_repository import ArticleRepository
from article_graph.blob_store import LocalBlobStore
from article_graph.dispatcher import (
    CeleryBroker,
    Dispatcher,
    DispatcherConfig,
    DispatcherSettings,
)
from article_graph.outbox import Delivery, OutboxRepository
from article_graph.pipeline_checkpoints import Attempt
from article_graph.publication import PublicationResult
from article_graph.registration import RegistrationService
from article_graph.settings import Limits, Neo4jSettings
from article_graph.worker import PipelineRunner, execute_job


class ControlledBroker:
    def __init__(self) -> None:
        self.messages: list[UUID] = []
        self.fail_before = False
        self.fail_after = False

    def publish(self, job_id: UUID) -> None:
        if self.fail_before:
            raise ConnectionError("controlled outage")
        self.messages.append(job_id)
        if self.fail_after:
            raise ConnectionError("controlled ambiguous publish")


class Harness:
    def __init__(self, driver: Driver, database: Neo4jSettings, root: Path) -> None:
        self.driver = driver
        self.database = database
        self.service = RegistrationService(
            LocalBlobStore(root, max_bytes=10000), ArticleRepository(driver, database)
        )
        self.repository = OutboxRepository(driver, database, Limits())
        self.leases = self.repository.leases
        self.config = DispatcherConfig(broker_url=SecretStr("memory://"))
        self.broker = ControlledBroker()
        self.dispatcher = Dispatcher(self.repository, self.broker, self.config)
        self.ids: list[str] = []

    def register(self, article_id: UUID | None = None) -> UUID:
        result = self.service.register_text(
            f"Dispatcher test {uuid4()}", app_id="p16-test", article_id=article_id
        ).body
        self.ids.append(str(result.article_id))
        return result.job_id

    def query(self, query: LiteralString, **params: object) -> list[dict[str, object]]:
        with self.driver.session(database=self.database.database) as session:
            return [dict(row.data()) for row in session.run(query, parameters=params)]

    def state(self, job: UUID) -> dict[str, object]:
        return self.query(
            "MATCH (j:Job {job_id: $job}) RETURN j.status AS status, "
            "j.attempt AS attempt, j.error_code AS error, j.available_at AS ready, "
            "COUNT { (j)-[:HAS_OUTBOX]->() } AS outboxes",
            job=str(job),
        )[0]

    def ready(self, job: UUID) -> None:
        self.query(
            "MATCH (j:Job {job_id: $job}) SET j.available_at = 0, "
            "j.last_dispatched_at = 0, j.created_at = 0 "
            "WITH j MATCH (j)-[:HAS_OUTBOX]->(o) SET o.available_at = 0, "
            "o.claim_expires_at = 0",
            job=str(job),
        )

    def claim_delivery(self) -> Delivery:
        delivery = self.repository.claim(60)
        assert delivery is not None
        return delivery


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


def test_settings_and_lazy_runtime() -> None:
    with pytest.raises(ValidationError):
        DispatcherConfig(broker_url=SecretStr("  "))
    with pytest.raises(ValidationError):
        DispatcherConfig(broker_url=SecretStr("memory://"), retry_max_seconds=1)
    with pytest.raises(ValidationError):
        DispatcherSettings(_env_file=None, dispatcher={})
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARTICLE_GRAPH_")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    code = "\n".join(
        [
            "import sys",
            "from article_graph.dispatcher import DispatcherSettings",
            "settings = DispatcherSettings(_env_file=None)",
            "assert not {'celery', 'neo4j', 'httpx', 'pypdfium2'} & sys.modules.keys()",
            "import article_graph.dispatcher as d",
            "assert d.Celery.__name__ == 'Celery'",
            "assert 'celery' in sys.modules",
            "assert 'neo4j' not in sys.modules",
        ]
    )
    missing = run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert missing.returncode != 0
    assert "dispatcher" in missing.stderr
    env["ARTICLE_GRAPH_DISPATCHER__BROKER_URL"] = "memory://"
    valid = run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert valid.returncode == 0, valid.stderr


def test_celery_adapter_sends_real_json_message() -> None:
    from kombu import Connection

    broker = CeleryBroker(DispatcherConfig(broker_url=SecretStr("memory://")))
    job = uuid4()
    try:
        broker.publish(job)
        with Connection("memory://") as connection:
            queue = connection.SimpleQueue("article_graph")
            try:
                message = queue.get(block=False)
                assert message.headers["task"] == "article_graph.process_job"
                assert message.payload[0] == [str(job)]
                assert message.content_type == "application/json"
                message.ack()
            finally:
                queue.close()
        assert broker.app.conf.task_publish_retry is False
    finally:
        broker.close()


@pytest.mark.integration
@pytest.mark.parametrize("after_publish", [False, True])
def test_outage_and_ambiguous_publish_are_retried(
    h: Harness, after_publish: bool
) -> None:
    job = h.register()
    h.broker.fail_before = not after_publish
    h.broker.fail_after = after_publish
    for _ in range(4):
        assert h.dispatcher.tick() == 0
        assert h.state(job)["status"] == "queued"
        assert h.state(job)["attempt"] == 0
        h.ready(job)
    assert h.repository.recover(100, 600) == 0  # existing durable delivery
    assert h.state(job)["outboxes"] == 1
    h.broker.fail_before = h.broker.fail_after = False
    assert h.dispatcher.tick() == 1
    assert h.broker.messages.count(job) == (5 if after_publish else 1)
    assert h.dispatcher.tick() == 0
    assert h.leases.claim(job) is not None
    assert h.leases.claim(job) is None


@pytest.mark.integration
@pytest.mark.parametrize("published", [False, True])
def test_process_death_before_mark_sent(h: Harness, published: bool) -> None:
    job = h.register()
    first = h.claim_delivery()
    if published:
        h.broker.publish(job)
    # No finish at all: simulates process death before/after broker acceptance.
    assert h.repository.claim(60) is None
    h.ready(job)
    second = h.claim_delivery()
    assert first.outbox_id == second.outbox_id
    assert first.token != second.token
    assert not h.repository.finish(first, sent=True)
    h.broker.publish(second.job_id)
    assert h.repository.finish(second, sent=True)
    assert not h.repository.finish(first, sent=False)
    assert h.broker.messages.count(job) == (2 if published else 1)
    assert h.leases.claim(job) is not None
    assert h.leases.claim(job) is None


@pytest.mark.integration
def test_two_dispatchers_publish_once_with_live_claim(h: Harness) -> None:
    jobs = {h.register() for _ in range(8)}
    barrier = Barrier(2)

    def tick() -> int:
        dispatcher = Dispatcher(h.repository, h.broker, h.config)
        barrier.wait(timeout=10)
        return dispatcher.tick()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(tick) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(results) == len(jobs)
    assert set(h.broker.messages) == jobs
    assert len(h.broker.messages) == len(jobs)


@pytest.mark.integration
@pytest.mark.parametrize("deleted", [False, True])
def test_recover_queued_lost_delivery(h: Harness, deleted: bool) -> None:
    job = h.register()
    assert h.dispatcher.tick() == 1
    if deleted:
        h.query(
            "MATCH (:Job {job_id: $job})-[:HAS_OUTBOX]->(o) DETACH DELETE o",
            job=str(job),
        )
    assert h.repository.recover(100, 600) == 0
    h.ready(job)
    assert h.repository.recover(100, 600) == 1
    assert h.repository.recover(100, 600) == 0
    assert h.dispatcher.tick() == 1
    assert h.broker.messages == [job, job]
    assert h.leases.claim(job) is not None
    assert h.leases.claim(job) is None


@pytest.mark.integration
def test_concurrent_recovery_creates_one_outbox(h: Harness) -> None:
    job = h.register()
    h.dispatcher.tick()
    h.ready(job)
    barrier = Barrier(2)

    def recover() -> int:
        barrier.wait(timeout=10)
        return h.repository.recover(100, 600)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(results) == 1
    assert h.state(job)["outboxes"] == 2


@pytest.mark.integration
def test_expired_processing_recovery_fences_old_owner(h: Harness) -> None:
    job = h.register()
    h.dispatcher.tick()
    old = h.leases.claim(job)
    assert old is not None
    h.query("MATCH (j:Job {job_id: $job}) SET j.lease_expires_at = 0", job=str(job))
    assert h.repository.recover(100, 600) == 1
    assert not h.leases.heartbeat(old)
    assert h.state(job)["status"] == "queued"
    assert h.dispatcher.tick() == 0  # durable recovery backoff
    assert h.leases.claim(job) is None
    h.ready(job)
    assert h.dispatcher.tick() == 1
    new = h.leases.claim(job)
    assert new is not None and new.attempt == old.attempt + 1
    assert new.attempt_token != old.attempt_token


@pytest.mark.integration
def test_recovery_respects_live_heartbeat_and_exhaustion(h: Harness) -> None:
    job = h.register()
    h.dispatcher.tick()
    for index in range(1, Limits().job_attempts + 1):
        attempt = h.leases.claim(job)
        assert attempt is not None and attempt.attempt == index
        assert h.leases.heartbeat(attempt)
        h.ready(job)
        assert h.repository.recover(100, 600) == 0
        assert h.state(job)["status"] == "processing"
        h.query("MATCH (j:Job {job_id: $job}) SET j.lease_expires_at = 0", job=str(job))
        recovered = h.repository.recover(100, 600)
        if index < Limits().job_attempts:
            assert recovered == 1
            h.ready(job)
            assert h.dispatcher.tick() == 1
        else:
            assert recovered == 0
    assert h.state(job)["status"] == "failed"
    assert h.state(job)["error"] == "processing_timeout"
    assert h.leases.claim(job) is None
    assert h.dispatcher.tick() == 0


@pytest.mark.integration
def test_recovery_rechecks_heartbeat_under_lock(h: Harness) -> None:
    job = h.register()
    h.dispatcher.tick()
    attempt = h.leases.claim(job)
    assert attempt is not None
    # A scanner can have observed an earlier expiry. A fresh heartbeat wins
    # before recovery acquires Article/Job; direct recover must re-read state.
    assert h.leases.heartbeat(attempt)
    assert not h.leases.recover(job, 600)
    assert h.state(job)["status"] == "processing"


@pytest.mark.integration
@pytest.mark.parametrize("terminal", ["completed", "superseded"])
def test_terminal_duplicates_never_open_pipeline(h: Harness, terminal: str) -> None:
    job = h.register()
    if terminal == "superseded":
        row = h.query(
            "MATCH (j:Job {job_id: $job}) RETURN j.article_id AS article", job=str(job)
        )[0]
        h.register(UUID(str(row["article"])))
        assert h.leases.claim(job) is None
    else:
        h.query("MATCH (j:Job {job_id: $job}) SET j.status = 'completed'", job=str(job))

    class ForbiddenRunner:
        async def run(self, attempt: Attempt) -> PublicationResult:
            raise AssertionError("terminal job ran pipeline")

    @asynccontextmanager
    async def forbidden() -> AsyncGenerator[PipelineRunner]:
        raise AssertionError("terminal job opened pipeline resources")
        yield ForbiddenRunner()

    h.dispatcher.tick()  # Old pending deliveries can still arrive at the worker.
    for _ in range(2):
        assert (
            asyncio.run(
                execute_job(
                    job, h.leases, forbidden, heartbeat_seconds=1, processing_seconds=10
                )
            )
            is None
        )
    assert h.state(job)["status"] == terminal


@pytest.mark.integration
def test_mark_sent_database_failure_replays_publish(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = h.register()
    finish = h.repository.finish

    def fail_mark(delivery: Delivery, *, sent: bool, delay: int = 0) -> bool:
        raise ConnectionError("controlled database outage after publish")

    monkeypatch.setattr(h.repository, "finish", fail_mark)
    with pytest.raises(ConnectionError):
        h.dispatcher.tick()
    assert h.broker.messages == [job]
    monkeypatch.setattr(h.repository, "finish", finish)
    h.ready(job)
    assert h.dispatcher.tick() == 1
    assert h.broker.messages == [job, job]
