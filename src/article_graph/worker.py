"""Explicit Celery registration; all I/O resources are task-local, after fork."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from pathlib import Path
from typing import Protocol
from uuid import UUID

from celery import Celery, Task
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
lazy from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from article_graph.domain import ErrorCode
from article_graph.pipeline_checkpoints import (
    Attempt,
    CheckpointRepository,
    PipelineError,
    Stage,
)
from article_graph.processing_lease import LeaseRepository
from article_graph.publication import PublicationResult
from article_graph.settings import Limits, LLMSettings, Neo4jSettings, OllamaSettings
lazy from article_graph.blob_store import LocalBlobStore
lazy from article_graph.candidate_repository import Neo4jCandidates
lazy from article_graph.database import open_database
lazy from article_graph.embeddings import EmbeddingError, open_embeddings
lazy from article_graph.llm import ChatCompletionsLLM
lazy from article_graph.pipeline import RevisionPipeline
lazy from article_graph.publication import GraphPublisher

TASK_NAME = "article_graph.process_job"


class WorkerConfig(BaseModel):
    broker_url: SecretStr = Field(min_length=1)
    blob_root: Path
    lease_seconds: int = Field(default=120, gt=0)
    heartbeat_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_lease(self) -> WorkerConfig:
        if not self.broker_url.get_secret_value().strip():
            raise ValueError("broker URL must not be blank")
        if self.lease_seconds < self.heartbeat_seconds * 4:
            raise ValueError("lease must cover at least four heartbeat intervals")
        return self


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARTICLE_GRAPH_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    worker: WorkerConfig
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    llm: LLMSettings
    ollama: OllamaSettings
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def coherent_budgets(self) -> WorkerSettings:
        if self.ollama.dimensions != self.neo4j.embedding_dimensions:
            raise ValueError("embedding dimensions must match Neo4j")
        if self.limits.provider_timeout_seconds > self.limits.processing_seconds:
            raise ValueError("provider timeout must fit within processing budget")
        return self


class PipelineRunner(Protocol):
    async def run(self, attempt: Attempt) -> PublicationResult: ...


type PipelineFactory = Callable[[], AbstractAsyncContextManager[PipelineRunner]]


async def execute_job(
    job_id: UUID,
    leases: LeaseRepository,
    factory: PipelineFactory,
    *,
    heartbeat_seconds: float,
    processing_seconds: int,
) -> int | None:
    attempt = await asyncio.to_thread(leases.claim, job_id)
    if attempt is None:
        return None

    async def work() -> None:
        async with asyncio.timeout(processing_seconds):
            async with factory() as pipeline:
                await pipeline.run(attempt)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            if not await asyncio.to_thread(leases.heartbeat, attempt):
                return

    task = asyncio.create_task(work())
    pulse = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait((task, pulse), return_when=asyncio.FIRST_COMPLETED)
        if pulse in done:
            # Unknown/lost ownership: stop provider work, leave recovery to P16.
            # Retrieving the exception avoids leaking DB error text into Celery logs.
            if not pulse.cancelled():
                pulse.exception()
            return None
        try:
            task.result()
        except PipelineError as error:
            return await asyncio.to_thread(leases.fail, attempt, error)
        except TimeoutError:
            return await asyncio.to_thread(
                leases.fail,
                attempt,
                PipelineError(
                    ErrorCode.PROCESSING_TIMEOUT, Stage.PREPARATION, retryable=True
                ),
            )
        except EmbeddingError as error:
            return await asyncio.to_thread(
                leases.fail,
                attempt,
                PipelineError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    Stage.PREPARATION,
                    retryable=error.retryable,
                ),
            )
        except ServiceUnavailable, SessionExpired:
            return await asyncio.to_thread(
                leases.fail,
                attempt,
                PipelineError(
                    ErrorCode.SERVICE_UNAVAILABLE, Stage.PREPARATION, retryable=True
                ),
            )
        except Neo4jError as error:
            return await asyncio.to_thread(
                leases.fail,
                attempt,
                PipelineError(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    Stage.PREPARATION,
                    retryable=error.is_retryable(),
                ),
            )
        except Exception:
            return await asyncio.to_thread(
                leases.fail,
                attempt,
                PipelineError(ErrorCode.INTERNAL_ERROR, Stage.PREPARATION),
            )
        return None
    finally:
        task.cancel()
        pulse.cancel()
        for pending in (task, pulse):
            with suppress(asyncio.CancelledError, Exception):
                await pending


@asynccontextmanager
async def open_pipeline(
    settings: WorkerSettings,
    leases: LeaseRepository,
) -> AsyncGenerator[PipelineRunner]:
    llm = ChatCompletionsLLM(settings.llm, settings.limits)
    try:
        async with open_embeddings(
            settings.ollama, leases.driver, settings.neo4j
        ) as embeddings:
            yield RevisionPipeline(
                checkpoints=CheckpointRepository(leases.driver, settings.neo4j),
                publisher=GraphPublisher(
                    leases.driver, settings.neo4j, settings.limits
                ),
                store=LocalBlobStore(
                    settings.worker.blob_root,
                    max_bytes=settings.limits.llm_total_input_bytes * 4,
                    limits=settings.limits,
                ),
                llm=llm,
                embeddings=embeddings,
                candidates=Neo4jCandidates(leases.driver, settings.neo4j),
                llm_settings=settings.llm,
                embedding_settings=settings.ollama,
                limits=settings.limits,
            )
    finally:
        await llm.aclose()


def create_app(settings: WorkerSettings) -> Celery:
    app = Celery("article_graph", broker=settings.worker.broker_url.get_secret_value())
    hard_limit = settings.limits.processing_seconds + 90
    app.conf.update(
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=settings.limits.processing_seconds + 60,
        task_time_limit=hard_limit,
        broker_transport_options={
            "visibility_timeout": hard_limit + settings.worker.lease_seconds + 60
        },
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        task_store_errors_even_if_ignored=False,
        task_remote_tracebacks=False,
        worker_redirect_stdouts=False,
        task_send_sent_event=False,
        task_default_queue="article_graph",
        broker_connection_retry_on_startup=True,
        task_publish_retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 1,
            "interval_max": 2,
        },
    )

    # lazy=False makes registration happen now, before worker consumes messages.
    @app.task(
        bind=True,
        name=TASK_NAME,
        lazy=False,
        shared=False,
        max_retries=settings.limits.job_attempts - 1,
    )
    def process_job(task: Task, job_id: str) -> None:
        try:
            parsed = UUID(job_id)
        except ValueError:
            return
        try:
            with open_database(settings.neo4j) as driver:
                leases = LeaseRepository(
                    driver,
                    settings.neo4j,
                    settings.limits,
                    settings.worker.lease_seconds,
                )
                delay = asyncio.run(
                    execute_job(
                        parsed,
                        leases,
                        lambda: open_pipeline(settings, leases),
                        heartbeat_seconds=settings.worker.heartbeat_seconds,
                        processing_seconds=settings.limits.processing_seconds,
                    )
                )
        except Exception:
            # No raw infrastructure errors or credentials in task exceptions.
            raise task.retry(
                exc=RuntimeError("Worker infrastructure unavailable"),
                countdown=settings.limits.retry_backoff_seconds,
            ) from None
        if delay is not None:
            raise task.retry(countdown=delay)

    # Eager registration, independent of lazy pipeline/provider imports.
    return app
