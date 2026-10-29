import asyncio
import json
import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from subprocess import run
from threading import Barrier
from typing import override
from uuid import uuid4

import httpx
import pytest
from fastapi import status as http_status
from pydantic import ValidationError
lazy from neo4j import Driver, Session

from article_graph.embeddings import (
    EmbeddingError,
    EmbeddingMetadata,
    OllamaEmbeddings,
    bind_embedding_space,
    open_embeddings,
)
from article_graph.schema import VECTOR_INDEX, require_ready
from article_graph.settings import LLMSettings, Neo4jSettings, OllamaSettings, Settings

DIGEST = "a" * 64


def config(**overrides: object) -> OllamaSettings:
    return OllamaSettings.model_validate(
        {
            "base_url": "http://ollama.test",
            "model": "bge-m3:latest",
            "digest": DIGEST,
            "dimensions": 3,
            **overrides,
        }
    )


def response(request: httpx.Request, dimensions: int = 3) -> httpx.Response:
    if request.url.path == "/api/tags":
        assert request.method == "GET"
        return httpx.Response(
            http_status.HTTP_200_OK,
            json={"models": [{"name": "bge-m3:latest", "digest": DIGEST}]},
        )
    assert request.method == "POST"
    assert request.url.path == "/api/embed"
    payload = json.loads(request.content)
    assert payload.keys() == {"model", "input", "truncate"}
    assert payload["truncate"] is False
    assert payload["model"] == "bge-m3:latest"
    assert isinstance(payload["input"], list)
    return httpx.Response(
        http_status.HTTP_200_OK,
        json={
            "model": payload["model"],
            "embeddings": [[1.0] + [0.0] * (dimensions - 1)] * len(payload["input"]),
        },
    )


class TrackingTransport(httpx.MockTransport):
    closed: bool = False

    @override
    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


def test_batch_probe_reuse_and_close() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(request)

    transport = TrackingTransport(handler)

    async def scenario() -> None:
        adapter = OllamaEmbeddings(config(), transport=transport)
        with pytest.raises(EmbeddingError, match="not_initialized"):
            await adapter.embed(["before enter"])
        async with adapter:
            for _ in range(2):
                batch = await adapter.embed(["Alice", "Боб"])
                assert batch.vectors == ((1.0, 0.0, 0.0),) * 2
                assert batch.metadata == adapter.metadata
                assert batch.metadata.input_version == "raw-text-v1"
            assert not transport.closed
        assert transport.closed
        with pytest.raises(EmbeddingError, match="not_initialized"):
            await adapter.embed(["after exit"])

    asyncio.run(scenario())
    assert len(calls) == 9
    assert json.loads(calls[-2].content)["input"] == ["Alice", "Боб"]


@pytest.mark.parametrize("texts", [[], [""], ["   "], "single string", ["x"] * 101])
def test_invalid_input_has_no_http(texts: list[str] | str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(request)

    async def scenario() -> None:
        async with OllamaEmbeddings(
            config(), transport=httpx.MockTransport(handler)
        ) as a:
            with pytest.raises(EmbeddingError, match="invalid_input"):
                await a.embed(texts)

    asyncio.run(scenario())
    assert calls == 3


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"model": "other", "embeddings": [[1, 0, 0]]}, "model_mismatch"),
        ({"model": "bge-m3:latest", "embeddings": []}, "count_mismatch"),
        ({"model": "bge-m3:latest", "embeddings": [[1, 0], [1, 0]]}, "count_mismatch"),
        *[
            ({"model": "bge-m3:latest", "embeddings": [v]}, "invalid_vector")
            for v in [list[float](), [0, 0, 0], [1, 0], [1, 0, 0, 0]]
        ],
        *[
            ({"model": "bge-m3:latest", "embeddings": [v]}, "invalid_response")
            for v in [[True, 0, 0], ["1", 0, 0], [None, 0, 0]]
        ],
        ({"embeddings": [[1, 0, 0]]}, "invalid_response"),
        ([], "invalid_response"),
    ],
)
def test_bad_probe_closes_client(body: object, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return response(request)
        return httpx.Response(http_status.HTTP_200_OK, json=body)

    transport = TrackingTransport(handler)

    async def scenario() -> None:
        with pytest.raises(EmbeddingError, match=code) as error:
            async with OllamaEmbeddings(config(), transport=transport):
                pytest.fail("Invalid probe must fail")
        assert not error.value.retryable
        assert error.value.__context__ is None
        assert transport.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite(value: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return response(request)
        return httpx.Response(
            http_status.HTTP_200_OK,
            content='{"model":"bge-m3:latest","embeddings":[[' + value + ",0,0]]}",
        )

    async def scenario() -> None:
        with pytest.raises(EmbeddingError, match="invalid_vector"):
            async with OllamaEmbeddings(
                config(), transport=httpx.MockTransport(handler)
            ):
                pytest.fail("Nonfinite probe must fail")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (http_status.HTTP_400_BAD_REQUEST, False),
        (http_status.HTTP_404_NOT_FOUND, False),
        (http_status.HTTP_408_REQUEST_TIMEOUT, True),
        (http_status.HTTP_429_TOO_MANY_REQUESTS, True),
        (http_status.HTTP_500_INTERNAL_SERVER_ERROR, True),
        (http_status.HTTP_307_TEMPORARY_REDIRECT, False),
    ],
)
def test_http_failure_no_retry_or_leak(status: int, retryable: bool) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="private document/token", headers={"Location": "/"}
        )

    async def scenario() -> None:
        with pytest.raises(EmbeddingError) as error:
            async with OllamaEmbeddings(
                config(), transport=httpx.MockTransport(handler)
            ):
                pytest.fail("Must fail")
        assert error.value.retryable == retryable
        assert "private" not in str(error.value)
        assert error.value.__context__ is None

    asyncio.run(scenario())
    assert calls == 1


