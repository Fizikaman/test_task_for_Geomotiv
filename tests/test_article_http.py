"""P17 HTTP boundary and durable registration against isolated real Neo4j."""

import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from subprocess import run
from typing import NoReturn
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient
from httpx2 import Response
lazy import pypdfium2 as pdfium
lazy from neo4j import Driver

import article_graph.article_repository as repository_module
import article_graph.article_routes as routes
from article_graph.app import create_app
from article_graph.auth import issue_pair
from article_graph.domain import Article, ProcessingResponse
from article_graph.settings import Limits, LLMSettings, Neo4jSettings, Settings


@pytest.fixture
def settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llm_settings: LLMSettings
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
        app_id=f"p17-{uuid4()}",
        blob_root=tmp_path / "blobs",
        llm=llm_settings,
        limits=Limits(upload_bytes=2000),
    )


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("P17 must not call external HTTP APIs")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def authorized_client(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    client.headers["Authorization"] = (
        f"Bearer {issue_pair(settings).tokens.access_token}"
    )
    return client


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return authorized_client(settings)


@pytest.fixture
def db_client(
    settings: Settings,
    neo4j_settings: Neo4jSettings,
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    generated: list[str] = []

    def tracked_uuid() -> UUID:
        value = uuid4()
        generated.append(str(value))
        return value

    monkeypatch.setattr(repository_module, "uuid4", tracked_uuid)
    settings.neo4j = neo4j_settings
    try:
        with authorized_client(settings) as client:
            yield client
    finally:
        with neo4j_driver.session(database=neo4j_settings.database) as session:
            session.run(
                "MATCH (n) WHERE n.article_id IN $ids OR n.job_id IN $ids "
                "OR n.idempotency_id IN $ids OR n.outbox_id IN $ids DETACH DELETE n",
                ids=generated,
            ).consume()


def upload(client: TestClient, text: str, article_id: UUID | None = None) -> Response:
    fields: dict[str, tuple[None, str]] = {"text": (None, text)}
    if article_id is not None:
        fields["article_id"] = (None, str(article_id))
    return client.post("/articles/upload", files=fields)


@pytest.mark.integration
def test_upload_update_noop_replay_outbox_and_status(
    db_client: TestClient, neo4j_driver: Driver
) -> None:
    content = f"# Статья {uuid4()}\r\nА и Б\n"
    key = str(uuid4())
    first = db_client.post(
        "/articles/upload",
        files={"text": (None, content)},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == http_status.HTTP_202_ACCEPTED
    original = ProcessingResponse.model_validate(first.json())
    assert original.status == "queued"
    assert not original.unchanged
    # Inspect real durable nodes, not an in-memory repository double.
    driver = neo4j_driver
    with driver.session(database="neo4j") as session:
        row = session.run(
            "MATCH (a:Article {article_id: $id})-[:HAS_REVISION]->(r)"
            "-[:HAS_JOB]->(j)-[:HAS_OUTBOX]->(o) "
            "RETURN r.content_hash AS hash, r.number AS number, j.attempt AS attempt, "
            "o.status AS status",
            id=str(original.article_id),
        ).single(strict=True)
        assert row is not None
        assert row["number"] == 1 and row["attempt"] == 0
        assert row["status"] == "pending"
        assert row["hash"] == sha256(content.encode()).hexdigest()
    noop = upload(db_client, content)
    assert noop.status_code == http_status.HTTP_200_OK
    assert noop.json() == {**first.json(), "unchanged": True}
    file_noop = db_client.post(
        "/articles/upload",
        files={"file": ("../../ignored.md", content.encode(), "text/markdown")},
    )
    assert file_noop.status_code == http_status.HTTP_200_OK
    assert file_noop.json() == noop.json()
    update = upload(db_client, content + "new", original.article_id)
    assert update.status_code == http_status.HTTP_202_ACCEPTED
    assert update.json()["revision_id"] != str(original.revision_id)
    replay = db_client.post(
        "/articles/upload",
        files={"text": (None, content)},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == first.status_code and replay.json() == first.json()
    conflict = db_client.post(
        "/articles/upload",
        files={"text": (None, content + "changed")},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == http_status.HTTP_409_CONFLICT
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    status = db_client.get(original.status_url)
    assert status.status_code == http_status.HTTP_200_OK
    article = Article.model_validate(status.json())
    assert str(article.latest_revision_id) == update.json()["revision_id"]
    assert article.active_revision_id is None and article.error is None
    assert status.headers["cache-control"] == "no-store"


@pytest.mark.integration
def test_failed_update_safe_status_and_concurrent_retry(
    db_client: TestClient, neo4j_driver: Driver
) -> None:
    content = f"# {uuid4()}"
    first = ProcessingResponse.model_validate(upload(db_client, content).json())
    driver = neo4j_driver
    with driver.session(database="neo4j") as session:
        session.run(
            "MATCH (a:Article {article_id: $id}), (j:Job {job_id: a.job_id}) "
            "SET a.active_revision_id = j.revision_id, j.status = 'completed'",
            id=str(first.article_id),
        ).consume()
    assert db_client.get(first.status_url).json()["status"] == "completed"
    updated = ProcessingResponse.model_validate(
        upload(db_client, content + "new", first.article_id).json()
    )
    assert db_client.get(first.status_url).json()["active_revision_id"] == str(
        first.revision_id
    )
    with driver.session(database="neo4j") as session:
        session.run(
            "MATCH (j:Job {job_id: $id}) SET j.status = 'failed', "
            "j.error_code = 'internal_error', j.error_message = $secret",
            id=str(updated.job_id),
            secret="source/path token traceback " + content,
        ).consume()
    failed = db_client.get(first.status_url)
    assert failed.status_code == http_status.HTTP_200_OK
    assert failed.json()["error"] == {
        "code": "internal_error",
        "message": "Processing failed",
    }
    assert content not in failed.text and "traceback" not in failed.text
    noop = upload(db_client, content + "new", first.article_id)
    assert (
        noop.status_code == http_status.HTTP_200_OK
        and noop.json()["status"] == "failed"
    )

    def retry_request(_: int) -> Response:
        return db_client.post(first.status_url + "/retry")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(retry_request, range(2)))
    assert sorted(r.status_code for r in results) == [
        http_status.HTTP_202_ACCEPTED,
        http_status.HTTP_409_CONFLICT,
    ]
    accepted = next(
        r for r in results if r.status_code == http_status.HTTP_202_ACCEPTED
    )
    retry = ProcessingResponse.model_validate(accepted.json())
    assert retry.revision_id == updated.revision_id and retry.job_id != updated.job_id
    assert retry.status == "queued" and not retry.unchanged
    assert db_client.get(first.status_url).json()["active_revision_id"] == str(
        first.revision_id
    )
    with driver.session(database="neo4j") as session:
        row = session.run(
            "MATCH (j:Job {job_id: $id})-[:HAS_OUTBOX]->(o) "
            "RETURN j.attempt AS attempt, o.status AS status",
            id=str(retry.job_id),
        ).single(strict=True)
        assert row is not None and row["attempt"] == 0 and row["status"] == "pending"


@pytest.mark.integration
@pytest.mark.parametrize(
    "content_type", ["text/markdown", "text/plain", "application/pdf"]
)
def test_file_registration(
    db_client: TestClient, tmp_path: Path, content_type: str
) -> None:
    if content_type == "application/pdf":
        path = tmp_path / "test.pdf"
        with pdfium.PdfDocument.new() as pdf:
            pdf.new_page(72, 72).close()
            pdf.save(path)
        data = path.read_bytes()
    else:
        data = f"# UTF-8 файл {uuid4()}".encode()
    response = db_client.post(
        "/articles/upload", files={"file": ("anything", data, content_type)}
    )
    assert response.status_code == http_status.HTTP_202_ACCEPTED
    result = ProcessingResponse.model_validate(response.json())
    assert db_client.get(result.status_url).json()["status"] == "queued"


@pytest.mark.parametrize("endpoint", ["upload", "status", "retry"])
@pytest.mark.parametrize("token", ["missing", "invalid", "refresh"])
def test_auth(
    client: TestClient, settings: Settings, endpoint: str, token: str
) -> None:
    client.headers.pop("Authorization")
    if token != "missing":
        raw = issue_pair(settings).tokens.refresh_token if token == "refresh" else "bad"
        client.headers["Authorization"] = f"Bearer {raw}"
    path = f"/articles/{uuid4()}"
    response = (
        client.get(path)
        if endpoint == "status"
        else client.post(
            "/articles/upload" if endpoint == "upload" else path + "/retry"
        )
    )
    assert response.status_code == http_status.HTTP_401_UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "fields",
    [
        [("other", (None, "x"))],
        [("text", (None, ""))],
        [("text", (None, "  "))],
        [("text", (None, "x")), ("file", ("x.md", b"x", "text/plain"))],
        [("text", (None, "")), ("file", ("x.md", b"x", "text/plain"))],
        [("text", (None, "x")), ("text", (None, "y"))],
        [("text", (None, "x")), ("article_id", (None, "bad"))],
        [("text", (None, "x")), ("article_id", (None, ""))],
        [("file", (None, "not a file"))],
        [("text", ("text.md", b"not a field", "text/plain"))],
    ],
)
def test_invalid_multipart(
    client: TestClient,
    fields: list[tuple[str, tuple[None, str] | tuple[str, bytes, str]]],
) -> None:
    response = client.post("/articles/upload", files=fields)
    assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "data,content_type,expected,code",
    [
        (
            b"x",
            "image/png",
            http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
        ),
        (
            b"\xff",
            "text/plain",
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_document",
        ),
        (
            b"%PDF-1.7\nbroken",
            "application/pdf",
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_document",
        ),
        (
            b"",
            "text/plain",
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
        ),
        (
            b"x" * 2001,
            "text/plain",
            http_status.HTTP_413_CONTENT_TOO_LARGE,
            "payload_too_large",
        ),
    ],
)
@pytest.mark.integration
def test_file_errors(
    db_client: TestClient, data: bytes, content_type: str, expected: int, code: str
) -> None:
    response = db_client.post(
        "/articles/upload", files={"file": ("secret.md", data, content_type)}
    )
    assert response.status_code == expected
    assert response.json()["error"]["code"] == code
    assert "secret.md" not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        b'--b\r\nContent-Disposition: form-data; name="text"\r\n\r\nsecret',
        b'--b\r\nContent-Disposition: form-data; name="text"\r\n\r\n\xff\r\n--b--\r\n',
    ],
)
def test_malformed_body(client: TestClient, raw: bytes) -> None:
    response = client.post(
        "/articles/upload",
        content=raw,
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )
    assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT


