"""P21: API sockets, real Celery/dispatcher, isolated Neo4j/Redis and HTTP AI."""

import subprocess
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import httpx
import pytest
from fastapi import status as http_status

from article_graph.domain import Article, Character, Page, ProcessingResponse, Relation
from article_graph.security.auth import TokenPair
from tests.e2e.p21_fixture import diagram_pdf

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(ROOT / "tests/e2e.env"),
    "-p",
    "geomotiv-p21-e2e",
    "-f",
    str(ROOT / "docker-compose.e2e.yml"),
]


def compose(*args: str) -> str:
    result = subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


class HTTPHarness:
    def __init__(self, client: httpx.Client, control: httpx.Client) -> None:
        self.client = client
        self.control = control

    def counters(self) -> dict[str, int]:
        response = self.control.get("/control")
        response.raise_for_status()
        return response.json()

    def authorize(self) -> TokenPair:
        self.client.headers.pop("Authorization", None)
        response = self.client.post(
            "/auth/token",
            json={
                "app_token": "p21-public-app-token-for-isolated-e2e-only",
            },
        )
        assert response.status_code == http_status.HTTP_200_OK
        pair = TokenPair.model_validate(response.json())
        self.client.headers["Authorization"] = f"Bearer {pair.access_token}"
        return pair

    def upload(
        self,
        data: bytes,
        filename: str,
        article: UUID | None = None,
        *,
        unchanged: bool = False,
    ) -> ProcessingResponse:
        response = self.client.post(
            "/articles/upload",
            files={"file": (filename, data)},
            data={"article_id": str(article)} if article else {},
        )
        expected = (
            http_status.HTTP_200_OK if unchanged else http_status.HTTP_202_ACCEPTED
        )
        assert response.status_code == expected
        result = ProcessingResponse.model_validate(response.json())
        assert result.unchanged == unchanged
        return result

    def done(self, item: ProcessingResponse) -> Article:
        deadline = monotonic() + 90
        state: Article | None = None
        while monotonic() < deadline:
            response = self.client.get(item.status_url)
            assert response.status_code == http_status.HTTP_200_OK
            state = Article.model_validate(response.json())
            assert state.status != "failed", state.error
            if state.status == "completed":
                assert state.active_revision_id == item.revision_id
                return state
            Event().wait(min(0.1, max(0, deadline - monotonic())))
        pytest.fail(f"Job {item.job_id} timeout; safe state={state!r}")

    def page[T](self, path: str, model: type[T]) -> tuple[T, ...]:
        response = self.client.get(path)
        assert response.status_code == http_status.HTTP_200_OK
        assert response.headers["cache-control"] == "no-store"
        page = Page[model].model_validate(response.json())
        assert page.next_offset is None
        return page.items


@pytest.fixture(scope="module")
def stack(request: pytest.FixtureRequest) -> Iterator[HTTPHarness]:
    if not request.config.getoption("--e2e"):
        pytest.skip("Pass --e2e to start isolated P21 HTTP Compose")
    failures = request.session.testsfailed
    started = False
    try:
        # Recover from an interrupted earlier run without touching other projects.
        compose("down", "--volumes", "--remove-orphans", "--timeout", "10")
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "180")
        started = True
        with (
            httpx.Client(
                base_url="http://127.0.0.1:38000", timeout=10, trust_env=False
            ) as client,
            httpx.Client(
                base_url="http://127.0.0.1:38080", timeout=5, trust_env=False
            ) as control,
        ):
            yield HTTPHarness(client, control)
    finally:
        try:
            if not started or request.session.testsfailed > failures:
                print(compose("ps", "-a"))
                print(
                    compose(
                        "logs",
                        "--no-color",
                        "--tail",
                        "80",
                        "api",
                        "worker",
                        "dispatcher",
                        "doubles",
                        "init-schema",
                    )
                )
        finally:
            compose("down", "--volumes", "--remove-orphans", "--timeout", "10")


@pytest.fixture
def e2e(stack: HTTPHarness, request: pytest.FixtureRequest) -> Iterator[HTTPHarness]:
    failures = request.session.testsfailed
    yield stack
    if request.session.testsfailed > failures:
        print(compose("ps", "-a"))
        print(
            compose(
                "logs",
                "--no-color",
                "--tail",
                "80",
                "api",
                "worker",
                "dispatcher",
                "doubles",
                "init-schema",
            )
        )


