"""P20: real processes and transports; only AI endpoints are HTTP doubles."""

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from time import monotonic
from typing import LiteralString
from uuid import UUID, uuid4

import httpx
import pytest
from celery import Celery
from fastapi import status as http_status
from fastapi.testclient import TestClient
from pydantic import SecretStr
lazy from neo4j import Driver
lazy from redis import Redis
lazy from redis.exceptions import ConnectionError as RedisConnectionError

from article_graph.app import create_app
from article_graph.auth import issue_pair
from article_graph.database import open_database
from article_graph.domain import Article, ProcessingResponse
from article_graph.settings import LLMSettings, Neo4jSettings, Settings
from article_graph.worker import TASK_NAME

pytestmark = [pytest.mark.integration, pytest.mark.infrastructure]
ROOT = Path(__file__).resolve().parents[1]
BLOBS = ROOT / ".cache" / "p20-blobs"
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(ROOT / "tests" / "infrastructure.env"),
    "-p",
    "geomotiv-p20-test",
    "-f",
    str(ROOT / "docker-compose.integration.yml"),
]


def compose(*args: str) -> str:
    result = subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env={
            **os.environ,
            "P20_TEST_UID": str(getattr(os, "getuid", lambda: 10001)()),
            "P20_TEST_GID": str(getattr(os, "getgid", lambda: 10001)()),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def until[T](
    description: str,
    probe: Callable[[], T],
    *,
    seconds: float = 90,
    diagnostic: Callable[[], object] | None = None,
) -> T:
    deadline = monotonic() + seconds
    last: object = None
    while monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except (httpx.TransportError, ConnectionError, RedisConnectionError) as error:
            last = type(error).__name__
        Event().wait(min(0.1, max(0, deadline - monotonic())))
    details = diagnostic() if diagnostic is not None else None
    # Never dump task args, provider bodies or configuration/credentials.
    pytest.fail(
        f"Timeout: {description}; last={last!r}; state={details!r}"
        f"\n{compose('ps', '-a')}"
    )


class Harness:
    def __init__(self, driver: Driver, client: TestClient) -> None:
        self.driver = driver
        self.client = client
        self.http = httpx.Client(
            base_url="http://127.0.0.1:28080", trust_env=False, timeout=5
        )
        self.broker = Celery("p20-test", broker="redis://127.0.0.1:26379/0")
        self.broker.conf.task_default_queue = "article_graph"

    def query(self, query: LiteralString, **params: object) -> list[dict[str, object]]:
        with self.driver.session(database="neo4j") as session:
            return [row.data() for row in session.run(query, parameters=params)]

    def counters(self) -> dict[str, int]:
        response = self.http.get("/control")
        response.raise_for_status()
        return response.json()

    def control(self, **values: bool) -> None:
        self.http.post("/control", json=values).raise_for_status()

    def upload(self, text: str, article: UUID | None = None) -> ProcessingResponse:
        fields = {"text": (None, text)}
        if article is not None:
            fields["article_id"] = (None, str(article))
        response = self.client.post("/articles/upload", files=fields)
        assert response.status_code in {
            http_status.HTTP_200_OK,
            http_status.HTTP_202_ACCEPTED,
        }
        return ProcessingResponse.model_validate(response.json())

    def state(self, item: ProcessingResponse) -> Article:
        response = self.client.get(item.status_url)
        assert response.status_code == http_status.HTTP_200_OK
        return Article.model_validate(response.json())

    def done(self, item: ProcessingResponse, status: str = "completed") -> Article:
        def probe() -> Article | None:
            state = self.state(item)
            assert state.status != "failed" or status == "failed", state.error
            return state if state.status == status else None

        result = until(
            f"job {item.job_id} becomes {status}",
            probe,
            diagnostic=lambda: self.diagnostic(item),
        )
        assert result is not None
        return result

    def diagnostic(self, item: ProcessingResponse) -> object:
        return self.query(
            "MATCH (j:Job {job_id: $id}) OPTIONAL MATCH (j)-[:HAS_OUTBOX]->(o) "
            "RETURN j.status AS status, j.attempt AS attempt, "
            "j.error_code AS error, j.lease_expires_at AS lease_expires_at, "
            "o.status AS delivery, o.delivery_attempt AS delivery_attempt",
            id=str(item.job_id),
        )

    def assertions(self, item: ProcessingResponse) -> list[dict[str, object]]:
        return self.query(
            "MATCH (a:Article {article_id: $id})-[:HAS_REVISION]->(v)"
            "-[:ASSERTS]->(r:RelationAssertion) "
            "WHERE v.revision_id = a.active_revision_id "
            "RETURN r.relation_id AS id, r.revision_id AS revision ORDER BY id",
            id=str(item.article_id),
        )

    def idle(self, *, expected_active: int = 0) -> None:
        def probe() -> bool:
            # Fresh control connection survives a broker restart; limiting the
            # destination returns as soon as this worker replies.
            with self.broker.connection_for_read() as connection:
                inspector = self.broker.control.inspect(
                    connection=connection,
                    destination=["p20@worker"],
                    timeout=2,
                    limit=1,
                )
                active = inspector.active(safe=True)
                reserved = inspector.reserved()
                scheduled = inspector.scheduled()
            with Redis(
                host="127.0.0.1", port=26379, socket_timeout=2, socket_connect_timeout=2
            ) as redis:
                queued = redis.llen("article_graph")
            return bool(active and reserved and scheduled) and (
                sum(len(tasks) for tasks in active.values()) == expected_active
                and not any(reserved.values())
                and not any(scheduled.values())
                and not queued
            )

        until(
            "worker has consumed all deliveries",
            probe,
            diagnostic=lambda: self.query(
                "MATCH (j:Job) RETURN j.status AS status, count(*) AS count"
            ),
        )

    def send(self, item: ProcessingResponse) -> None:
        self.broker.send_task(TASK_NAME, args=[str(item.job_id)], retry=False)

    def accepted(self) -> int:
        with self.broker.connection_for_read() as connection:
            stats = self.broker.control.inspect(
                connection=connection, destination=["p20@worker"], timeout=2, limit=1
            ).stats()
        if not stats:
            return -1
        return int(stats["p20@worker"]["total"].get(TASK_NAME, 0))

    def duplicates(self, item: ProcessingResponse, *, active: int = 0) -> None:
        accepted = self.accepted()
        assert accepted >= 0
        for _ in range(3):
            self.send(item)
        # Worker acceptance is an observable delivery barrier. Checking only an
        # empty Redis queue could race with messages moving to the worker pool.
        until(
            "worker accepted all duplicate messages",
            lambda: self.accepted() >= accepted + 3,
            diagnostic=self.accepted,
        )
        self.idle(expected_active=active)


@pytest.fixture(scope="module")
def infra(request: pytest.FixtureRequest) -> Iterator[Harness]:
    if not request.config.getoption("--infrastructure"):
        pytest.skip("Pass --infrastructure to start isolated P20 Compose")
    assert BLOBS.resolve() == ROOT / ".cache" / "p20-blobs"
    BLOBS.mkdir(parents=True, exist_ok=True)
    try:
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "180")
        database = Neo4jSettings(
            uri="bolt://127.0.0.1:27687",
            username="neo4j",
            password=SecretStr("geomotiv-p20-test-only"),
            database="neo4j",
        )
        settings = Settings(
            _env_file=None,
            app_token="p20-app-" * 8,
            jwt_access_key="p20-access-" * 8,
            jwt_refresh_key="p20-refresh-" * 8,
            app_id="p20-test",
            blob_root=BLOBS,
            neo4j=database,
            ollama=None,
            llm=LLMSettings(
                base_url="http://127.0.0.1:28080/v1",
                api_key=SecretStr("p20-no-paid-api"),
                text_model="p20-text",
                output_mode="json_object",
            ),
        )
        with (
            open_database(database) as driver,
            TestClient(create_app(settings)) as client,
        ):
            client.headers["Authorization"] = (
                f"Bearer {issue_pair(settings).tokens.access_token}"
            )
            harness = Harness(driver, client)
            try:
                until(
                    "registered worker ready",
                    lambda: harness.broker.control.ping(
                        destination=["p20@worker"], timeout=2
                    ),
                )
                yield harness
            finally:
                harness.control(release=True)
                harness.http.close()
                harness.broker.close()
    finally:
        # The fixed Compose project owns an ephemeral DB and broker. No production
        # endpoint, broad Cypher deletion or Redis FLUSH is accepted here.
        compose("down", "--volumes", "--remove-orphans", "--timeout", "10")
        assert BLOBS.resolve() == ROOT / ".cache" / "p20-blobs"
        shutil.rmtree(BLOBS)


