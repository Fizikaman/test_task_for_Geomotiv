"""Durable registration only. No broker, HTTP handler or processing side effects."""

from http import HTTPStatus
from time import time
from uuid import UUID, uuid4

lazy from neo4j import Driver, ManagedTransaction

from article_graph.domain import Article, ErrorCode, ProcessingResponse
from article_graph.processing.registration import (
    RegistrationError,
    RegistrationResult,
    UploadRequest,
)
from article_graph.settings import Neo4jSettings


def _lock(tx: ManagedTransaction) -> None:
    # The unique singleton protects absence queries as well as existing articles.
    # Every registration/retry acquires it before any other lock. A separate query
    # reads state AFTER the exclusive lock; MERGE alone is insufficient.
    tx.run(
        "MERGE (l:RegistrationLock {name: 'uploads'}) SET l._lock = true REMOVE l._lock"
    ).consume()


def _article(tx: ManagedTransaction, article_id: str) -> Article:
    # Future publication must use this Article node lock too; latest/active cannot
    # be read before taking it. The registration mutex alone does not guard workers.
    row = tx.run(
        "MATCH (a:Article {article_id: $id}) "
        "SET a._registration_lock = true REMOVE a._registration_lock "
        "RETURN a.article_id AS id",
        id=article_id,
    ).single()
    if row is None:
        raise RegistrationError(ErrorCode.NOT_FOUND)
    row = tx.run(
        "MATCH (a:Article {article_id: $id}), (j:Job {job_id: a.job_id}) "
        "RETURN a.article_id AS article_id, a.latest_revision_id AS latest_revision_id, "
        "a.active_revision_id AS active_revision_id, j.job_id AS job_id, "
        "j.status AS status, j.error_code AS error_code, j.error_message AS error_message",
        id=article_id,
    ).single(strict=True)
    if row is None:
        raise RuntimeError("Article registration state is missing")
    return Article.model_validate(
        {
            **{
                key: row[key]
                for key in (
                    "article_id",
                    "latest_revision_id",
                    "active_revision_id",
                    "job_id",
                    "status",
                )
            },
            "error": (
                {"code": row["error_code"], "message": row["error_message"]}
                if row["status"] == "failed"
                else None
            ),
        }
    )


def _response(article: Article, *, unchanged: bool) -> RegistrationResult:
    return RegistrationResult(
        status_code=HTTPStatus.OK if unchanged else HTTPStatus.ACCEPTED,
        body=ProcessingResponse(
            article_id=article.article_id,
            revision_id=article.latest_revision_id,
            job_id=article.job_id,
            status=article.status,
            status_url=f"/articles/{article.article_id}",
            unchanged=unchanged,
        ),
    )


def _job(tx: ManagedTransaction, article_id: str, revision_id: str) -> None:
    # Kept in the caller's managed transaction, including all pointer updates.
    tx.run(
        "MATCH (a:Article {article_id: $article}), "
        "(r:ArticleRevision {revision_id: $revision}) "
        "CREATE (j:Job {job_id: $job, article_id: $article, revision_id: $revision, "
        "status: 'queued', attempt: 0, created_at: $now}) "
        "CREATE (o:Outbox {outbox_id: $outbox, job_id: $job, status: 'pending', "
        "available_at: $now, created_at: $now}) "
        "CREATE (r)-[:HAS_JOB]->(j) CREATE (j)-[:HAS_OUTBOX]->(o) "
        "SET a.job_id = $job, r.job_id = $job",
        article=article_id,
        revision=revision_id,
        job=str(uuid4()),
        outbox=str(uuid4()),
        now=time(),
    ).consume()