def test_markdown_http_lifecycle(e2e: HTTPHarness) -> None:
    pair = e2e.authorize()
    source = b"# P21\nAlice met Bob."
    first = e2e.upload(source, "article.md")
    e2e.done(first)
    characters = e2e.page(f"/characters/by_article/{first.article_id}", Character)
    assert {c.name for c in characters} == {"Alice", "Bob"}
    assert set(characters) == set(e2e.page("/characters", Character))
    alice = next(c.character_id for c in characters if c.name == "Alice")
    paths = [
        "/characters",
        f"/characters/by_article/{first.article_id}",
        "/relations/types",
        f"/relations/connected/{alice}",
    ]
    types = e2e.client.get(paths[2])
    assert types.status_code == http_status.HTTP_200_OK
    assert types.json()["items"] == [{"kind": "FRIEND_OF", "directed": False}]
    relations = e2e.page(paths[3], Relation)
    assert len(relations) == 1
    assert relations[0].article_id == first.article_id
    assert relations[0].revision_id == first.revision_id
    before = e2e.counters()
    noop = e2e.upload(source, "article.md", unchanged=True)
    assert (noop.article_id, noop.revision_id, noop.job_id) == (
        first.article_id,
        first.revision_id,
        first.job_id,
    )
    assert e2e.counters() == before

    # Refresh really rotates a durable session via HTTP, then protects all reads.
    e2e.client.headers.pop("Authorization")
    for path in [*paths, first.status_url]:
        assert e2e.client.get(path).status_code == http_status.HTTP_401_UNAUTHORIZED
    assert (
        e2e.client.post(
            "/articles/upload", files={"file": ("denied.md", source)}
        ).status_code
        == http_status.HTTP_401_UNAUTHORIZED
    )
    refreshed = e2e.client.post(
        "/auth/refresh",
        json={
            "refresh_token": pair.refresh_token,
        },
    )
    assert refreshed.status_code == http_status.HTTP_200_OK
    rotated = TokenPair.model_validate(refreshed.json())
    assert rotated.refresh_token != pair.refresh_token
    e2e.client.headers["Authorization"] = f"Bearer {rotated.refresh_token}"
    assert e2e.client.get(paths[0]).status_code == http_status.HTTP_401_UNAUTHORIZED
    e2e.client.headers["Authorization"] = f"Bearer {rotated.access_token}"
    changed = e2e.upload(
        b"# P21_CHANGED\nAlice met Carol.", "article.md", first.article_id
    )
    assert changed.revision_id != first.revision_id and changed.job_id != first.job_id
    e2e.done(changed)
    current = e2e.page(paths[1], Character)
    assert {c.name for c in current} == {"Alice", "Carol"}
    assert set(current) == set(e2e.page(paths[0], Character))
    new_alice = next(c.character_id for c in current if c.name == "Alice")
    current_relations = e2e.page(f"/relations/connected/{new_alice}", Relation)
    assert len(current_relations) == 1
    assert current_relations[0].revision_id == changed.revision_id
    assert current_relations[0].relation_id != relations[0].relation_id
    bob = next(c.character_id for c in characters if c.name == "Bob")
    assert not e2e.page(f"/relations/connected/{bob}", Relation)


def test_pdf_relation_requires_received_image(e2e: HTTPHarness) -> None:
    e2e.authorize()
    # Only the vector arrow expresses the relationship; text has names alone.
    data = diagram_pdf()
    before = e2e.counters()
    item = e2e.upload(data, "diagram.pdf")
    e2e.done(item)
    characters = e2e.page(f"/characters/by_article/{item.article_id}", Character)
    assert {c.name for c in characters} == {"Alice", "Bob"}
    # Character UUID order does not identify the source of a directed relation.
    alice = next(c.character_id for c in characters if c.name == "Alice")
    bob = next(c.character_id for c in characters if c.name == "Bob")
    relations = e2e.page(f"/relations/connected/{alice}", Relation)
    relation = next(r for r in relations if r.article_id == item.article_id)
    assert relation.kind == "PARENT_OF" and relation.directed
    assert relation.subject_id == alice and relation.object_id == bob
    assert relation.revision_id == item.revision_id
    assert relation.evidence[0].kind == "visual"
    assert relation.evidence[0].page == 1
    assert e2e.counters()["images"] == before["images"] + 1