@pytest.fixture
def h(infra: Harness) -> Iterator[Harness]:
    try:
        yield infra
    finally:
        infra.control(release=True)
        compose("start", "redis", "worker", "dispatcher")
        infra.idle()


def text(marker: str = "") -> str:
    return f"Alice met Bob. {marker} {uuid4()}"


def test_new_noop_update_and_failed_update(h: Harness) -> None:
    source = text()
    first = h.upload(source)
    assert h.done(first).active_revision_id == first.revision_id
    original = h.assertions(first)
    assert len(original) == 1 and original[0]["revision"] == str(first.revision_id)
    characters = h.client.get(f"/characters/by_article/{first.article_id}")
    assert characters.status_code == http_status.HTTP_200_OK
    assert len(characters.json()["items"]) == 2
    character_id = characters.json()["items"][0]["character_id"]
    endpoint = f"/relations/connected/{character_id}"
    graph_response = h.client.get(endpoint)
    assert graph_response.status_code == http_status.HTTP_200_OK
    assert graph_response.json()["items"][0]["relation_id"] == original[0]["id"]
    h.idle()
    until(
        "both published characters searchable in the real vector index",
        lambda: (
            h.query(
                "CALL db.index.vector.queryNodes('character_embedding', 10, $vector) "
                "YIELD node RETURN count(node) AS count",
                vector=[1.0] + [0.0] * 1023,
            )
            == [{"count": 2}]
        ),
    )
    counters = h.counters()
    noop = h.upload(source)
    assert noop.unchanged and noop.job_id == first.job_id
    # A following real delivery is an ordering barrier, not a fixed quiet sleep.
    other = h.upload(text())
    h.done(other)
    h.idle()
    assert h.counters()["llm"] > counters["llm"]
    other_graph = h.assertions(other)
    assert len(other_graph) == 1
    shared = h.client.get(f"/characters/by_article/{other.article_id}")
    assert shared.status_code == http_status.HTTP_200_OK
    assert shared.json()["items"] == characters.json()["items"]
    counters = h.counters()
    assert h.upload(source).unchanged
    h.idle()
    assert h.counters() == counters
    update = h.upload(text("P20_EMPTY"), first.article_id)
    assert h.done(update).active_revision_id == update.revision_id
    assert h.assertions(first) == []
    visible = h.client.get(endpoint)
    assert visible.status_code == http_status.HTTP_200_OK
    assert [item["relation_id"] for item in visible.json()["items"]] == [
        other_graph[0]["id"]
    ]
    assert h.assertions(other) == other_graph
    failed = h.upload(text("P20_FAIL"), other.article_id)
    state = h.done(failed, "failed")
    assert state.active_revision_id == other.revision_id
    assert state.latest_revision_id == failed.revision_id and state.error is not None
    assert h.assertions(other) == other_graph


