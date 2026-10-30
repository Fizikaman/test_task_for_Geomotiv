"""Opt-in infrastructure, hardcoded to this project's dedicated local test endpoint."""

import os
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
lazy from neo4j import Driver

from article_graph.db.database import open_database
from article_graph.db.schema import init_schema
from article_graph.settings import LLMSettings, Neo4jSettings


@pytest.fixture
def llm_settings() -> LLMSettings:
    return LLMSettings(
        base_url="https://provider.test/v1",
        api_key=SecretStr("test-key-only"),
        text_model="test-text-model",
        output_mode="json_object",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--neo4j", action="store_true", help="Use isolated test Compose")
    parser.addoption(
        "--infrastructure", action="store_true", help="Run isolated P20 Compose suite"
    )

    parser.addoption("--e2e", action="store_true", help="Run isolated P21 HTTP E2E")
    parser.addoption(
        "--live-provider", action="store_true", help="P22: explicitly allow paid smoke"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # Skip before fixtures/settings can read the separate live key or open sockets.
    if os.environ.get("CI") or not config.getoption("--live-provider"):
        skip = pytest.mark.skip(reason="P22 live smoke disabled (also always in CI)")
        for item in items:
            if "live_provider" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def neo4j_settings(request: pytest.FixtureRequest) -> Neo4jSettings:
    if not request.config.getoption("--neo4j"):
        pytest.skip("Start docker-compose.test.yml and pass --neo4j")
    return Neo4jSettings(
        uri="bolt://127.0.0.1:17687",
        username="neo4j",
        password=SecretStr("geomotiv-isolated-test-only"),
        database="neo4j",
        embedding_dimensions=1024,
        schema_timeout_seconds=60,
    )


@pytest.fixture(scope="session")
def neo4j_driver(neo4j_settings: Neo4jSettings) -> Iterator[Driver]:
    with open_database(neo4j_settings) as driver:
        init_schema(driver, neo4j_settings)
        yield driver
