"""P18 HTTP reads against real publication and the isolated Neo4j database."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from subprocess import run
from typing import NoReturn
from uuid import uuid4

import httpx
import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient

from article_graph.app import create_app
from article_graph.auth import issue_pair
from article_graph.domain import Character, Page, Relation, RelationType, TextEvidence
from article_graph.settings import Limits, LLMSettings, Settings
from tests.test_publication import Database
from tests.test_publication import (
    db as db,  # Reuse real publication and scoped cleanup.
)


@pytest.fixture
def settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm_settings: LLMSettings
) -> Settings:
    monkeypatch.chdir(tmp_path)
    for name in os.environ:
        if name.startswith("ARTICLE_GRAPH_"):
            monkeypatch.delenv(name)
    return Settings(
        _env_file=None,
        app_token="test-app-" * 8,
        jwt_access_key="test-access-" * 8,
        jwt_refresh_key="test-refresh-" * 8,
        app_id=f"p18-{uuid4()}",
        llm=llm_settings,
        limits=Limits(page_default=2, page_max=3),
    )


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("P18 must not call external HTTP APIs")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def authorized_client(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    client.headers["Authorization"] = (
        f"Bearer {issue_pair(settings).tokens.access_token}"
    )
    return client


@pytest.fixture
def client(db: Database, settings: Settings) -> Iterator[TestClient]:
    settings.neo4j = db.settings
    with authorized_client(settings) as client:
        yield client


def read_page[T](client: TestClient, path: str, model: type[T]) -> Page[T]:
    response = client.get(path)
    assert response.status_code == http_status.HTTP_200_OK, response.text
    assert response.headers["cache-control"] == "no-store"
    return Page[model].model_validate(response.json())


def all_items[T](client: TestClient, path: str, model: type[T]) -> list[T]:
    items: list[T] = []
    offset = 0
    for _ in range(100):
        separator = "&" if "?" in path else "?"
        page = read_page(client, f"{path}{separator}limit=1&offset={offset}", model)
        assert page.limit == 1 and page.offset == offset
        items.extend(page.items)
        if page.next_offset is None:
            return items
        assert page.next_offset == offset + 1
        offset = page.next_offset
    pytest.fail("Pagination did not terminate")


@pytest.mark.integration
def test_active_distinct_characters_and_pending_update(
    client: TestClient, db: Database
) -> None:
    first = db.prepare()
    db.publisher.publish(first)
    db.query(
        "MATCH (r:ArticleRevision {revision_id: $id})-[:MENTIONS]->(c) "
        "CREATE (r)-[:MENTIONS]->(c)",
        id=str(first.artifact.revision_id),
    )
    shared = db.profile(first.artifact.characters[0].character_id)
    second = db.prepare(shared=shared)
    db.publisher.publish(second)
    expected = {c.character_id for p in (first, second) for c in p.artifact.characters}
    items = all_items(client, "/characters", Character)
    ids = [c.character_id for c in items]
    assert ids == sorted(set(ids), key=str)
    assert expected <= set(ids)
    by_article = all_items(
        client, f"/characters/by_article/{first.article_id}", Character
    )
    assert {c.character_id for c in by_article} == {
        c.character_id for c in first.artifact.characters
    }
    pending = db.prepare(article_id=first.article_id, empty=True)
    assert (
        all_items(client, f"/characters/by_article/{first.article_id}", Character)
        == by_article
    )
    db.publisher.publish(pending)
    assert not read_page(
        client, f"/characters/by_article/{first.article_id}", Character
    ).items
    ids_after = {c.character_id for c in all_items(client, "/characters", Character)}
    assert shared.character_id in ids_after
    historical = next(c.character_id for c in by_article if c != shared)
    assert historical not in ids_after
    assert not read_page(client, f"/relations/connected/{historical}", Relation).items
    assert read_page(client, "/characters?offset=1000000", Character).items == ()
    # A known article with no first publication has an empty active graph.
    unpublished = db.prepare(empty=True)
    assert not read_page(
        client, f"/characters/by_article/{unpublished.article_id}", Character
    ).items


@pytest.mark.integration
@pytest.mark.parametrize("directed", [True, False])
def test_direction_types_evidence_and_multiple_articles(
    client: TestClient, db: Database, directed: bool
) -> None:
    kind = "P18_" + uuid4().hex.upper()
    first = db.prepare(kind=kind, directed=directed)
    relation = first.artifact.relations[0]
    evidence = relation.evidence + (
        relation.evidence[0],
        TextEvidence(fragment_id="other", quote="Additional explicit evidence."),
    )
    relation = relation.model_copy(update={"evidence": evidence})
    first = first.model_copy(
        update={
            "artifact": first.artifact.model_copy(update={"relations": (relation,)})
        }
    )
    db.publisher.publish(first)
    db.query(
        "MATCH (r:ArticleRevision {revision_id: $id})-[:ASSERTS]->(v) "
        "CREATE (r)-[:ASSERTS]->(v)",
        id=str(first.artifact.revision_id),
    )
    shared = db.profile(relation.subject_id)
    second = db.prepare(shared=shared, kind=kind, directed=directed)
    db.publisher.publish(second)
    endpoint = f"/relations/connected/{relation.subject_id}?type={kind}"
    outgoing = all_items(client, endpoint, Relation)
    expected = sorted(
        [relation, second.artifact.relations[0]], key=lambda r: str(r.relation_id)
    )
    assert outgoing == expected
    assert (
        all_items(client, f"/relations/connected/{relation.subject_id}", Relation)
        == expected
    )
    opposite = all_items(
        client, f"/relations/connected/{relation.object_id}?type={kind}", Relation
    )
    assert opposite == ([] if directed else [relation])
    types = all_items(client, "/relations/types", RelationType)
    assert [t.kind for t in types] == sorted({t.kind for t in types})
    assert RelationType(kind=kind, directed=directed) in types
    pending = db.prepare(article_id=first.article_id, empty=True)
    assert all_items(client, endpoint, Relation) == expected
    db.publisher.publish(pending)
    assert all_items(client, endpoint, Relation) == [second.artifact.relations[0]]
    last = db.prepare(article_id=second.article_id, empty=True)
    db.publisher.publish(last)
    assert not all_items(client, endpoint, Relation)
    assert kind not in {
        t.kind for t in all_items(client, "/relations/types", RelationType)
    }
    assert not read_page(
        client,
        f"/relations/connected/{shared.character_id}?type=UNKNOWN_TYPE",
        Relation,
    ).items


PATHS = (
    "/characters",
    f"/characters/by_article/{uuid4()}",
    "/relations/types",
    f"/relations/connected/{uuid4()}",
)


@pytest.mark.integration
@pytest.mark.parametrize("path", PATHS)
def test_auth_and_pagination_validation(client: TestClient, path: str) -> None:
    for authorization in ("", "Bearer invalid"):
        response = client.get(path, headers={"Authorization": authorization})
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED
    for query in (
        "limit=0",
        "limit=-1",
        "limit=4",
        "offset=-1",
        "limit=abc",
        "offset=9223372036854775808",
    ):
        response = client.get(f"{path}?{query}")
        assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.integration
def test_self_relation_failed_update_and_mentions_without_relations(
    client: TestClient, db: Database, settings: Settings
) -> None:
    first = db.prepare()
    relation = first.artifact.relations[0]
    relation = relation.model_copy(update={"object_id": relation.subject_id})
    first = first.model_copy(
        update={
            "artifact": first.artifact.model_copy(update={"relations": (relation,)})
        }
    )
    db.publisher.publish(first)
    endpoint = f"/relations/connected/{relation.subject_id}"
    assert all_items(client, endpoint, Relation) == [relation]
    replacement = db.prepare(
        article_id=first.article_id,
        shared=db.profile(relation.subject_id),
        with_relation=False,
    )
    db.query(
        "MATCH (r:ArticleRevision {revision_id: $id}) SET r.status = 'failed'",
        id=str(replacement.artifact.revision_id),
    )
    assert all_items(client, endpoint, Relation) == [relation]
    db.query(
        "MATCH (r:ArticleRevision {revision_id: $id}) SET r.status = 'processing'",
        id=str(replacement.artifact.revision_id),
    )
    db.publisher.publish(replacement)
    assert not all_items(client, endpoint, Relation)
    assert relation.subject_id in {
        c.character_id for c in all_items(client, "/characters", Character)
    }
    refresh = issue_pair(settings).tokens.refresh_token
    assert (
        client.get(endpoint, headers={"Authorization": f"Bearer {refresh}"}).status_code
        == http_status.HTTP_401_UNAUTHORIZED
    )


@pytest.mark.integration
def test_empty_unknown_and_invalid_inputs(client: TestClient, db: Database) -> None:
    known = db.prepare(with_relation=False)
    db.publisher.publish(known)
    character = known.artifact.characters[0].character_id
    for prefix in ("/characters/by_article/", "/relations/connected/"):
        missing = client.get(prefix + str(uuid4()))
        assert missing.status_code == http_status.HTTP_404_NOT_FOUND
        assert missing.json()["error"]["code"] == "not_found"
        assert client.get(prefix + "invalid").status_code == (
            http_status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    for kind in ("friend_of", "", "PARENT_OF' OR true //"):
        response = client.get(
            f"/relations/connected/{character}", params={"type": kind}
        )
        assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT
    assert read_page(client, "/characters", Character).limit == 2
    assert not read_page(client, f"/relations/connected/{character}", Relation).items
    assert not read_page(client, "/relations/types?offset=1000000", RelationType).items


def test_graph_runtime_lazy_imports_and_eager_openapi() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import article_graph.graph_routes as routes\n"
            "import article_graph.graph_repository as repository\n"
            "assert 'neo4j' not in sys.modules\n"
            "assert 'httpx' not in sys.modules\n"
            "assert 'pypdfium2' not in sys.modules\n"
            "assert 'celery' not in sys.modules\n"
            "assert len(routes.router.routes) == 4\n"
            "assert repository.Driver.__name__ == 'Driver'\n"
            "assert 'neo4j' in sys.modules\n",
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_eager_openapi_and_unavailable_database(settings: Settings) -> None:
    client = authorized_client(settings)
    schema = client.get("/openapi.json").json()
    for path in (
        "/characters",
        "/characters/by_article/{article_id}",
        "/relations/types",
        "/relations/connected/{character_id}",
    ):
        assert "get" in schema["paths"][path]
    assert client.get("/characters").status_code == (
        http_status.HTTP_503_SERVICE_UNAVAILABLE
    )
