"""Independent Outbox process: python -m article_graph.dispatcher."""

import logging
import signal
from threading import Event
from types import FrameType
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
lazy from celery import Celery

from article_graph.outbox import OutboxRepository
from article_graph.settings import Limits, Neo4jSettings
lazy from article_graph.database import open_database
lazy from article_graph.worker import TASK_NAME

logger = logging.getLogger(__name__)


class DispatcherConfig(BaseModel):
    broker_url: SecretStr = Field(min_length=1)
    claim_seconds: int = Field(default=60, gt=0)
    redelivery_seconds: int = Field(default=600, gt=0)
    poll_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    batch_size: int = Field(default=100, gt=0, le=1000)
    retry_seconds: int = Field(default=2, gt=0)
    retry_max_seconds: int = Field(default=60, gt=0)

    @model_validator(mode="after")
    def valid_config(self) -> DispatcherConfig:
        if not self.broker_url.get_secret_value().strip():
            raise ValueError("broker URL must not be blank")
        if self.retry_max_seconds < self.retry_seconds:
            raise ValueError("retry maximum must cover initial delay")
        return self


class DispatcherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARTICLE_GRAPH_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    dispatcher: DispatcherConfig
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    limits: Limits = Field(default_factory=Limits)


class Broker(Protocol):
    def publish(self, job_id: UUID) -> None: ...


class CeleryBroker:
    def __init__(self, config: DispatcherConfig) -> None:
        self.app = Celery(
            "article_graph_dispatcher", broker=config.broker_url.get_secret_value()
        )
        self.app.conf.update(
            task_default_queue="article_graph",
            task_serializer="json",
            task_ignore_result=True,
            task_publish_retry=False,
            broker_connection_timeout=5,
            broker_transport_options={
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
                "retry_on_timeout": False,
                "max_retries": 0,
            },
        )

    def publish(self, job_id: UUID) -> None:
        self.app.send_task(TASK_NAME, args=[str(job_id)], retry=False)

    def close(self) -> None:
        self.app.close()


class Dispatcher:
    def __init__(
        self, repository: OutboxRepository, broker: Broker, config: DispatcherConfig
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.config = config

    def tick(self) -> int:
        self.repository.recover(self.config.batch_size, self.config.redelivery_seconds)
        sent = 0
        for _ in range(self.config.batch_size):
            delivery = self.repository.claim(self.config.claim_seconds)
            if delivery is None:
                break
            try:
                self.broker.publish(delivery.job_id)
            except Exception:
                # An ambiguous publish is safe to repeat; never log broker credentials.
                delay = min(
                    self.config.retry_max_seconds,
                    self.config.retry_seconds * 2 ** min(delivery.attempt - 1, 16),
                )
                self.repository.finish(delivery, sent=False, delay=delay)
                logger.warning(
                    "outbox_publish_failed outbox_id=%s job_id=%s",
                    delivery.outbox_id,
                    delivery.job_id,
                )
                break
            if self.repository.finish(delivery, sent=True):
                sent += 1
        return sent


def main() -> None:
    settings = DispatcherSettings()
    stopped = Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logging.basicConfig(level=logging.INFO)
    broker = CeleryBroker(settings.dispatcher)
    try:
        while not stopped.is_set():
            try:
                with open_database(settings.neo4j) as driver:
                    dispatcher = Dispatcher(
                        OutboxRepository(driver, settings.neo4j, settings.limits),
                        broker,
                        settings.dispatcher,
                    )
                    while not stopped.is_set():
                        dispatcher.tick()
                        stopped.wait(settings.dispatcher.poll_seconds)
            except Exception:
                logger.warning("dispatcher_storage_unavailable")
                stopped.wait(settings.dispatcher.retry_max_seconds)
    finally:
        broker.close()


if __name__ == "__main__":
    main()
