"""Ollama batch embeddings and an explicit, process-local resource lifecycle."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Literal, Protocol
lazy from asyncio import run, to_thread

from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
lazy import httpx
lazy from neo4j import Driver, ManagedTransaction

from article_graph.db.database import open_database
from article_graph.db.schema import VECTOR_INDEX, require_ready, validate_embedding
from article_graph.domain import DTO
from article_graph.settings import Neo4jSettings, OllamaSettings


class EmbeddingMetadata(DTO):
    model: str = Field(min_length=1, pattern=r"^\S+:\S+$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimensions: int = Field(ge=1, le=4096)
    input_version: Literal["raw-text-v1"] = "raw-text-v1"


class EmbeddingBatch(DTO):
    metadata: EmbeddingMetadata
    vectors: tuple[tuple[float, ...], ...] = Field(repr=False)


class Embeddings(Protocol):
    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch: ...


class EmbeddingError(Exception):
    """Safe codes only; HTTP errors and document contents are never retained."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class _Model(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str
    digest: str


class _Tags(BaseModel):
    models: list[_Model]


class _Response(BaseModel):
    model_config = ConfigDict(strict=True)
    model: str
    embeddings: list[list[float]]


class OllamaEmbeddings:
    """One client, no retries. Entering probes; exiting always closes the client.

    Use open_embeddings for database consumers, which also binds the index space.
    A running Ollama deployment must not mutate model tags (see docs/embeddings.md).
    """

    def __init__(
        self,
        settings: OllamaSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._metadata = EmbeddingMetadata(
            model=settings.model, digest=settings.digest, dimensions=settings.dimensions
        )
        self._ready = False
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    async def __aenter__(self) -> OllamaEmbeddings:
        try:
            # The configured size is confirmed by real output, never requested via
            # Ollama's dimensions option (which could silently reduce the space).
            await self._embed(["embedding dimension probe"])
            self._ready = True
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._ready = False
        await self._client.aclose()

    async def _request[T: BaseModel](
        self, method: str, path: str, payload: dict[str, object] | None, schema: type[T]
    ) -> T:
        failure: EmbeddingError | None = None
        raw = bytearray()
        try:
            async with self._client.stream(
                method,
                self._settings.base_url.rstrip("/") + path,
                json=payload,
            ) as response:
                if response.status_code != http_status.HTTP_200_OK:
                    failure = EmbeddingError(
                        "embedding_http_error",
                        retryable=response.status_code
                        in {
                            http_status.HTTP_408_REQUEST_TIMEOUT,
                            http_status.HTTP_429_TOO_MANY_REQUESTS,
                        }
                        or httpx.codes.is_server_error(response.status_code),
                    )
                else:
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self._settings.max_response_bytes:
                            failure = EmbeddingError("embedding_response_too_large")
                            break
        except httpx.TransportError:
            failure = EmbeddingError("embedding_unavailable", retryable=True)
        if failure is not None:
            raise failure
        result: T | None = None
        try:
            result = schema.model_validate_json(raw)
        except ValidationError:
            pass
        if result is None:
            raise EmbeddingError("embedding_invalid_response")
        return result

    async def _check_model(self) -> None:
        tags = await self._request("GET", "/api/tags", None, _Tags)
        matches = [item for item in tags.models if item.name == self.metadata.model]
        if len(matches) != 1 or matches[0].digest != self.metadata.digest:
            raise EmbeddingError("embedding_model_mismatch")

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if not self._ready:
            raise EmbeddingError("embedding_not_initialized")
        return await self._embed(texts)

    async def _embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if (
            isinstance(texts, str)
            or not texts
            or len(texts) > self._settings.max_batch_size
            or any(not isinstance(text, str) or not text.strip() for text in texts)
        ):
            raise EmbeddingError("embedding_invalid_input")
        await self._check_model()
        response = await self._request(
            "POST",
            "/api/embed",
            {"model": self.metadata.model, "input": list(texts), "truncate": False},
            _Response,
        )
        if response.model != self.metadata.model:
            raise EmbeddingError("embedding_model_mismatch")
        if len(response.embeddings) != len(texts):
            raise EmbeddingError("embedding_count_mismatch")
        vectors: tuple[tuple[float, ...], ...] | None = None
        try:
            vectors = tuple(
                tuple(validate_embedding(vector, self.metadata.dimensions))
                for vector in response.embeddings
            )
        except ValueError:
            pass
        if vectors is None:
            raise EmbeddingError("embedding_invalid_vector")
        await self._check_model()
        return EmbeddingBatch(metadata=self.metadata, vectors=vectors)


def bind_embedding_space(
    driver: Driver, settings: Neo4jSettings, metadata: EmbeddingMetadata
) -> None:
    """Bind once under a unique, transaction-locked index key; never migrate DDL."""
    if metadata.dimensions != settings.embedding_dimensions:
        raise EmbeddingError("embedding_index_dimension_mismatch")
    require_ready(driver, settings)

    def bind(tx: ManagedTransaction) -> None:
        expected = metadata.model_dump()
        row = tx.run(
            "MERGE (s:EmbeddingSpace {index_name: $index}) "
            "ON CREATE SET s += $metadata, s.binding_pending = true "
            "SET s.lock_version = coalesce(s.lock_version, 0) + 1 "
            "RETURN s.model AS model, s.digest AS digest, "
            "s.dimensions AS dimensions, s.input_version AS input_version, "
            "s.binding_pending AS binding_pending",
            index=VECTOR_INDEX,
            metadata=expected,
        ).single(strict=True)
        stored = row.data()
        pending = stored.pop("binding_pending")
        if stored != expected:
            raise EmbeddingError("embedding_space_mismatch")
        if pending:
            legacy = tx.run(
                "MATCH (c:Character) WHERE c.embedding IS NOT NULL "
                "RETURN c.character_id LIMIT 1"
            ).single()
            if legacy is not None:
                raise EmbeddingError("embedding_unregistered_vectors")
            tx.run(
                "MATCH (s:EmbeddingSpace {index_name: $index}) "
                "REMOVE s.binding_pending",
                index=VECTOR_INDEX,
            ).consume()

    with driver.session(database=settings.database) as session:
        session.execute_write(bind)


@asynccontextmanager
async def open_embeddings(
    settings: OllamaSettings,
    driver: Driver,
    database: Neo4jSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncGenerator[OllamaEmbeddings]:
    """Probe and bind before accepting work; failure also closes HTTP resources."""
    async with OllamaEmbeddings(settings, transport=transport) as adapter:
        await to_thread(bind_embedding_space, driver, database, adapter.metadata)
        yield adapter


def main() -> None:
    # This init command reads explicit environment only, never credentials in .env.
    settings = OllamaSettings()
    database = Neo4jSettings()
    with open_database(database) as driver:

        async def initialize() -> None:
            async with open_embeddings(settings, driver, database):
                pass

        run(initialize())
    print("Ollama embedding space ready")


if __name__ == "__main__":
    main()