class ArticleRepository:
    def __init__(self, driver: Driver, settings: Neo4jSettings) -> None:
        self.driver = driver
        self.database = settings.database

    def register(self, request: UploadRequest) -> RegistrationResult:
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._register, request)

    def _register(
        self, tx: ManagedTransaction, request: UploadRequest
    ) -> RegistrationResult:
        _lock(tx)
        if request.idempotency_key is not None:
            replay = tx.run(
                "MATCH (i:IdempotencyRecord {app_id: $app, key: $key}) "
                "RETURN i.fingerprint AS fingerprint, i.result AS result",
                app=request.app_id,
                key=request.idempotency_key,
            ).single()
            if replay is not None:
                if replay["fingerprint"] != request.fingerprint():
                    raise RegistrationError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return RegistrationResult.model_validate_json(replay["result"])

        article_id = str(request.article_id) if request.article_id else None
        if article_id is None:
            match = tx.run(
                "MATCH (a:Article {latest_source_hash: $hash}) "
                "RETURN a.article_id AS id ORDER BY id LIMIT 1",
                hash=request.content_hash,
            ).single()
            if match is not None:
                article_id = match["id"]

        if article_id is not None:
            article = _article(tx, article_id)
            row = tx.run(
                "MATCH (a:Article {article_id: $id}) "
                "RETURN a.latest_source_hash AS hash, a.latest_revision_number AS number",
                id=article_id,
            ).single(strict=True)
            if row is None:
                raise RuntimeError("Article registration state is missing")
            if row["hash"] == request.content_hash:
                result = _response(article, unchanged=True)
                self._remember(tx, request, result)
                return result
            number = row["number"] + 1
        else:
            article_id = str(uuid4())
            number = 1
            tx.run(
                "CREATE (:Article {article_id: $id, created_at: $now})",
                id=article_id,
                now=time(),
            ).consume()

        revision_id = str(uuid4())
        tx.run(
            "MATCH (a:Article {article_id: $article}) "
            "CREATE (r:ArticleRevision {article_id: $article, revision_id: $revision, "
            "number: $number, content_hash: $hash, source_hash: $hash, "
            "source_namespace: 'sources', source_size: $size, format: $format, "
            "content_type: $content_type, input_method: $method, created_at: $now}) "
            "CREATE (a)-[:HAS_REVISION]->(r) "
            "SET a.latest_revision_id = $revision, a.latest_revision_number = $number, "
            "a.latest_source_hash = $hash",
            article=article_id,
            revision=revision_id,
            number=number,
            hash=request.content_hash,
            size=request.size,
            format=request.format.value,
            content_type=request.content_type,
            method=request.method,
            now=time(),
        ).consume()
        _job(tx, article_id, revision_id)
        result = _response(_article(tx, article_id), unchanged=False)
        self._remember(tx, request, result)
        return result

    @staticmethod
    def _remember(
        tx: ManagedTransaction, request: UploadRequest, result: RegistrationResult
    ) -> None:
        if request.idempotency_key is not None:
            tx.run(
                "CREATE (:IdempotencyRecord {idempotency_id: $id, app_id: $app, "
                "key: $key, fingerprint: $fingerprint, result: $result})",
                id=str(uuid4()),
                app=request.app_id,
                key=request.idempotency_key,
                fingerprint=request.fingerprint(),
                result=result.model_dump_json(),
            ).consume()

    def retry(self, article_id: UUID) -> RegistrationResult:
        def transaction(tx: ManagedTransaction) -> RegistrationResult:
            _lock(tx)
            article = _article(tx, str(article_id))
            if article.status != "failed":
                raise RegistrationError(ErrorCode.RETRY_CONFLICT)
            _job(tx, str(article_id), str(article.latest_revision_id))
            return _response(_article(tx, str(article_id)), unchanged=False)

        with self.driver.session(database=self.database) as session:
            return session.execute_write(transaction)

    def get(self, article_id: UUID) -> Article:
        # A short write transaction takes the same Article lock so status and
        # latest/active pointers form one consistent view, including during retry.
        with self.driver.session(database=self.database) as session:
            return session.execute_write(_article, str(article_id))
