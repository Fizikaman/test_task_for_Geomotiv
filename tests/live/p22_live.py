"""Opt-in live harness. Imported only after the P22 consent gate."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override

from fastapi import status as http_status
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
lazy import httpx
lazy from fastapi.testclient import TestClient
lazy from neo4j import Driver

from article_graph.ai.embeddings import open_embeddings
from article_graph.ai.llm import ChatCompletionsLLM
from article_graph.app import create_app
from article_graph.db.candidate_repository import Neo4jCandidates
from article_graph.db.database import open_database
from article_graph.db.pipeline_checkpoints import CheckpointRepository
from article_graph.db.processing_lease import LeaseRepository
from article_graph.db.publication import GraphPublisher
from article_graph.db.schema import init_schema, validate_embedding
from article_graph.documents.blob_store import LocalBlobStore
from article_graph.domain import Character, Page, ProcessingResponse, Relation
from article_graph.processing.pipeline import RevisionPipeline
from article_graph.processing.worker import execute_job
from article_graph.settings import (
    Limits,
    LLMSettings,
    Neo4jSettings,
    OllamaSettings,
    Settings,
)
from tests.live.p22_fixture import Sample, check_graph


class LiveOllamaSettings(OllamaSettings):
    model_config = SettingsConfigDict(
        env_prefix="P22_OLLAMA__",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class LiveSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="P22_",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
    )
    llm: LLMSettings
    ollama: LiveOllamaSettings


class ObservedProvider(httpx.AsyncBaseTransport):
    """Inspect outgoing capability fields, then use the real network transport."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.transport = httpx.AsyncHTTPTransport(retries=0)
        self.text_calls = 0
        self.vision_calls = 0

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == self.settings.output_mode
        images = any(
            block.get("type") == "image_url"
            for message in payload["messages"]
            if isinstance(message.get("content"), list)
            for block in message["content"]
        )
        if images:
            assert payload["model"] == self.settings.vision_model
            self.vision_calls += 1
        else:
            assert payload["model"] == self.settings.text_model
            self.text_calls += 1
        return await self.transport.handle_async_request(request)

    @override
    async def aclose(self) -> None:
        await self.transport.aclose()


async def process(
    config: Settings,
    live: LiveSettings,
    driver: Driver,
    uploaded: ProcessingResponse,
    observer: ObservedProvider,
) -> None:
    leases = LeaseRepository(driver, config.neo4j, config.limits, 120)

    @asynccontextmanager
    async def factory() -> AsyncGenerator[RevisionPipeline]:
        llm = ChatCompletionsLLM(live.llm, config.limits, transport=observer)
        try:
            async with open_embeddings(live.ollama, driver, config.neo4j) as embeddings:
                yield RevisionPipeline(
                    checkpoints=CheckpointRepository(driver, config.neo4j),
                    publisher=GraphPublisher(driver, config.neo4j, config.limits),
                    store=LocalBlobStore(config.blob_root, max_bytes=64_000_000),
                    llm=llm,
                    embeddings=embeddings,
                    candidates=Neo4jCandidates(driver, config.neo4j),
                    llm_settings=live.llm,
                    embedding_settings=live.ollama,
                    limits=config.limits,
                )
        finally:
            await llm.aclose()

    delay = await execute_job(
        uploaded.job_id,
        leases,
        factory,
        heartbeat_seconds=15,
        processing_seconds=config.limits.processing_seconds,
    )
    assert delay is None, "live provider requested retry; smoke does not retry"


def run_sample(sample: Sample, root: Path) -> None:
    live = LiveSettings()  # Environment only; never loads the working .env.
    assert live.llm.vision_model, "P22 requires an explicit vision model"
    database = Neo4jSettings(
        uri="bolt://127.0.0.1:47687",
        username="neo4j",
        password=SecretStr("geomotiv-p22-synthetic-only"),
        database="neo4j",
        embedding_dimensions=live.ollama.dimensions,
    )
    config = Settings(
        _env_file=None,
        app_token=SecretStr("p22-synthetic-app-token-at-least-32-characters"),
        jwt_access_key=SecretStr("p22-synthetic-access-key-at-least-32-characters"),
        jwt_refresh_key=SecretStr("p22-synthetic-refresh-key-at-least-32-characters"),
        app_id="p22-synthetic",
        blob_root=root,
        neo4j=database,
        llm=live.llm,
        ollama=live.ollama,
        limits=Limits(provider_timeout_seconds=60, processing_seconds=300),
    )
    with open_database(database) as driver:
        with driver.session(database=database.database) as session:
            row = session.run("MATCH (n) RETURN count(n) AS count").single(strict=True)
            assert row is not None and row["count"] == 0, "P22 DB must be empty"
        # Only this fixed, dedicated endpoint is ever mutated. No concurrent runs.
        try:
            init_schema(driver, database)
            with TestClient(create_app(config)) as client:
                token = client.post(
                    "/auth/token",
                    json={"app_token": config.app_token.get_secret_value()},
                )
                assert token.status_code == http_status.HTTP_200_OK
                client.headers["Authorization"] = (
                    f"Bearer {token.json()['access_token']}"
                )
                response = client.post(
                    "/articles/upload",
                    files={"file": (sample.name, sample.data, sample.content_type)},
                )
                assert response.status_code == http_status.HTTP_202_ACCEPTED
                item = ProcessingResponse.model_validate(response.json())
                observer = ObservedProvider(live.llm)
                asyncio.run(process(config, live, driver, item, observer))
                state = client.get(item.status_url)
                assert state.status_code == http_status.HTTP_200_OK
                assert state.json()["status"] == "completed", "live processing failed"
                assert state.json()["active_revision_id"] == str(item.revision_id)
                if sample.visual_page is None:
                    assert observer.text_calls > 0 and observer.vision_calls == 0
                else:
                    assert observer.vision_calls >= sample.visual_page

            # New API lifecycle/driver reads durable results after processing closes.
            with TestClient(create_app(config)) as reader:
                reader.headers["Authorization"] = client.headers["Authorization"]
                response = reader.get(f"/characters/by_article/{item.article_id}")
                assert response.status_code == http_status.HTTP_200_OK
                characters = Page[Character].model_validate(response.json()).items
                relations: dict[str, Relation] = {}
                for character in characters:
                    response = reader.get(
                        f"/relations/connected/{character.character_id}"
                    )
                    assert response.status_code == http_status.HTTP_200_OK
                    for relation in (
                        Page[Relation].model_validate(response.json()).items
                    ):
                        assert relation.article_id == item.article_id
                        assert relation.revision_id == item.revision_id
                        relations[str(relation.relation_id)] = relation
                check_graph(sample, characters, tuple(relations.values()))
            with driver.session(database=database.database) as session:
                rows = list(
                    session.run(
                        "MATCH (c:Character) RETURN c.embedding AS vector, "
                        "c.embedding_digest AS digest"
                    )
                )
                assert len(rows) == len(characters)
                for row in rows:
                    validate_embedding(row["vector"], live.ollama.dimensions)
                    assert row["digest"] == live.ollama.digest
        finally:
            with driver.session(database=database.database) as session:
                session.run("MATCH (n) DETACH DELETE n").consume()
