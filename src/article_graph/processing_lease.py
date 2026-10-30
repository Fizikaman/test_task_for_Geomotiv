"""Database-time leases. Every mutation locks Article then Job before reading state."""

from uuid import UUID, uuid4

lazy from neo4j import Driver, ManagedTransaction, Record

from article_graph.pipeline_checkpoints import Attempt, PipelineError
from article_graph.settings import Limits, Neo4jSettings


class LeaseRepository:
    def __init__(
        self, driver: Driver, settings: Neo4jSettings, limits: Limits, seconds: int
    ) -> None:
        self.driver = driver
        self.database = settings.database
        self.limits = limits
        self.seconds = seconds

    def _state(self, tx: ManagedTransaction, job_id: UUID) -> Record | None:
        row = tx.run(
            "MATCH (j:Job {job_id: $job}) RETURN j.article_id AS article",
            job=str(job_id),
        ).single()
        if row is None:
            return None
        tx.run(
            "MATCH (a:Article {article_id: $article}) "
            "SET a._lease_lock = true REMOVE a._lease_lock",
            article=row["article"],
        ).consume()
        tx.run(
            "MATCH (j:Job {job_id: $job}) "
            "SET j._lease_lock = true REMOVE j._lease_lock",
            job=str(job_id),
        ).consume()
        return tx.run(
            "MATCH (a:Article)-[:HAS_REVISION]->(r:ArticleRevision)-[:HAS_JOB]->"
            "(j:Job {job_id: $job}) "
            "WHERE a.article_id = j.article_id AND r.article_id = j.article_id "
            "AND r.revision_id = j.revision_id AND r.job_id = j.job_id "
            "RETURN j.article_id AS article, j.revision_id AS revision, "
            "j.status AS status, coalesce(j.attempt, 0) AS attempt, "
            "j.attempt_token AS token, a.latest_revision_id AS latest, "
            "a.job_id AS latest_job, "
            "coalesce(j.lease_expires_at, 0) > "
            "datetime.realtime().epochMillis / 1000.0 AS live, "
            "coalesce(j.available_at, 0) <= "
            "datetime.realtime().epochMillis / 1000.0 AS ready",
            job=str(job_id),
        ).single()

    def claim(self, job_id: UUID) -> Attempt | None:
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._claim, job_id)

    def _claim(self, tx: ManagedTransaction, job_id: UUID) -> Attempt | None:
        row = self._state(tx, job_id)
        if row is None or row["status"] not in {"queued", "processing"}:
            return None
        if row["latest"] != row["revision"] or row["latest_job"] != str(job_id):
            tx.run(
                "MATCH (j:Job {job_id: $job}), "
                "(r:ArticleRevision {revision_id: $revision}) "
                "SET j.status = 'superseded', r.status = 'superseded', "
                "j.completed_at = datetime.realtime().epochMillis / 1000.0 "
                "REMOVE j.lease_expires_at",
                job=str(job_id),
                revision=row["revision"],
            ).consume()
            return None
        if row["status"] == "processing" and row["live"]:
            return None
        if not row["ready"]:
            return None
        if row["attempt"] >= self.limits.job_attempts:
            tx.run(
                "MATCH (j:Job {job_id: $job}), "
                "(r:ArticleRevision {revision_id: $revision}) "
                "SET j.status = 'failed', r.status = 'failed', "
                "j.error_code = 'processing_timeout', "
                "j.error_message = 'Processing attempts exhausted', "
                "j.completed_at = datetime.realtime().epochMillis / 1000.0 "
                "REMOVE j.lease_expires_at",
                job=str(job_id),
                revision=row["revision"],
            ).consume()
            return None
        attempt = Attempt(
            article_id=row["article"],
            revision_id=row["revision"],
            job_id=job_id,
            attempt=row["attempt"] + 1,
            attempt_token=uuid4(),
        )
        tx.run(
            "MATCH (j:Job {job_id: $job}), "
            "(r:ArticleRevision {revision_id: $revision}) "
            "SET j.status = 'processing', r.status = 'processing', "
            "j.attempt = $attempt, j.attempt_token = $token, "
            "j.heartbeat_at = datetime.realtime().epochMillis / 1000.0, "
            "j.lease_expires_at = datetime.realtime().epochMillis / 1000.0 + $seconds "
            "REMOVE j.error_code, j.error_message, j.error_stage, j.available_at",
            job=str(job_id),
            revision=str(attempt.revision_id),
            attempt=attempt.attempt,
            token=str(attempt.attempt_token),
            seconds=self.seconds,
        ).consume()
        return attempt

    def _owned(self, tx: ManagedTransaction, attempt: Attempt) -> bool:
        row = self._state(tx, attempt.job_id)
        return row is not None and (
            row["article"] == str(attempt.article_id)
            and row["revision"] == str(attempt.revision_id)
            and row["latest"] == str(attempt.revision_id)
            and row["latest_job"] == str(attempt.job_id)
            and row["status"] == "processing"
            and row["attempt"] == attempt.attempt
            and row["token"] == str(attempt.attempt_token)
            and row["live"] is True
        )

    def recover(self, job_id: UUID, redelivery_seconds: int) -> bool:
        """Recheck under worker locks; queue recovery without claiming an attempt."""
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._recover, job_id, redelivery_seconds)

    def _recover(
        self, tx: ManagedTransaction, job_id: UUID, redelivery_seconds: int
    ) -> bool:
        row = self._state(tx, job_id)
        if row is None or row["status"] not in {"queued", "processing"}:
            return False
        # Reuse claim's terminal transitions, without starting provider work.
        stale = row["latest"] != row["revision"] or row["latest_job"] != str(job_id)
        expired = row["status"] == "processing" and not row["live"]
        if stale or (row["attempt"] >= self.limits.job_attempts and expired):
            self._claim(tx, job_id)
            return False
        if row["status"] == "processing" and not expired:
            return False
        if not row["ready"]:
            return False
        due = tx.run(
            "MATCH (j:Job {job_id: $job}) WHERE "
            "($expired OR coalesce(j.last_dispatched_at, j.created_at, 0) "
            "+ $redelivery <= datetime.realtime().epochMillis / 1000.0) "
            "AND NOT EXISTS { MATCH (j)-[:HAS_OUTBOX]->(o:Outbox) "
            "WHERE o.status IN ['pending', 'dispatching'] } "
            "RETURN j.job_id AS id",
            job=str(job_id),
            expired=expired,
            redelivery=redelivery_seconds,
        ).single()
        if due is None:
            return False
        if row["attempt"] >= self.limits.job_attempts:
            self._claim(tx, job_id)
            return False
        delay = (
            self.limits.retry_backoff_seconds * 2 ** (row["attempt"] - 1)
            if expired
            else 0
        )
        tx.run(
            "MATCH (j:Job {job_id: $job}), "
            "(r:ArticleRevision {revision_id: $revision}) "
            "SET j.status = 'queued', r.status = 'queued', "
            "j.available_at = datetime.realtime().epochMillis / 1000.0 + $delay "
            "REMOVE j.lease_expires_at, j.attempt_token "
            "CREATE (o:Outbox {outbox_id: $outbox, job_id: $job, status: 'pending', "
            "available_at: j.available_at, "
            "created_at: datetime.realtime().epochMillis / 1000.0}) "
            "CREATE (j)-[:HAS_OUTBOX]->(o)",
            job=str(job_id),
            revision=row["revision"],
            outbox=str(uuid4()),
            delay=delay,
        ).consume()
        return True

    def heartbeat(self, attempt: Attempt) -> bool:
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._heartbeat, attempt)

    def _heartbeat(self, tx: ManagedTransaction, attempt: Attempt) -> bool:
        if not self._owned(tx, attempt):
            return False
        return (
            tx.run(
                "MATCH (j:Job {job_id: $job}) "
                "WHERE j.lease_expires_at > datetime.realtime().epochMillis / 1000.0 "
                "SET j.heartbeat_at = datetime.realtime().epochMillis / 1000.0, "
                "j.lease_expires_at = datetime.realtime().epochMillis / 1000.0 + $seconds "
                "RETURN j.job_id AS id",
                job=str(attempt.job_id),
                seconds=self.seconds,
            ).single()
            is not None
        )

    def fail(self, attempt: Attempt, error: PipelineError) -> int | None:
        """Return retry delay only after persisting a fenced retry and its outbox."""
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._fail, attempt, error)

    def _fail(
        self, tx: ManagedTransaction, attempt: Attempt, error: PipelineError
    ) -> int | None:
        if not self._owned(tx, attempt):
            return None
        retry = error.retryable and attempt.attempt < self.limits.job_attempts
        delay = self.limits.retry_backoff_seconds * 2 ** (attempt.attempt - 1)
        row = tx.run(
            "MATCH (j:Job {job_id: $job}), "
            "(r:ArticleRevision {revision_id: $revision}) "
            "WHERE j.lease_expires_at > datetime.realtime().epochMillis / 1000.0 "
            "SET j.status = $status, r.status = $status, j.error_code = $code, "
            "j.error_message = 'Processing failed', j.error_stage = $stage, "
            "j.available_at = CASE WHEN $retry THEN "
            "datetime.realtime().epochMillis / 1000.0 + $delay ELSE null END, "
            "j.completed_at = CASE WHEN $retry THEN null ELSE "
            "datetime.realtime().epochMillis / 1000.0 END "
            "REMOVE j.lease_expires_at RETURN j.job_id AS id",
            job=str(attempt.job_id),
            revision=str(attempt.revision_id),
            status="queued" if retry else "failed",
            code=error.code.value,
            stage=error.stage.value,
            retry=retry,
            delay=delay,
        ).single()
        if row is None or not retry:
            return None
        tx.run(
            "MATCH (j:Job {job_id: $job}) "
            "CREATE (o:Outbox {outbox_id: $outbox, job_id: $job, status: 'pending', "
            "available_at: j.available_at, "
            "created_at: datetime.realtime().epochMillis / 1000.0}) "
            "CREATE (j)-[:HAS_OUTBOX]->(o)",
            job=str(attempt.job_id),
            outbox=str(uuid4()),
        ).consume()
        return delay