def test_duplicate_delivery_is_consumed_without_ai_or_graph_changes(h: Harness) -> None:
    before_blocked = h.counters()["blocked"]
    h.control(block_next=True)
    item = h.upload(text())
    until(
        "first delivery in flight",
        lambda: h.counters()["blocked"] == before_blocked + 1,
        diagnostic=h.counters,
    )
    inflight = h.counters()
    h.duplicates(item, active=1)
    assert h.counters() == inflight
    h.control(release=True)
    h.done(item)
    h.idle()
    before = h.counters()
    graph = h.assertions(item)
    h.duplicates(item)
    assert h.counters() == before
    assert h.assertions(item) == graph
    assert h.query(
        "MATCH (j:Job {job_id: $id}) RETURN j.attempt AS attempt", id=str(item.job_id)
    ) == [{"attempt": 1}]


@pytest.mark.parametrize("stale", ["revision", "attempt"])
def test_inflight_stale_work_cannot_publish(h: Harness, stale: str) -> None:
    before = h.counters()
    h.control(block_next=True)
    old = h.upload(text())
    until(
        "old extraction held by HTTP double",
        lambda: h.counters()["blocked"] == before["blocked"] + 1,
        diagnostic=h.counters,
    )
    if stale == "revision":
        latest = h.upload(text("P20_EMPTY"), old.article_id)
    else:
        # Simulate loss of the first lease while its HTTP request is in flight;
        # recovery and the next attempt still run in real dispatcher/worker.
        h.query(
            "MATCH (j:Job {job_id: $id}) SET j.lease_expires_at = 0", id=str(old.job_id)
        )
        latest = old
    state = h.done(latest)
    assert state.active_revision_id == latest.revision_id
    graph = h.assertions(latest)
    assert len(graph) == (0 if stale == "revision" else 1)
    h.control(release=True)
    until(
        "held provider response released",
        lambda: h.counters()["released"] == before["released"] + 1,
        diagnostic=h.counters,
    )
    h.idle()
    assert h.assertions(latest) == graph
    assert h.state(latest).active_revision_id == latest.revision_id
    expected = {
        "status": "superseded" if stale == "revision" else "completed",
        "attempt": 1 if stale == "revision" else 2,
    }
    until(
        "stale job fenced or recovered",
        lambda: (
            h.query(
                "MATCH (j:Job {job_id: $id}) RETURN j.status AS status, j.attempt AS attempt",
                id=str(old.job_id),
            )
            == [expected]
        ),
        diagnostic=lambda: h.diagnostic(old),
    )
    if stale == "revision":
        assert h.query(
            "MATCH (r:RelationAssertion {revision_id: $id}) RETURN count(r) AS count",
            id=str(old.revision_id),
        ) == [{"count": 0}]


