"""Revision-owned checkpoint pointers; blobs are published before DB pointers."""

import json
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field
lazy from neo4j import Driver, ManagedTransaction

from article_graph.document_format import DocumentFormat
from article_graph.domain import DTO, ContentHash, ErrorCode
from article_graph.publication import PublicationResult
from article_graph.settings import Neo4jSettings


class Stage(StrEnum):
    PREPARATION = "preparation"
    EXTRACTION = "extraction"
    RESOLUTION = "resolution"
    PUBLICATION = "publication"


class PipelineError(Exception):
    def __init__(
        self, code: ErrorCode, stage: Stage, *, retryable: bool = False
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.stage = stage
        self.retryable = retryable


class PipelineStopped(Exception):
    def __init__(self, result: PublicationResult) -> None:
        super().__init__(result.value)
        self.result = result


class Attempt(DTO):
    article_id: UUID
    revision_id: UUID
    job_id: UUID
    attempt: int = Field(gt=0)
    attempt_token: UUID = Field(repr=False)


class Pointer(DTO):
    key: ContentHash
    blob_hash: ContentHash


class RevisionInput(DTO):
    revision_id: UUID
    content_hash: ContentHash
    source_hash: ContentHash
    source_namespace: Literal["sources"]
    source_size: int = Field(gt=0)
    format: DocumentFormat
    checkpoints: dict[Stage, Pointer] = Field(default_factory=dict)


class CheckpointRepository:
    def __init__(self, driver: Driver, settings: Neo4jSettings) -> None:
        self.driver = driver
        self.database = settings.database

    def check(self, attempt: Attempt) -> RevisionInput:
        with self.driver.session(database=self.database) as session:
            result, source = session.execute_write(self._check, attempt)
        if result is not None:
            raise PipelineStopped(result)
        assert source is not None
        return source

    def _check(
        self, tx: ManagedTransaction, attempt: Attempt
    ) -> tuple[PublicationResult | None, RevisionInput | None]:
        # Same lock order as registration/publication and the future lease owner.
        tx.run(
            "MATCH (a:Article {article_id: $article}) "
            "SET a._pipeline_lock = true REMOVE a._pipeline_lock",
            article=str(attempt.article_id),
        ).consume()
        tx.run(
            "MATCH (j:Job {job_id: $job}) "
            "SET j._pipeline_lock = true REMOVE j._pipeline_lock",
            job=str(attempt.job_id),
        ).consume()
        row = tx.run(
            "MATCH (a:Article {article_id: $article})-[:HAS_REVISION]->"
            "(r:ArticleRevision {revision_id: $revision})-[:HAS_JOB]->"
            "(j:Job {job_id: $job}) "
            "WHERE r.article_id = $article AND j.article_id = $article "
            "AND j.revision_id = $revision AND r.job_id = $job "
            "RETURN a.latest_revision_id AS latest, a.job_id AS latest_job, "
            "j.status AS status, j.attempt AS attempt, j.attempt_token AS token, "
            "j.lease_expires_at > datetime.realtime().epochMillis / 1000.0 AS live, "
            "r.revision_id AS revision_id, r.content_hash AS content_hash, "
            "r.source_hash AS source_hash, "
            "r.source_namespace AS source_namespace, r.source_size AS source_size, "
            "r.format AS format, r.pipeline_checkpoints AS checkpoints",
            article=str(attempt.article_id),
            revision=str(attempt.revision_id),
            job=str(attempt.job_id),
        ).single()
        if (
            row is None
            or row["attempt"] != attempt.attempt
            or row["token"] != str(attempt.attempt_token)
        ):
            raise PipelineError(ErrorCode.INTERNAL_ERROR, Stage.PREPARATION)
        if row["status"] in {"completed", "superseded"}:
            return PublicationResult(row["status"]), None
        if row["status"] != "processing" or row["live"] is not True:
            raise PipelineError(ErrorCode.INTERNAL_ERROR, Stage.PREPARATION)
        if row["latest"] != str(attempt.revision_id):
            tx.run(
                "MATCH (r:ArticleRevision {revision_id: $revision}), "
                "(j:Job {job_id: $job}) SET r.status = 'superseded', "
                "j.status = 'superseded', "
                "j.completed_at = datetime.realtime().epochMillis / 1000.0 "
                "REMOVE j.lease_expires_at",
                revision=str(attempt.revision_id),
                job=str(attempt.job_id),
            ).consume()
            return PublicationResult.SUPERSEDED, None
        if row["latest_job"] != str(attempt.job_id):
            raise PipelineError(ErrorCode.INTERNAL_ERROR, Stage.PREPARATION)
        try:
            checkpoints: dict[Stage, Pointer] = {
                Stage(key): Pointer.model_validate(value)
                for key, value in json.loads(row["checkpoints"] or "{}").items()
            }
        except ValueError, AttributeError, TypeError:
            # Invalid metadata is a cache miss, never authority for provider I/O.
            checkpoints = {}
        return None, RevisionInput.model_validate(
            {
                **{key: row[key] for key in RevisionInput.model_fields},
                "checkpoints": checkpoints,
            }
        )

    def save(self, attempt: Attempt, stage: Stage, pointer: Pointer | None) -> None:
        with self.driver.session(database=self.database) as session:
            result = session.execute_write(self._save, attempt, stage, pointer)
        if result is not None:
            raise PipelineStopped(result)

    def _save(
        self,
        tx: ManagedTransaction,
        attempt: Attempt,
        stage: Stage,
        pointer: Pointer | None,
    ) -> PublicationResult | None:
        result, source = self._check(tx, attempt)
        if result is not None:
            return result
        assert source is not None
        pointers = dict(source.checkpoints)
        if pointer is None:
            pointers.pop(stage, None)
        else:
            pointers[stage] = pointer
        row = tx.run(
            "MATCH (r:ArticleRevision {revision_id: $revision}), "
            "(j:Job {job_id: $job}) "
            "WHERE j.lease_expires_at > datetime.realtime().epochMillis / 1000.0 "
            "SET r.pipeline_checkpoints = $checkpoints RETURN r.revision_id AS id",
            revision=str(attempt.revision_id),
            job=str(attempt.job_id),
            checkpoints=json.dumps(
                {key: value.model_dump() for key, value in pointers.items()},
                sort_keys=True,
            ),
        ).single()
        if row is None:
            raise PipelineError(ErrorCode.INTERNAL_ERROR, stage)
        return None
