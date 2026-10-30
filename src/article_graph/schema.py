"""Community-compatible schema DDL and explicit readiness gate (no APOC)."""

import math
from typing import LiteralString

lazy from neo4j import Driver

from article_graph.database import open_database
from article_graph.settings import Neo4jSettings

VECTOR_INDEX = "character_embedding"
UNIQUE_PROPERTIES: tuple[tuple[LiteralString, LiteralString, LiteralString], ...] = (
    ("article_id", "Article", "article_id"),
    ("revision_id", "ArticleRevision", "revision_id"),
    ("character_id", "Character", "character_id"),
    ("relation_id", "RelationAssertion", "relation_id"),
    ("job_id", "Job", "job_id"),
    ("outbox_id", "Outbox", "outbox_id"),
    ("session_id", "RefreshSession", "session_id"),
    ("refresh_hash", "RefreshSession", "token_hash"),
    ("idempotency_id", "IdempotencyRecord", "idempotency_id"),
    ("embedding_space_index", "EmbeddingSpace", "index_name"),
    ("registration_lock", "RegistrationLock", "name"),
    ("relation_kind", "RelationType", "kind"),
)
COMPOSITE_PROPERTIES: tuple[
    tuple[LiteralString, LiteralString, LiteralString, LiteralString], ...
] = (
    ("revision_number", "ArticleRevision", "article_id", "number"),
    ("idempotency_key", "IdempotencyRecord", "app_id", "key"),
)
RANGE_PROPERTIES: tuple[tuple[LiteralString, ...], ...] = (
    ("article_input_hash", "Article", "latest_source_hash"),
    ("job_recovery", "Job", "status", "lease_expires_at"),
    ("outbox_delivery", "Outbox", "status", "available_at"),
    ("refresh_family", "RefreshSession", "family_id"),
    ("revision_article", "ArticleRevision", "article_id"),
    ("assertion_revision", "RelationAssertion", "revision_id"),
)


class SchemaNotReady(RuntimeError):
    """Safe startup/readiness failure without database response contents."""


def init_schema(driver: Driver, settings: Neo4jSettings) -> None:
    with driver.session(database=settings.database) as session:
        for name, label, prop in UNIQUE_PROPERTIES:
            session.run(
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            ).consume()
        for name, label, first, second in COMPOSITE_PROPERTIES:
            session.run(
                f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) "
                f"REQUIRE (n.{first}, n.{second}) IS UNIQUE"
            ).consume()
        for name, label, *props in RANGE_PROPERTIES:
            properties = ", ".join(f"n.{prop}" for prop in props)
            session.run(
                f"CREATE RANGE INDEX {name} IF NOT EXISTS "
                f"FOR (n:{label}) ON ({properties})"
            ).consume()
        session.run(
            f"CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS "
            "FOR (n:Character) ON n.embedding OPTIONS {indexConfig: {"
            "`vector.dimensions`: $dimensions, "
            "`vector.similarity_function`: 'cosine'}}",
            dimensions=settings.embedding_dimensions,
        ).consume()
        session.run(
            "CALL db.awaitIndexes($timeout)", timeout=settings.schema_timeout_seconds
        ).consume()
    require_ready(driver, settings)


def require_ready(driver: Driver, settings: Neo4jSettings) -> None:
    """Call before accepting work; never silently recreate incompatible indexes."""
    with driver.session(database=settings.database) as session:
        rows = list(session.run("SHOW INDEXES YIELD *"))
        constraints = {row["name"]: row for row in session.run("SHOW CONSTRAINTS")}
        for name, label, prop in UNIQUE_PROPERTIES:
            constraint = constraints.get(name)
            if (
                constraint is None
                or constraint["type"] != "UNIQUENESS"
                or constraint["entityType"] != "NODE"
                or constraint["labelsOrTypes"] != [label]
                or constraint["properties"] != [prop]
            ):
                raise SchemaNotReady(
                    "Required uniqueness constraint is missing or incompatible"
                )
        for name, label, first, second in COMPOSITE_PROPERTIES:
            constraint = constraints.get(name)
            if (
                constraint is None
                or constraint["type"] != "UNIQUENESS"
                or constraint["entityType"] != "NODE"
                or constraint["labelsOrTypes"] != [label]
                or constraint["properties"] != [first, second]
            ):
                raise SchemaNotReady(
                    "Required composite constraint is missing or incompatible"
                )
        for name, label, *props in RANGE_PROPERTIES:
            matching = [row for row in rows if row["name"] == name]
            if (
                len(matching) != 1
                or matching[0]["type"] != "RANGE"
                or matching[0]["entityType"] != "NODE"
                or matching[0]["labelsOrTypes"] != [label]
                or matching[0]["properties"] != props
            ):
                raise SchemaNotReady("Required range index is missing or incompatible")
        expected = {
            item[0]
            for item in (*UNIQUE_PROPERTIES, *COMPOSITE_PROPERTIES, *RANGE_PROPERTIES)
        }
        online = {row["name"] for row in rows if row["state"] == "ONLINE"}
        if not expected.issubset(online):
            raise SchemaNotReady("Required schema indexes are missing or not ONLINE")
        vectors = [row for row in rows if row["name"] == VECTOR_INDEX]
        if len(vectors) != 1:
            raise SchemaNotReady("Character vector index is missing")
        index = vectors[0]
        config = index["options"]["indexConfig"]
        if (
            index["state"] != "ONLINE"
            or index["type"] != "VECTOR"
            or index["entityType"] != "NODE"
            or index["labelsOrTypes"] != ["Character"]
            or index["properties"] != ["embedding"]
            or config.get("vector.dimensions") != settings.embedding_dimensions
            or str(config.get("vector.similarity_function")).lower() != "cosine"
        ):
            raise SchemaNotReady("Character vector index is incompatible or not ONLINE")


def validate_embedding(values: list[float], dimensions: int) -> list[float]:
    """Community has no property-type constraint; validate before future writes."""
    if (
        len(values) != dimensions
        or any(type(value) not in (int, float) for value in values)
        or any(not math.isfinite(value) for value in values)
        or not any(value != 0 for value in values)
    ):
        raise ValueError(
            "Embedding must be a finite nonzero numeric list of configured size"
        )
    return [float(value) for value in values]


def main() -> None:
    # This CLI does not need API credentials and deliberately never loads .env.
    configuration = Neo4jSettings()
    with open_database(configuration) as driver:
        init_schema(driver, configuration)
    print("Neo4j schema ready")
