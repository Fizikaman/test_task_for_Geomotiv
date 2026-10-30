"""Publish a prepared artifact atomically; no providers or file I/O in the transaction."""

import hashlib
import json
from enum import StrEnum
from uuid import UUID, uuid5

from pydantic import Field
lazy from neo4j import Driver, ManagedTransaction

from article_graph.domain import DTO, Character
from article_graph.resolution import RESOLUTION_NAMESPACE, ResolutionArtifact
from article_graph.schema import VECTOR_INDEX, validate_embedding
from article_graph.settings import Limits, Neo4jSettings


class PublicationResult(StrEnum):
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class PublicationError(Exception):
    """Safe internal code, never includes document contents or attempt tokens."""


class Publication(DTO):
    article_id: UUID
    job_id: UUID
    attempt: int = Field(gt=0)
    attempt_token: UUID = Field(repr=False)
    artifact: ResolutionArtifact = Field(repr=False)


def _validate(request: Publication, dimensions: int, limits: Limits) -> None:
    artifact = request.artifact
    characters = {c.source.local_id: c for c in artifact.characters}
    mappings = {m.local_id: m for m in artifact.mappings}
    ids = {c.character_id for c in artifact.characters}
    if (
        len(characters) != len(artifact.characters)
        or len(mappings) != len(artifact.mappings)
        or characters.keys() != mappings.keys()
        or len(characters) > limits.characters
        or len(artifact.relations) > limits.relations
        or len({r.relation_id for r in artifact.relations}) != len(artifact.relations)
    ):
        raise PublicationError("invalid_artifact")
    if characters and (
        artifact.embedding_metadata is None
        or artifact.embedding_metadata.dimensions != dimensions
    ):
        raise PublicationError("embedding_space_changed")
    for local_id, character in characters.items():
        mapping = mappings[local_id]
        if character.character_id != mapping.character_id:
            raise PublicationError("invalid_mapping")
        validate_embedding(list(character.embedding), dimensions)
        if mapping.decision == "match":
            if (
                sum(
                    c.character.character_id == mapping.character_id
                    for c in mapping.candidates
                )
                != 1
            ):
                raise PublicationError("invalid_mapping")
        elif character.character_id != uuid5(
            RESOLUTION_NAMESPACE, f"character:{artifact.extraction_hash}:{local_id}"
        ):
            raise PublicationError("invalid_mapping")
    directions: dict[str, bool] = {}
    for relation in artifact.relations:
        if (
            relation.article_id != request.article_id
            or relation.revision_id != artifact.revision_id
            or not {relation.subject_id, relation.object_id} <= ids
            or len(relation.evidence) > limits.evidence_per_relation
            or (
                not relation.directed
                and str(relation.subject_id) > str(relation.object_id)
            )
            or directions.setdefault(relation.kind, relation.directed)
            != relation.directed
        ):
            raise PublicationError("invalid_relation")