def test_broker_outage_keeps_registration_and_recovers(h: Harness) -> None:
    compose("stop", "--timeout", "5", "redis")
    item = h.upload(text())
    assert h.state(item).status == "queued"
    until(
        "durable failed outbox publish",
        lambda: h.query(
            "MATCH (j:Job {job_id: $id})-[:HAS_OUTBOX]->(o) "
            "WHERE o.delivery_attempt > 0 AND o.status = 'pending' "
            "RETURN o.status AS status",
            id=str(item.job_id),
        ),
        diagnostic=lambda: h.diagnostic(item),
    )
    compose("start", "redis")
    assert h.done(item).active_revision_id == item.revision_id
    assert len(h.assertions(item)) == 1


def test_worker_restart_after_registration_consumes_durable_delivery(
    h: Harness,
) -> None:
    compose("stop", "--timeout", "10", "worker")
    item = h.upload(text())
    until(
        "outbox sent with worker stopped",
        lambda: h.query(
            "MATCH (j:Job {job_id: $id})-[:HAS_OUTBOX]->(o) "
            "WHERE o.status = 'sent' RETURN o.status AS status",
            id=str(item.job_id),
        ),
        diagnostic=lambda: h.diagnostic(item),
    )
    assert h.state(item).status == "queued"
    compose("start", "worker")
    assert h.done(item).active_revision_id == item.revision_id
    assert len(h.assertions(item)) == 1
