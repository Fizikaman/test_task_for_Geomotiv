"""Read-only Neo4j top-k search. ANN scores nominate, never prove identity."""

from uuid import UUID

lazy from neo4j import Driver

from article_graph.domain import Character
from article_graph.embeddings import EmbeddingMetadata
from article_graph.resolution import Candidate
from article_graph.schema import VECTOR_INDEX, validate_embedding
from article_graph.settings import Neo4jSettings


class Neo4jCandidates:
    def __init__(self, driver: Driver, settings: Neo4jSettings) -> None:
        self._driver = driver
        self._settings = settings

    def search(
        self, vector: tuple[float, ...], metadata: EmbeddingMetadata, top_k: int
    ) -> tuple[Candidate, ...]:
        if not 1 <= top_k <= 100:
            raise ValueError("invalid top_k")
        if metadata.dimensions != self._settings.embedding_dimensions:
            raise ValueError("embedding dimension mismatch")
        values = validate_embedding(list(vector), metadata.dimensions)
        with self._driver.session(database=self._settings.database) as session:
            row = session.run(
                "MATCH (s:EmbeddingSpace {index_name: $index}) "
                "RETURN s.model AS model, s.digest AS digest, "
                "s.dimensions AS dimensions, s.input_version AS input_version",
                index=VECTOR_INDEX,
            ).single()
            if row is None or row.data() != metadata.model_dump():
                raise ValueError("embedding space mismatch")
            rows = session.run(
                "CALL db.index.vector.queryNodes($index, $top_k, $vector) "
                "YIELD node, score "
                "WHERE node.embedding_digest = $digest "
                "RETURN node.character_id AS character_id, node.name AS name, "
                "node.description AS description, coalesce(node.aliases, []) AS aliases, "
                "score ORDER BY score DESC, character_id ASC",
                index=VECTOR_INDEX,
                top_k=top_k,
                vector=values,
                digest=metadata.digest,
            )
            return tuple(
                Candidate(
                    character=Character.model_validate(
                        {
                            key: value
                            for key, value in row.data().items()
                            if key != "score"
                        }
                    ),
                    score=row["score"],
                )
                for row in rows
            )

    def current(self, character_id: UUID) -> Character | None:
        with self._driver.session(database=self._settings.database) as session:
            row = session.run(
                "MATCH (c:Character {character_id: $id}) "
                "RETURN c.character_id AS character_id, c.name AS name, "
                "c.description AS description, coalesce(c.aliases, []) AS aliases",
                id=str(character_id),
            ).single()
            return None if row is None else Character.model_validate(row.data())
