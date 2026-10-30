import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from subprocess import run
from threading import Barrier
from typing import Literal
from uuid import UUID, uuid4

import pytest
lazy from neo4j import Driver, ManagedTransaction

import article_graph.db.article_repository as repository_module
from article_graph.db.article_repository import ArticleRepository
from article_graph.documents.blob_store import BlobNamespace, LocalBlobStore
from article_graph.documents.document_format import DocumentError
from article_graph.domain import ErrorCode
from article_graph.processing.registration import (
    RegistrationError,
    RegistrationResult,
    RegistrationService,
    UploadRequest,
)
from article_graph.settings import Neo4jSettings


@pytest.fixture
def repository(
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ArticleRepository]:
    generated: list[str] = []

    def tracked_uuid() -> UUID:
        value = uuid4()
        generated.append(str(value))
        return value

    monkeypatch.setattr(repository_module, "uuid4", tracked_uuid)
    try:
        yield ArticleRepository(neo4j_driver, neo4j_settings)
    finally:
        with neo4j_driver.session(database=neo4j_settings.database) as session:
            session.run(
                "MATCH (n) WHERE n.article_id IN $ids OR n.job_id IN $ids "
                "OR n.idempotency_id IN $ids OR n.outbox_id IN $ids DETACH DELETE n",
                ids=generated,
            ).consume()


@pytest.fixture
def service(repository: ArticleRepository, tmp_path: Path) -> RegistrationService:
    return RegistrationService(LocalBlobStore(tmp_path, max_bytes=10000), repository)


def text() -> str:
    return f"Article {uuid4()}\n"


def counts(repository: ArticleRepository) -> tuple[int, int, int, int, int]:
    with repository.driver.session(database=repository.database) as session:
        row = session.run(
            "CALL { MATCH (n:Article) RETURN count(n) AS articles } "
            "CALL { MATCH (n:ArticleRevision) RETURN count(n) AS revisions } "
            "CALL { MATCH (n:Job) RETURN count(n) AS jobs } "
            "CALL { MATCH (n:Outbox) RETURN count(n) AS outboxes } "
            "CALL { MATCH (n:IdempotencyRecord) RETURN count(n) AS keys } "
            "RETURN articles, revisions, jobs, outboxes, keys"
        ).single(strict=True)
        assert row is not None
        return (
            row["articles"],
            row["revisions"],
            row["jobs"],
            row["outboxes"],
            row["keys"],
        )


def set_status(
    repository: ArticleRepository,
    result: RegistrationResult,
    status: Literal["failed", "completed", "processing"],
) -> None:
    # Only fixtures simulate the future worker/publication implementation.
    with repository.driver.session(database=repository.database) as session:
        session.run(
            "MATCH (a:Article {article_id: $article}), (j:Job {job_id: $job}) "
            "SET j.status = $status, "
            "j.error_code = CASE WHEN $status = 'failed' THEN 'internal_error' END, "
            "j.error_message = CASE WHEN $status = 'failed' THEN 'Processing failed' END "
            "FOREACH (_ IN CASE WHEN $status = 'completed' THEN [1] ELSE [] END | "
            "SET a.active_revision_id = j.revision_id)",
            article=str(result.body.article_id),
            job=str(result.body.job_id),
            status=status,
        ).consume()


def test_imports_keep_optional_adapters_lazy() -> None:
    completed = run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import sys",
                    "import article_graph.processing.registration",
                    "import article_graph.db.article_repository as repo",
                    "assert 'neo4j' not in sys.modules",
                    "assert 'pypdfium2' not in sys.modules",
                    "assert repo.Driver.__name__ == 'Driver'",
                    "assert 'neo4j' in sys.modules",
                    "assert 'celery' not in sys.modules",
                ]
            ),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.integration