class GraphPublisher:
    def __init__(self, driver: Driver, settings: Neo4jSettings, limits: Limits) -> None:
        self.driver = driver
        self.settings = settings
        self.limits = limits

    def publish(self, request: Publication) -> PublicationResult:
        _validate(request, self.settings.embedding_dimensions, self.limits)
        # Serialize the prepared graph before opening the transaction.
        digest = hashlib.sha256(request.artifact.model_dump_json().encode()).hexdigest()
        characters = [
            {
                "character_id": str(c.character_id),
                "local_id": c.source.local_id,
                "name": c.source.name,
                "description": c.source.description,
                "aliases": list(c.source.aliases),
                "embedding": list(c.embedding),
            }
            for c in request.artifact.characters
        ]
        relations = [
            {
                **r.model_dump(mode="json", exclude={"evidence"}),
                "evidence": json.dumps([e.model_dump(mode="json") for e in r.evidence]),
            }
            for r in request.artifact.relations
        ]
        kinds = [
            {"kind": kind, "directed": directed}
            for kind, directed in sorted(
                {r.kind: r.directed for r in request.artifact.relations}.items()
            )
        ]
        with self.driver.session(database=self.settings.database) as session:
            return session.execute_write(
                self._publish, request, digest, characters, relations, kinds
            )

    def _publish(
        self,
        tx: ManagedTransaction,
        request: Publication,
        digest: str,
        characters: list[dict[str, str | list[str] | list[float]]],
        relations: list[dict[str, object]],
        kinds: list[dict[str, str | bool]],
    ) -> PublicationResult:
        article, revision, job = (
            str(request.article_id),
            str(request.artifact.revision_id),
            str(request.job_id),
        )
        # Same Article lock as registration/retry, then Job. P15 must follow this
        # order for claim/heartbeat too; read lease and pointers AFTER acquiring it.
        tx.run(
            "MATCH (a:Article {article_id: $article}) "
            "SET a._publication_lock = true REMOVE a._publication_lock",
            article=article,
        ).consume()
        tx.run(
            "MATCH (j:Job {job_id: $job}) "
            "SET j._publication_lock = true REMOVE j._publication_lock",
            job=job,
        ).consume()
        row = tx.run(
            "MATCH (a:Article {article_id: $article})-[:HAS_REVISION]->"
            "(r:ArticleRevision {revision_id: $revision})-[:HAS_JOB]->"
            "(j:Job {job_id: $job}) "
            "WHERE r.article_id = $article AND j.article_id = $article "
            "AND j.revision_id = $revision AND r.job_id = $job "
            "RETURN a.latest_revision_id AS latest, a.job_id AS latest_job, "
            "j.status AS status, j.attempt AS attempt, j.attempt_token AS token, "
            "j.publication_hash AS hash, "
            "j.lease_expires_at > datetime.realtime().epochMillis / 1000.0 AS live",
            article=article,
            revision=revision,
            job=job,
        ).single()
        if row is None:
            raise PublicationError("job_changed")
        if row["attempt"] != request.attempt or row["token"] != str(
            request.attempt_token
        ):
            raise PublicationError("stale_attempt")
        if row["status"] == "completed":
            if row["hash"] != digest:
                raise PublicationError("publication_conflict")
            return PublicationResult.COMPLETED
        if row["status"] == "superseded":
            return PublicationResult.SUPERSEDED
        if row["status"] != "processing" or row["live"] is not True:
            raise PublicationError("lease_lost")
        if row["latest"] != revision:
            tx.run(
                "MATCH (j:Job {job_id: $job}), (r:ArticleRevision {revision_id: $revision}) "
                "SET j.status = 'superseded', r.status = 'superseded', "
                "j.completed_at = datetime.realtime().epochMillis / 1000.0 "
                "REMOVE j.lease_expires_at",
                job=job,
                revision=revision,
            ).consume()
            return PublicationResult.SUPERSEDED
        if row["latest_job"] != job:
            raise PublicationError("job_changed")
        # Serializes absent Character/type checks across articles. No registration
        # mutex is acquired here, so registration -> Article cannot deadlock.
        tx.run(
            "MERGE (l:RegistrationLock {name: 'graph-publication'}) "
            "SET l._lock = true REMOVE l._lock"
        ).consume()
        self._check_profiles(tx, request)
        live = tx.run(
            "MATCH (j:Job {job_id: $job}) "
            "RETURN j.lease_expires_at > datetime.realtime().epochMillis / 1000.0 AS live",
            job=job,
        ).single(strict=True)
        if live is None or live["live"] is not True:
            raise PublicationError("lease_lost")
        self._write_graph(tx, request, characters, relations, kinds)
        # Recheck real time after waiting for graph/profile locks. Expiry rolls
        # back ALL preceding graph writes, including the first publication.
        row = tx.run(
            "MATCH (a:Article {article_id: $article}), "
            "(r:ArticleRevision {revision_id: $revision}), (j:Job {job_id: $job}) "
            "WHERE j.lease_expires_at > datetime.realtime().epochMillis / 1000.0 "
            "SET a.active_revision_id = $revision, r.status = 'completed', "
            "r.publication_hash = $hash, j.status = 'completed', j.publication_hash = $hash, "
            "j.completed_at = datetime.realtime().epochMillis / 1000.0 "
            "REMOVE j.lease_expires_at, j.error_code, j.error_message "
            "RETURN j.job_id AS id",
            article=article,
            revision=revision,
            job=job,
            hash=digest,
        ).single()
        if row is None:
            raise PublicationError("lease_lost")
        return PublicationResult.COMPLETED

    def _check_profiles(self, tx: ManagedTransaction, request: Publication) -> None:
        artifact = request.artifact
        if artifact.characters:
            row = tx.run(
                "MATCH (s:EmbeddingSpace {index_name: $index}) "
                "SET s._publication_lock = true REMOVE s._publication_lock "
                "RETURN s.model AS model, s.digest AS digest, "
                "s.dimensions AS dimensions, s.input_version AS input_version",
                index=VECTOR_INDEX,
            ).single()
            if (
                artifact.embedding_metadata is None
                or row is None
                or (row.data() != artifact.embedding_metadata.model_dump())
            ):
                raise PublicationError("embedding_space_changed")
        ids = sorted({str(m.character_id) for m in artifact.mappings})
        tx.run(
            "UNWIND $ids AS id WITH id ORDER BY id "
            "MATCH (c:Character {character_id: id}) "
            "SET c._publication_lock = true REMOVE c._publication_lock",
            ids=ids,
        ).consume()
        profiles = {
            row["character_id"]: Character.model_validate(row.data())
            for row in tx.run(
                "UNWIND $ids AS id MATCH (c:Character {character_id: id}) "
                "RETURN c.character_id AS character_id, c.name AS name, "
                "c.description AS description, coalesce(c.aliases, []) AS aliases",
                ids=ids,
            )
        }
        for mapping in artifact.mappings:
            current = profiles.get(str(mapping.character_id))
            if mapping.decision == "match":
                snapshot = next(
                    c.character
                    for c in mapping.candidates
                    if c.character.character_id == mapping.character_id
                )
                if current != snapshot:
                    raise PublicationError("candidate_changed")
            elif current is not None:
                # A deterministic new UUID already owned by another publication
                # must never silently become a match without identity evidence.
                raise PublicationError("character_conflict")

    @staticmethod
    def _write_graph(
        tx: ManagedTransaction,
        request: Publication,
        characters: list[dict[str, str | list[str] | list[float]]],
        relations: list[dict[str, object]],
        kinds: list[dict[str, str | bool]],
    ) -> None:
        metadata = request.artifact.embedding_metadata
        tx.run(
            "UNWIND $characters AS item "
            "MATCH (r:ArticleRevision {revision_id: $revision}) "
            "MERGE (c:Character {character_id: item.character_id}) "
            "ON CREATE SET c.name = item.name, c.description = item.description, "
            "c.aliases = item.aliases, c.embedding = item.embedding, "
            "c.embedding_digest = $digest "
            "CREATE (r)-[:MENTIONS {local_id: item.local_id}]->(c)",
            characters=characters,
            revision=str(request.artifact.revision_id),
            digest=metadata.digest if metadata else None,
        ).consume()
        # The persistent registry keeps kind direction stable even when its last
        # active assertion disappears. A conflict aborts preceding Character writes.
        tx.run(
            "UNWIND $kinds AS item MERGE (t:RelationType {kind: item.kind}) "
            "ON CREATE SET t.directed = item.directed",
            kinds=kinds,
        ).consume()
        if (
            tx.run(
                "UNWIND $kinds AS item MATCH (t:RelationType {kind: item.kind}) "
                "WHERE t.directed <> item.directed OR t.directed IS NULL "
                "RETURN t.kind LIMIT 1",
                kinds=kinds,
            ).single()
            is not None
        ):
            raise PublicationError("relation_direction_conflict")
        tx.run(
            "UNWIND $relations AS item "
            "MATCH (r:ArticleRevision {revision_id: item.revision_id}), "
            "(s:Character {character_id: item.subject_id}), "
            "(o:Character {character_id: item.object_id}) "
            "CREATE (v:RelationAssertion) SET v = item "
            "CREATE (r)-[:ASSERTS]->(v) CREATE (v)-[:SUBJECT]->(s) "
            "CREATE (v)-[:OBJECT]->(o)",
            relations=relations,
        ).consume()
