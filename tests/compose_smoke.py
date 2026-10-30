"""Run via stdin inside the isolated acceptance API; never calls an LLM."""

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import httpx
from fastapi import status as http_status
from pydantic import BaseModel
lazy from redis import Redis

from article_graph.ai.embeddings import EmbeddingBatch, OllamaEmbeddings
from article_graph.ai.resolution import (
    RESOLUTION_NAMESPACE,
    IdentityMapping,
    ResolutionArtifact,
    ResolvedCharacter,
)
from article_graph.db.database import open_database
from article_graph.db.processing_lease import LeaseRepository
from article_graph.db.publication import GraphPublisher, Publication, PublicationResult
from article_graph.db.schema import require_ready
from article_graph.domain import ExtractedCharacter, ProcessingResponse
from article_graph.security.auth import issue_pair
from article_graph.settings import Settings


class State(BaseModel):
    active: ProcessingResponse
    pending: ProcessingResponse
    character_id: UUID
    content_hash: str


def upload(
    client: httpx.Client, text: str, article_id: UUID | None = None
) -> ProcessingResponse:
    fields = {"text": text}
    if article_id is not None:
        fields["article_id"] = str(article_id)
    response = client.post(
        "/articles/upload", files={key: (None, value) for key, value in fields.items()}
    )
    assert response.status_code == http_status.HTTP_202_ACCEPTED
    return ProcessingResponse.model_validate(response.json())


def main() -> None:
    settings = Settings()
    # Refuse all production/P03/BroLango configurations before mutating anything.
    assert settings.neo4j.uri == "bolt://neo4j:7687"
    assert settings.neo4j.password.get_secret_value() == "geomotiv-p19-test-only"
    assert settings.llm.base_url == "http://127.0.0.1:9/v1"
    assert os.getuid() == 10001
    assert sys.version_info[:3] == (3, 15, 2)
    assert sys._is_gil_enabled()
    state_path = Path(settings.blob_root) / "p19-acceptance-state.json"
    headers = {"Authorization": f"Bearer {issue_pair(settings).tokens.access_token}"}
    with (
        httpx.Client(
            base_url="http://127.0.0.1:8000", headers=headers, timeout=90
        ) as client,
        open_database(settings.neo4j) as driver,
        Redis.from_url("redis://redis:6379/0") as redis,
    ):
        require_ready(driver, settings.neo4j)
        assert client.get("/health/ready").status_code == http_status.HTTP_200_OK
        if sys.argv[1] == "seed":
            text = f"P19 isolated published graph {uuid4()}"
            active = upload(client, text)
            claim = LeaseRepository(driver, settings.neo4j, settings.limits, 120).claim(
                active.job_id
            )
            assert claim is not None

            async def embed() -> EmbeddingBatch:
                assert settings.ollama is not None
                async with OllamaEmbeddings(settings.ollama) as adapter:
                    return await adapter.embed(["P19 test character"])

            batch = asyncio.run(embed())
            extraction_hash = hashlib.sha256(text.encode()).hexdigest()
            character_id = uuid5(RESOLUTION_NAMESPACE, f"character:{extraction_hash}:a")
            artifact = ResolutionArtifact(
                extraction_hash=extraction_hash,
                revision_id=active.revision_id,
                embedding_metadata=batch.metadata,
                characters=(
                    ResolvedCharacter(
                        source=ExtractedCharacter(
                            local_id="a",
                            name="P19",
                            description="Isolated durability fixture",
                        ),
                        character_id=character_id,
                        embedding=batch.vectors[0],
                    ),
                ),
                mappings=(
                    IdentityMapping(
                        local_id="a",
                        character_id=character_id,
                        decision="no_match",
                        reason="no_candidates",
                        candidates=(),
                    ),
                ),
                relations=(),
            )
            result = GraphPublisher(driver, settings.neo4j, settings.limits).publish(
                Publication(
                    article_id=active.article_id,
                    job_id=active.job_id,
                    attempt=claim.attempt,
                    attempt_token=claim.attempt_token,
                    artifact=artifact,
                )
            )
            assert result == PublicationResult.COMPLETED
            pending = upload(
                client, f"P19 pending update {active.article_id}", active.article_id
            )
            # Real Celery/Kombu delivery to Redis, with consumers stopped by the harness.
            from article_graph.processing.dispatcher import (
                CeleryBroker,
                DispatcherConfig,
            )

            broker = CeleryBroker(DispatcherConfig(broker_url="redis://redis:6379/0"))
            try:
                broker.publish(pending.job_id)
            finally:
                broker.close()
            redis.set("p19-durability", str(pending.job_id))
            state = State(
                active=active,
                pending=pending,
                character_id=character_id,
                content_hash=extraction_hash,
            )
            state_path.write_text(state.model_dump_json(), encoding="utf-8")
        else:
            state = State.model_validate_json(state_path.read_text(encoding="utf-8"))
            assert redis.get("p19-durability") == str(state.pending.job_id).encode()
            queued = redis.lrange("article_graph", 0, -1)
            assert isinstance(queued, list)
            assert any(
                json.loads(base64.b64decode(json.loads(item)["body"]))[0]
                == [str(state.pending.job_id)]
                for item in queued
            )
            source = Path(settings.blob_root) / "sources" / state.content_hash
            assert hashlib.sha256(source.read_bytes()).hexdigest() == state.content_hash
        response = client.get(f"/articles/{state.active.article_id}")
        assert response.status_code == http_status.HTTP_200_OK
        current = response.json()
        assert current["active_revision_id"] == str(state.active.revision_id)
        assert current["latest_revision_id"] == str(state.pending.revision_id)
        assert current["job_id"] == str(state.pending.job_id)
        assert current["status"] == "queued"
        page = client.get(f"/characters/by_article/{state.active.article_id}")
        assert page.status_code == http_status.HTTP_200_OK
        assert [item["character_id"] for item in page.json()["items"]] == [
            str(state.character_id)
        ]
    print("P19 durability and real embedding/index checks passed")


if __name__ == "__main__":
    main()