def test_registration_stores_source_and_atomic_graph(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    content = text()
    result = service.register_text(content, app_id="test")
    assert result.status_code == HTTPStatus.ACCEPTED
    assert result.body.status == "queued"
    assert not result.body.unchanged
    digest = sha256(content.encode("utf-8")).hexdigest()
    with service.blobs.open(BlobNamespace.SOURCES, digest) as stream:
        assert stream.read() == content.encode("utf-8")
    with repository.driver.session(database=repository.database) as session:
        row = session.run(
            "MATCH (a:Article)-[:HAS_REVISION]->(r:ArticleRevision)"
            "-[:HAS_JOB]->(j:Job)-[:HAS_OUTBOX]->(o:Outbox) "
            "WHERE a.article_id = $id RETURN a, r, j, o",
            id=str(result.body.article_id),
        ).single(strict=True)
        assert row is not None
        assert row["r"]["number"] == 1
        assert row["r"]["source_hash"] == digest
        assert row["r"]["source_namespace"] == "sources"
        assert row["r"]["source_size"] == len(content.encode("utf-8"))
        assert row["j"]["attempt"] == 0
        assert row["o"]["status"] == "pending"
        assert row["o"]["job_id"] == str(result.body.job_id)
    assert repository.get(result.body.article_id).active_revision_id is None


@pytest.mark.integration
@pytest.mark.parametrize("state", ["queued", "processing", "completed", "failed"])
def test_noop_creates_no_revision_job_or_outbox(
    service: RegistrationService,
    repository: ArticleRepository,
    state: Literal["queued", "processing", "completed", "failed"],
) -> None:
    content = text()
    first = service.register_text(content, app_id="test")
    if state != "queued":
        set_status(repository, first, state)
    before = counts(repository)
    for article_id in (None, first.body.article_id):
        result = service.register_text(content, app_id="test", article_id=article_id)
        assert result.status_code == HTTPStatus.OK
        assert result.body.unchanged
        assert result.body.revision_id == first.body.revision_id
        assert result.body.job_id == first.body.job_id
        assert result.body.status == state
        assert counts(repository) == before


@pytest.mark.integration
def test_idempotency_replays_original_code_and_body(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    content, key = text(), str(uuid4())
    first = service.register_text(content, app_id="test", idempotency_key=key)
    set_status(repository, first, "failed")
    service.retry(first.body.article_id)
    service.register_text(text(), app_id="test", article_id=first.body.article_id)
    before = counts(repository)
    assert service.register_text(content, app_id="test", idempotency_key=key) == first
    assert counts(repository) == before


@pytest.mark.integration
def test_idempotent_noop_snapshot_and_application_scope(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    content, key = text(), str(uuid4())
    first = service.register_text(content, app_id="test")
    noop = service.register_text(content, app_id="test", idempotency_key=key)
    set_status(repository, first, "failed")
    assert service.register_text(content, app_id="test", idempotency_key=key) == noop
    assert noop.status_code == HTTPStatus.OK
    other = service.register_text(text(), app_id="other-app", idempotency_key=key)
    assert other.status_code == HTTPStatus.ACCEPTED


@pytest.mark.integration
@pytest.mark.parametrize("change", ["hash", "article", "method", "type"])
def test_key_conflict_covers_entire_canonical_request(
    service: RegistrationService, repository: ArticleRepository, change: str
) -> None:
    content, key = text(), str(uuid4())
    first = service.register_text(content, app_id="test", idempotency_key=key)
    before = counts(repository)
    with pytest.raises(RegistrationError) as error:
        if change in {"method", "type"}:
            service.register_file(
                BytesIO(content.encode()),
                app_id="test",
                idempotency_key=key,
                content_type="text/plain" if change == "type" else "text/markdown",
            )
        else:
            service.register_text(
                text() if change == "hash" else content,
                app_id="test",
                idempotency_key=key,
                article_id=first.body.article_id if change == "article" else None,
            )
    assert error.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert error.value.status_code == HTTPStatus.CONFLICT
    assert counts(repository) == before


@pytest.mark.integration
def test_file_content_type_is_part_of_key(service: RegistrationService) -> None:
    content, key = text().encode(), str(uuid4())
    service.register_file(
        BytesIO(content), app_id="test", content_type="text/plain", idempotency_key=key
    )
    with pytest.raises(RegistrationError, match="idempotency_conflict"):
        service.register_file(
            BytesIO(content),
            app_id="test",
            content_type="text/markdown",
            idempotency_key=key,
        )


@pytest.mark.integration
def test_missing_article_and_retry(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    before = counts(repository)
    missing = uuid4()
    for action in (
        lambda: service.register_text(text(), app_id="test", article_id=missing),
        lambda: service.retry(missing),
        lambda: repository.get(missing),
    ):
        with pytest.raises(RegistrationError) as error:
            action()
        assert error.value.status_code == HTTPStatus.NOT_FOUND
        assert error.value.code == ErrorCode.NOT_FOUND
    assert counts(repository) == before


@pytest.mark.integration
def test_explicit_update_keeps_active_and_does_not_redirect_to_other_article(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    old_content, new_content = text(), text()
    old = service.register_text(old_content, app_id="test")
    set_status(repository, old, "completed")
    other = service.register_text(new_content, app_id="test")
    updated = service.register_text(
        new_content, app_id="test", article_id=old.body.article_id
    )
    assert updated.body.article_id == old.body.article_id != other.body.article_id
    assert updated.body.revision_id != old.body.revision_id
    article = repository.get(old.body.article_id)
    assert article.active_revision_id == old.body.revision_id
    assert article.latest_revision_id == updated.body.revision_id
    assert article.status == "queued"
    set_status(repository, updated, "failed")
    failed = repository.get(old.body.article_id)
    assert failed.active_revision_id == old.body.revision_id
    assert failed.status == "failed" and failed.error is not None
    # Both articles now have this current hash: choose lexicographically smallest UUID.
    noop = service.register_text(new_content, app_id="test")
    assert str(noop.body.article_id) == min(
        str(old.body.article_id), str(other.body.article_id)
    )
    # Historical hash is not current input, even though still the active result.
    fresh = service.register_text(old_content, app_id="test")
    assert fresh.status_code == HTTPStatus.ACCEPTED
    assert fresh.body.article_id != old.body.article_id


@pytest.mark.integration
def test_retry_failed_keeps_content_and_active_result(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    first = service.register_text(text(), app_id="test")
    set_status(repository, first, "completed")
    latest = service.register_text(
        text(), app_id="test", article_id=first.body.article_id
    )
    set_status(repository, latest, "failed")
    before = counts(repository)
    retried = service.retry(first.body.article_id)
    after = counts(repository)
    assert after == (before[0], before[1], before[2] + 1, before[3] + 1, before[4])
    assert retried.status_code == HTTPStatus.ACCEPTED
    assert retried.body.revision_id == latest.body.revision_id
    assert retried.body.job_id != latest.body.job_id
    article = repository.get(first.body.article_id)
    assert article.active_revision_id == first.body.revision_id
    assert article.status == "queued" and article.error is None
    with repository.driver.session(database=repository.database) as session:
        row = session.run(
            "MATCH (j:Job {job_id: $id}) RETURN j.status AS status, j.error_code AS error",
            id=str(latest.body.job_id),
        ).single(strict=True)
        assert row is not None and row["status"] == "failed"
        assert row["error"] == "internal_error"
    with pytest.raises(RegistrationError, match="retry_conflict"):
        service.retry(first.body.article_id)


@pytest.mark.integration
@pytest.mark.parametrize("state", ["queued", "processing", "completed"])
def test_retry_rejects_nonfailed(
    service: RegistrationService,
    repository: ArticleRepository,
    state: Literal["queued", "processing", "completed"],
) -> None:
    first = service.register_text(text(), app_id="test")
    if state != "queued":
        set_status(repository, first, state)
    before = counts(repository)
    with pytest.raises(RegistrationError) as error:
        service.retry(first.body.article_id)
    assert error.value.status_code == HTTPStatus.CONFLICT
    assert error.value.code == ErrorCode.RETRY_CONFLICT
    assert counts(repository) == before


@pytest.mark.integration
@pytest.mark.parametrize("keyed", [False, True])
def test_concurrent_primary_registration(
    service: RegistrationService, repository: ArticleRepository, keyed: bool
) -> None:
    # Exercise the first-ever lock creation race as well, on this isolated DB.
    with repository.driver.session(database=repository.database) as session:
        session.run("MATCH (l:RegistrationLock {name: 'uploads'}) DELETE l").consume()
    barrier = Barrier(6)
    content, key = text(), str(uuid4()) if keyed else None
    before = counts(repository)

    def upload(_: int) -> RegistrationResult:
        barrier.wait(timeout=10)
        return service.register_text(content, app_id="test", idempotency_key=key)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(upload, range(6)))
    assert len({result.body.article_id for result in results}) == 1
    assert len({result.body.job_id for result in results}) == 1
    assert sum(result.status_code == HTTPStatus.ACCEPTED for result in results) == (
        6 if keyed else 1
    )
    assert counts(repository) == tuple(
        value + delta
        for value, delta in zip(before, (1, 1, 1, 1, int(keyed)), strict=True)
    )


@pytest.mark.integration
def test_concurrent_updates_have_sequential_numbers(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    first = service.register_text(text(), app_id="test")
    set_status(repository, first, "completed")
    barrier = Barrier(6)

    def upload(_: int) -> RegistrationResult:
        barrier.wait(timeout=10)
        return service.register_text(
            text(), app_id="test", article_id=first.body.article_id
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(upload, range(6)))
    assert len({result.body.revision_id for result in results}) == 6
    with repository.driver.session(database=repository.database) as session:
        numbers = [
            row["number"]
            for row in session.run(
                "MATCH (r:ArticleRevision {article_id: $id}) RETURN r.number AS number ORDER BY number",
                id=str(first.body.article_id),
            )
        ]
    assert numbers == list(range(1, 8))
    assert (
        repository.get(first.body.article_id).active_revision_id
        == first.body.revision_id
    )


@pytest.mark.integration
def test_concurrent_retry_accepts_only_one(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    first = service.register_text(text(), app_id="test")
    set_status(repository, first, "failed")
    barrier = Barrier(6)
    before = counts(repository)

    def retry(_: int) -> HTTPStatus:
        barrier.wait(timeout=10)
        try:
            return service.retry(first.body.article_id).status_code
        except RegistrationError as error:
            assert error.code == ErrorCode.RETRY_CONFLICT
            return error.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(retry, range(6)))
    assert results.count(HTTPStatus.ACCEPTED) == 1
    assert results.count(HTTPStatus.CONFLICT) == 5
    assert counts(repository) == (
        before[0],
        before[1],
        before[2] + 1,
        before[3] + 1,
        before[4],
    )


@pytest.mark.integration
@pytest.mark.parametrize("updating", [False, True])
def test_registration_rolls_back_including_key_and_pointers(
    service: RegistrationService,
    repository: ArticleRepository,
    monkeypatch: pytest.MonkeyPatch,
    updating: bool,
) -> None:
    first = service.register_text(text(), app_id="test") if updating else None
    if first is not None:
        set_status(repository, first, "completed")
    before = counts(repository)
    original = ArticleRepository._remember

    def fail(
        tx: ManagedTransaction, request: UploadRequest, result: RegistrationResult
    ) -> None:
        original(tx, request, result)
        raise RuntimeError("Injected failure after all registration writes")

    with monkeypatch.context() as patch:
        patch.setattr(ArticleRepository, "_remember", staticmethod(fail))
        with pytest.raises(RuntimeError, match="Injected failure"):
            service.register_text(
                text(),
                app_id="test",
                idempotency_key=str(uuid4()),
                article_id=first.body.article_id if first else None,
            )
    assert counts(repository) == before
    if first is not None:
        article = repository.get(first.body.article_id)
        assert article.latest_revision_id == first.body.revision_id
        assert article.active_revision_id == first.body.revision_id
        assert article.status == "completed"


@pytest.mark.integration
def test_invalid_source_does_not_register(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    before = counts(repository)
    with pytest.raises(DocumentError):
        service.register_text("  ", app_id="test")
    assert counts(repository) == before


@pytest.mark.integration
def test_concurrent_same_key_different_payloads(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    barrier, key = Barrier(6), str(uuid4())
    before = counts(repository)

    def upload(_: int) -> HTTPStatus:
        content = text()
        barrier.wait(timeout=10)
        try:
            return service.register_text(
                content, app_id="test", idempotency_key=key
            ).status_code
        except RegistrationError as error:
            assert error.code == ErrorCode.IDEMPOTENCY_CONFLICT
            return error.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(upload, range(6)))
    assert results.count(HTTPStatus.ACCEPTED) == 1
    assert results.count(HTTPStatus.CONFLICT) == 5
    assert counts(repository) == tuple(value + 1 for value in before)


@pytest.mark.integration
def test_concurrent_identical_explicit_updates(
    service: RegistrationService, repository: ArticleRepository
) -> None:
    first = service.register_text(text(), app_id="test")
    barrier, content = Barrier(6), text()
    before = counts(repository)

    def update(_: int) -> RegistrationResult:
        barrier.wait(timeout=10)
        return service.register_text(
            content, app_id="test", article_id=first.body.article_id
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(update, range(6)))
    assert len({result.body.revision_id for result in results}) == 1
    assert sum(result.status_code == HTTPStatus.ACCEPTED for result in results) == 1
    assert counts(repository) == (
        before[0],
        before[1] + 1,
        before[2] + 1,
        before[3] + 1,
        before[4],
    )


@pytest.mark.integration
def test_retry_rollback_restores_failed_job_pointer(
    service: RegistrationService,
    repository: ArticleRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = service.register_text(text(), app_id="test")
    set_status(repository, first, "failed")
    before_article, before = repository.get(first.body.article_id), counts(repository)
    original = repository_module._job

    def fail(tx: ManagedTransaction, article_id: str, revision_id: str) -> None:
        original(tx, article_id, revision_id)
        raise RuntimeError("Injected retry failure")

    with monkeypatch.context() as patch:
        patch.setattr(repository_module, "_job", fail)
        with pytest.raises(RuntimeError, match="Injected retry failure"):
            service.retry(first.body.article_id)
    assert counts(repository) == before
    assert repository.get(first.body.article_id) == before_article
    assert service.retry(first.body.article_id).status_code == HTTPStatus.ACCEPTED