def test_wire_limit_chunked_and_nonmultipart(client: TestClient) -> None:
    response = client.post(
        "/articles/upload",
        content=iter([b"x" * 40000] * 2),
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )
    assert response.status_code == http_status.HTTP_413_CONTENT_TOO_LARGE
    assert (
        client.post("/articles/upload", json={"text": "secret"}).status_code
        == http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    )


@pytest.mark.integration
def test_unknown_and_invalid_ids(db_client: TestClient) -> None:
    missing = uuid4()
    assert (
        upload(db_client, "new", missing).status_code == http_status.HTTP_404_NOT_FOUND
    )
    assert (
        db_client.get(f"/articles/{missing}").status_code
        == http_status.HTTP_404_NOT_FOUND
    )
    assert (
        db_client.post(f"/articles/{missing}/retry").status_code
        == http_status.HTTP_404_NOT_FOUND
    )
    assert (
        db_client.get("/articles/invalid").status_code
        == http_status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    assert (
        db_client.post("/articles/invalid/retry").status_code
        == http_status.HTTP_422_UNPROCESSABLE_CONTENT
    )


def test_blank_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/articles/upload",
        files={"text": (None, "x")},
        headers={"Idempotency-Key": " "},
    )
    assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
def test_source_size_and_pdf_page_limits(
    db_client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    # Apply bytes, rather than character count, to UTF-8 text without truncation.
    content = str(uuid4()) + "я" * 982
    assert len(content.encode()) == settings.limits.upload_bytes
    assert upload(db_client, content).status_code == http_status.HTTP_202_ACCEPTED
    oversized = upload(db_client, content + "я")
    assert oversized.status_code == http_status.HTTP_413_CONTENT_TOO_LARGE
    settings.limits = settings.limits.model_copy(update={"pdf_pages": 1})
    path = tmp_path / "two-pages.pdf"
    with pdfium.PdfDocument.new() as pdf:
        for _ in range(2):
            pdf.new_page(72, 72).close()
        pdf.save(path)
    response = db_client.post(
        "/articles/upload",
        files={"file": ("two-pages.pdf", path.read_bytes(), "application/pdf")},
    )
    assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "limit_exceeded"


@pytest.mark.parametrize("failure", [OSError, RuntimeError])
def test_storage_failure_is_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    def broken(*args: object, **kwargs: object) -> NoReturn:
        raise failure("source/path token secret traceback")

    monkeypatch.setattr(routes, "repository", broken)
    result = upload(client, "document secret")
    expected = (
        http_status.HTTP_503_SERVICE_UNAVAILABLE
        if failure is OSError
        else http_status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    assert result.status_code == expected
    assert "secret" not in result.text and "source/path" not in result.text


def test_unavailable_and_internal_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        client.get(f"/articles/{uuid4()}").status_code
        == http_status.HTTP_503_SERVICE_UNAVAILABLE
    )

    def broken(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("source/path token secret traceback")

    monkeypatch.setattr(routes, "repository", broken)
    result = client.get(f"/articles/{uuid4()}")
    assert result.status_code == http_status.HTTP_500_INTERNAL_SERVER_ERROR
    assert result.json()["error"]["code"] == "internal_error"
    assert "secret" not in result.text


def test_imports_keep_processing_lazy() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import sys",
                    "import article_graph.app",
                    "import article_graph.article_routes as r",
                    "assert 'neo4j' not in sys.modules",
                    "assert 'pypdfium2' not in sys.modules",
                    "assert 'article_graph.llm' not in sys.modules",
                    "assert 'article_graph.pipeline' not in sys.modules",
                    "assert 'celery' not in sys.modules",
                    "assert len(r.router.routes) == 3",
                    "assert r.run_in_threadpool.__name__ == 'run_in_threadpool'",
                ]
            ),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
