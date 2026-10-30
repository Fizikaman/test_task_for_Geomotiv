"""Model readiness gates and safe automatic download failures, without paid APIs."""

import asyncio
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient
lazy from neo4j import Driver

import article_graph.app as app_module
import article_graph.model_init as init_module
from article_graph.embeddings import OllamaEmbeddings
from article_graph.settings import LLMSettings, Neo4jSettings, OllamaSettings, Settings


@pytest.fixture
def settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm_settings: LLMSettings
) -> Settings:
    monkeypatch.chdir(tmp_path)
    return Settings(
        _env_file=None,
        app_token="deployment-app-" * 4,
        jwt_access_key="deployment-access-" * 4,
        jwt_refresh_key="deployment-refresh-" * 4,
        llm=llm_settings,
        neo4j=Neo4jSettings(embedding_dimensions=3),
        ollama=OllamaSettings(
            base_url="http://ollama.test",
            model="test:v1",
            digest="a" * 64,
            dimensions=3,
        ),
    )


@pytest.mark.parametrize("ready", [True, False])
def test_api_readiness_requires_real_model_probe(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    def schema_ready(driver: Driver, database: Neo4jSettings) -> None:
        pass

    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                http_status.HTTP_200_OK,
                json={"models": [{"name": "test:v1", "digest": "a" * 64}]},
            )
        assert request.url.path == "/api/embed"
        return httpx.Response(
            http_status.HTTP_200_OK,
            json={
                "model": "test:v1",
                "embeddings": [[1.0, 0.0, 0.0] if ready else [1.0]],
            },
        )

    def adapter(model: OllamaSettings) -> OllamaEmbeddings:
        return OllamaEmbeddings(model, transport=httpx.MockTransport(response))

    monkeypatch.setattr(app_module, "require_ready", schema_ready)
    monkeypatch.setattr(app_module, "OllamaEmbeddings", adapter)
    app = app_module.create_app(settings)
    app.state.driver = object()
    client = TestClient(app)
    assert client.get("/health/ready").status_code == (
        http_status.HTTP_200_OK if ready else http_status.HTTP_503_SERVICE_UNAVAILABLE
    )
    assert client.get("/health/live").status_code == http_status.HTTP_200_OK


def test_failed_download_never_binds_or_reports_ready(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_type = httpx.AsyncClient

    def response(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/pull"
        return httpx.Response(
            http_status.HTTP_200_OK, json={"error": "download failed"}
        )

    def client(**kwargs: object) -> httpx.AsyncClient:
        return client_type(transport=httpx.MockTransport(response))

    def forbidden(database: Neo4jSettings) -> NoReturn:
        pytest.fail("A failed download must never bind an embedding space")

    monkeypatch.setattr(init_module.httpx, "AsyncClient", client)
    monkeypatch.setattr(init_module, "open_database", forbidden)
    assert settings.ollama is not None
    with pytest.raises(RuntimeError, match="model_download_failed"):
        asyncio.run(init_module.initialize(settings.ollama, settings.neo4j))
