"""Read only the published graph, with deterministic distinct pagination."""

from typing import LiteralString
from uuid import UUID
lazy import json

lazy from neo4j import Driver, ManagedTransaction

from article_graph.domain import (
    DTO,
    Character,
    ErrorCode,
    Page,
    PageRequest,
    Relation,
    RelationType,
)
from article_graph.registration import RegistrationError
from article_graph.settings import Neo4jSettings

ACTIVE = (
    "MATCH (a:Article)-[:HAS_REVISION]->(r:ArticleRevision) "
    "WHERE r.revision_id = a.active_revision_id AND r.status = 'completed' "
)
CHARACTER = (
    "RETURN DISTINCT c.character_id AS character_id, c.name AS name, "
    "c.description AS description, c.aliases AS aliases "
    "ORDER BY character_id SKIP $offset LIMIT $fetch"
)


class GraphRepository:
    def __init__(self, driver: Driver, settings: Neo4jSettings) -> None:
        self.driver = driver
        self.settings = settings

    def _page[T: DTO](
        self,
        model: type[T],
        query: LiteralString,
        page: PageRequest,
        *,
        existence: LiteralString | None = None,
        **parameters: object,
    ) -> Page[T]:
        def read(tx: ManagedTransaction) -> Page[T]:
            if (
                existence is not None
                and tx.run(existence, parameters=parameters).single() is None
            ):
                raise RegistrationError(ErrorCode.NOT_FOUND)
            rows = tx.run(
                query,
                parameters={
                    **parameters,
                    "offset": page.offset,
                    "fetch": page.limit + 1,
                },
            )
            items: list[T] = []
            for row in rows:
                data = dict(row.data())
                if model is Relation:
                    data["evidence"] = json.loads(data["evidence"])
                items.append(model.model_validate(data))
            return Page[T](
                items=tuple(items[: page.limit]),
                limit=page.limit,
                offset=page.offset,
                next_offset=page.offset + page.limit
                if len(items) > page.limit
                else None,
            )

        with self.driver.session(database=self.settings.database) as session:
            return session.execute_read(read)

    def characters(
        self, page: PageRequest, article_id: UUID | None = None
    ) -> Page[Character]:
        return self._page(
            Character,
            ACTIVE
            + "AND ($id IS NULL OR a.article_id = $id) "
            + "MATCH (r)-[:MENTIONS]->(c:Character) "
            + CHARACTER,
            page,
            existence="MATCH (a:Article {article_id: $id}) RETURN a.article_id"
            if article_id is not None
            else None,
            id=str(article_id) if article_id is not None else None,
        )

    def types(self, page: PageRequest) -> Page[RelationType]:
        return self._page(
            RelationType,
            ACTIVE
            + "MATCH (r)-[:ASSERTS]->(v:RelationAssertion) "
            + "RETURN DISTINCT v.kind AS kind, v.directed AS directed "
            + "ORDER BY kind SKIP $offset LIMIT $fetch",
            page,
        )

    def connected(
        self, character_id: UUID, kind: str | None, page: PageRequest
    ) -> Page[Relation]:
        return self._page(
            Relation,
            ACTIVE
            + "MATCH (r)-[:ASSERTS]->(v:RelationAssertion) "
            + "WHERE (v.subject_id = $id OR "
            + "(v.directed = false AND v.object_id = $id)) "
            + "AND ($kind IS NULL OR v.kind = $kind) "
            + "RETURN DISTINCT v.relation_id AS relation_id, "
            + "v.article_id AS article_id, v.revision_id AS revision_id, "
            + "v.subject_id AS subject_id, v.object_id AS object_id, "
            + "v.kind AS kind, v.directed AS directed, v.evidence AS evidence, "
            + "v.confidence AS confidence "
            + "ORDER BY relation_id SKIP $offset LIMIT $fetch",
            page,
            existence="MATCH (c:Character {character_id: $id}) RETURN c.character_id",
            id=str(character_id),
            kind=kind,
        )