@pytest.mark.parametrize("transport_failure", [False, True])
def test_invalid_json_or_transport_failure(transport_failure: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if transport_failure:
            raise httpx.ReadTimeout("private content", request=request)
        return httpx.Response(http_status.HTTP_200_OK, content=b"not json")

    async def scenario() -> None:
        with pytest.raises(EmbeddingError) as error:
            async with OllamaEmbeddings(
                config(), transport=httpx.MockTransport(handler)
            ):
                pytest.fail("Must fail")
        assert error.value.retryable == transport_failure
        assert error.value.__context__ is None

    asyncio.run(scenario())


def test_response_size_bound() -> None:
    async def scenario() -> None:
        with pytest.raises(EmbeddingError, match="response_too_large"):
            async with OllamaEmbeddings(
                config(max_response_bytes=10), transport=httpx.MockTransport(response)
            ):
                pytest.fail("Must fail")

    asyncio.run(scenario())


@pytest.mark.parametrize("change_at", [1, 3, 4, 6])
def test_tag_changed_before_or_after_probe_or_batch(change_at: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == change_at:
            return httpx.Response(
                http_status.HTTP_200_OK,
                json={"models": [{"name": "bge-m3:latest", "digest": "b" * 64}]},
            )
        return response(request)

    async def scenario() -> None:
        with pytest.raises(EmbeddingError, match="model_mismatch"):
            async with OllamaEmbeddings(
                config(), transport=httpx.MockTransport(handler)
            ) as a:
                await a.embed(["document"])

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["base_url", "model", "digest", "dimensions"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_configuration(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str | None
) -> None:
    for name in os.environ:
        if name.startswith("ARTICLE_GRAPH_OLLAMA__"):
            monkeypatch.delenv(name)
    values = config().model_dump()
    values.pop(field)
    if value is not None:
        values[field] = value
    with pytest.raises(ValidationError):
        OllamaSettings(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", "file:///tmp/model"),
        ("base_url", "http://user:password@ollama.test"),
        ("base_url", "http://ollama.test/?token=private"),
        ("model", "bge-m3"),
        ("dimensions", 0),
        ("dimensions", 4097),
        ("timeout_seconds", float("nan")),
    ],
)
def test_invalid_configuration(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        config(**{field: value})


@pytest.mark.parametrize("dimensions", [3, 1024])
def test_application_configuration(dimensions: int, llm_settings: LLMSettings) -> None:
    def build() -> Settings:
        return Settings(
            _env_file=None,
            app_token="app-" * 10,
            jwt_access_key="access-" * 10,
            jwt_refresh_key="refresh-" * 10,
            llm=llm_settings,
            neo4j=Neo4jSettings(embedding_dimensions=1024),
            ollama=config(dimensions=dimensions),
        )

    if dimensions != 1024:
        with pytest.raises(ValidationError, match="dimensions must match"):
            build()
    else:
        assert build().ollama == config(dimensions=dimensions)


def test_lazy_http_runtime() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "import sys\nimport article_graph.embeddings as e\n"
            "assert 'httpx' not in sys.modules\n"
            "assert 'neo4j' not in sys.modules\n"
            "assert e.httpx.__name__ == 'httpx'\n"
            "assert 'httpx' in sys.modules\n",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def space_session(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings
) -> Iterator[Session]:
    with neo4j_driver.session(database=neo4j_settings.database) as session:
        assert (
            session.run(
                "MATCH (s:EmbeddingSpace {index_name: $index}) RETURN s",
                index=VECTOR_INDEX,
            ).single()
            is None
        ), "P06 requires an unbound isolated test index"
        try:
            yield session
        finally:
            session.run("MATCH (n:_GeomotivP06Test) DETACH DELETE n").consume()
            session.run(
                "MATCH (s:EmbeddingSpace {index_name: $index}) "
                "WHERE s.digest IN $digests DETACH DELETE s",
                index=VECTOR_INDEX,
                digests=[DIGEST, "b" * 64],
            ).consume()


def metadata(dimensions: int = 1024, digest: str = DIGEST) -> EmbeddingMetadata:
    return EmbeddingMetadata(
        model="bge-m3:latest", digest=digest, dimensions=dimensions
    )


@pytest.mark.integration
def test_space_binding_and_rejection(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, space_session: Session
) -> None:
    expected = metadata()
    bind_embedding_space(neo4j_driver, neo4j_settings, expected)
    bind_embedding_space(neo4j_driver, neo4j_settings, expected)
    for wrong in [
        metadata(digest="b" * 64),
        metadata(dimensions=3),
        expected.model_copy(update={"model": "other:latest"}),
    ]:
        with pytest.raises(EmbeddingError, match="mismatch"):
            bind_embedding_space(neo4j_driver, neo4j_settings, wrong)
    row = space_session.run(
        "MATCH (s:EmbeddingSpace {index_name: $index}) RETURN s.digest AS digest",
        index=VECTOR_INDEX,
    ).single(strict=True)
    assert row["digest"] == DIGEST
    require_ready(neo4j_driver, neo4j_settings)


@pytest.mark.integration
def test_partial_metadata_rejected(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, space_session: Session
) -> None:
    space_session.run(
        "CREATE (:EmbeddingSpace {index_name: $index, digest: $digest})",
        index=VECTOR_INDEX,
        digest=DIGEST,
    ).consume()
    with pytest.raises(EmbeddingError, match="space_mismatch"):
        bind_embedding_space(neo4j_driver, neo4j_settings, metadata())


@pytest.mark.integration
def test_unregistered_existing_vectors_rejected(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, space_session: Session
) -> None:
    space_session.run(
        "CREATE (:Character:_GeomotivP06Test {character_id: $id, embedding: $vector})",
        id=str(uuid4()),
        vector=[1.0] + [0.0] * 1023,
    ).consume()
    with pytest.raises(EmbeddingError, match="unregistered"):
        bind_embedding_space(neo4j_driver, neo4j_settings, metadata())
    assert (
        space_session.run(
            "MATCH (s:EmbeddingSpace {index_name: $index}) RETURN s", index=VECTOR_INDEX
        ).single()
        is None
    )


@pytest.mark.integration
def test_concurrent_different_spaces(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, space_session: Session
) -> None:
    barrier = Barrier(2)

    def bind(digest: str) -> str:
        barrier.wait(timeout=10)
        try:
            bind_embedding_space(neo4j_driver, neo4j_settings, metadata(digest=digest))
        except EmbeddingError as error:
            return error.code
        return "bound"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bind, [DIGEST, "b" * 64]))
    assert sorted(results) == ["bound", "embedding_space_mismatch"]


@pytest.mark.integration
def test_bound_lifecycle_and_real_vectors(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, space_session: Session
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(request, dimensions=1024)

    transport = TrackingTransport(handler)

    async def scenario() -> None:
        async with open_embeddings(
            config(dimensions=1024), neo4j_driver, neo4j_settings, transport=transport
        ) as adapter:
            batch = await adapter.embed(["Alice", "Bob"])
            for vector in batch.vectors:
                space_session.run(
                    "CREATE (:Character:_GeomotivP06Test "
                    "{character_id: $id, embedding: $vector, embedding_digest: $digest})",
                    id=str(uuid4()),
                    vector=list(vector),
                    digest=batch.metadata.digest,
                ).consume()
            rows = list(
                space_session.run(
                    "MATCH (c:_GeomotivP06Test) RETURN size(c.embedding) AS size, "
                    "c.embedding_digest AS digest"
                )
            )
            assert len(rows) == 2
            assert all(row["size"] == 1024 and row["digest"] == DIGEST for row in rows)
        assert transport.closed

    asyncio.run(scenario())


@pytest.mark.integration
def test_database_failure_closes_http(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, space_session: Session
) -> None:
    transport = TrackingTransport(response)

    async def scenario() -> None:
        with pytest.raises(EmbeddingError, match="dimension_mismatch"):
            async with open_embeddings(
                config(), neo4j_driver, neo4j_settings, transport=transport
            ):
                pytest.fail("Must fail before yielding")
        assert transport.closed

    asyncio.run(scenario())
