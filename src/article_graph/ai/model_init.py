"""Download the explicitly pinned model, then probe and bind its embedding space."""

import asyncio

lazy import httpx

from article_graph.ai.embeddings import open_embeddings
from article_graph.db.database import open_database
from article_graph.settings import Neo4jSettings, OllamaSettings


async def initialize(model: OllamaSettings, database: Neo4jSettings) -> None:
    # Pull may download gigabytes. Never log the response or provider credentials.
    async with httpx.AsyncClient(timeout=1800, trust_env=False) as client:
        response = await client.post(
            model.base_url.rstrip("/") + "/api/pull",
            json={"model": model.model, "stream": False},
        )
        if not response.is_success or response.json().get("status") != "success":
            raise RuntimeError("model_download_failed")
    with open_database(database) as driver:
        # Checks installed digest, real vector dimension and ONLINE index compatibility.
        async with open_embeddings(model, driver, database):
            pass


def main() -> None:
    try:
        asyncio.run(initialize(OllamaSettings(), Neo4jSettings()))
    except Exception:
        raise SystemExit(
            "Model initialization failed; verify explicit configuration"
        ) from None
    print("Model downloaded and embedding space ready")


if __name__ == "__main__":
    main()
