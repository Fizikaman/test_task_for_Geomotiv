import json
import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from subprocess import run
from threading import Barrier
from time import sleep, time
from typing import LiteralString
from uuid import UUID, uuid4, uuid5

import pytest
lazy from neo4j import Driver, ManagedTransaction
lazy from neo4j.exceptions import ConstraintError

from article_graph.article_repository import ArticleRepository
from article_graph.document_format import DocumentFormat
from article_graph.domain import Character, ExtractedCharacter, Relation, TextEvidence
from article_graph.embeddings import EmbeddingMetadata, bind_embedding_space
from article_graph.publication import (
    GraphPublisher,
    Publication,
    PublicationError,
    PublicationResult,
)
from article_graph.registration import UploadRequest
from article_graph.resolution import (
    RESOLUTION_NAMESPACE,
    Candidate,
    IdentityMapping,
    ResolutionArtifact,
    ResolvedCharacter,
)
from article_graph.settings import Limits, Neo4jSettings

META = EmbeddingMetadata(model="publication-test:v1", digest="e" * 64, dimensions=1024)
VECTOR = (1.0,) + (0.0,) * 1023


class Database:
    def __init__(self, driver: Driver, settings: Neo4jSettings) -> None:
        self.driver = driver
        self.settings = settings
        self.publisher = GraphPublisher(driver, settings, Limits())
        self.articles: list[str] = []
        self.characters: list[str] = []
        self.relations: list[str] = []
        self.kinds: list[str] = []

    def query(self, query: LiteralString, **params: object) -> list[dict[str, object]]:
        with self.driver.session(database=self.settings.database) as session:
            return [dict(row.data()) for row in session.run(query, parameters=params)]

    def prepare(
        self,
        *,
        article_id: UUID | None = None,
        shared: Character | None = None,
        with_relation: bool = True,
        empty: bool = False,
        kind: str = "FRIEND_OF",
        directed: bool = False,
    ) -> Publication:
        registered = (
            ArticleRepository(self.driver, self.settings)
            .register(
                UploadRequest(
                    app_id="p13-test",
                    article_id=article_id,
                    method="text",
                    content_type="text/markdown",
                    format=DocumentFormat.MARKDOWN,
                    content_hash=sha256(uuid4().bytes).hexdigest(),
                    size=1,
                )
            )
            .body
        )
        self.articles.append(str(registered.article_id))
        extraction_hash = sha256(uuid4().bytes).hexdigest()
        characters: list[ResolvedCharacter] = []
        mappings: list[IdentityMapping] = []
        for local_id in () if empty else ("a", "b"):
            source = ExtractedCharacter(
                local_id=local_id,
                name=f"Name {local_id}",
                description=f"Distinct biographical context for {local_id}.",
                aliases=(f"Alias {local_id}",),
            )
            target = shared if local_id == "a" else None
            character_id = (
                target.character_id
                if target
                else uuid5(
                    RESOLUTION_NAMESPACE, f"character:{extraction_hash}:{local_id}"
                )
            )
            self.characters.append(str(character_id))
            characters.append(
                ResolvedCharacter(
                    source=source,
                    character_id=character_id,
                    embedding=VECTOR,
                )
            )
            mappings.append(
                IdentityMapping(
                    local_id=local_id,
                    character_id=character_id,
                    decision="match" if target else "no_match",
                    reason="alias_and_context" if target else "no_candidates",
                    candidates=(Candidate(character=target, score=1),)
                    if target
                    else (),
                )
            )
        relations: tuple[Relation, ...] = ()
        if not empty and with_relation:
            endpoints = [c.character_id for c in characters]
            if not directed:
                endpoints.sort(key=str)
            relations = (
                Relation(
                    relation_id=uuid4(),
                    article_id=registered.article_id,
                    revision_id=registered.revision_id,
                    subject_id=endpoints[0],
                    object_id=endpoints[1],
                    kind=kind,
                    directed=directed,
                    evidence=(
                        TextEvidence(fragment_id="chunk-1", quote="Explicit evidence."),
                    ),
                    confidence=0.9,
                ),
            )
            self.relations.extend(str(r.relation_id) for r in relations)
            self.kinds.append(kind)
        publication = Publication(
            article_id=registered.article_id,
            job_id=registered.job_id,
            attempt=1,
            attempt_token=uuid4(),
            artifact=ResolutionArtifact(
                extraction_hash=extraction_hash,
                revision_id=registered.revision_id,
                embedding_metadata=META if characters else None,
                characters=tuple(characters),
                mappings=tuple(mappings),
                relations=relations,
            ),
        )
        self.claim(publication)
        return publication

    def claim(self, request: Publication, *, expires: float | None = None) -> None:
        # Simulates ONLY the P15 claim. Publication always uses the real repository.
        self.query(
            "MATCH (j:Job {job_id: $job}) SET j.status = 'processing', "
            "j.attempt = $attempt, j.attempt_token = $token, j.lease_expires_at = $expires",
            job=str(request.job_id),
            attempt=request.attempt,
            token=str(request.attempt_token),
            expires=expires if expires is not None else time() + 120,
        )

    def state(self, request: Publication) -> dict[str, object]:
        return self.query(
            "MATCH (a:Article {article_id: $article}), (j:Job {job_id: $job}), "
            "(r:ArticleRevision {revision_id: $revision}) "
            "CALL { WITH r OPTIONAL MATCH (r)-[m:MENTIONS]->(c) "
            "RETURN count(m) AS mentions, collect(c.character_id) AS characters } "
            "CALL { WITH r OPTIONAL MATCH (r)-[:ASSERTS]->(v) "
            "RETURN count(v) AS assertions } "
            "RETURN properties(a) AS article, properties(j) AS job, "
            "properties(r) AS revision, mentions, characters, assertions",
            article=str(request.article_id),
            job=str(request.job_id),
            revision=str(request.artifact.revision_id),
        )[0]

    def active_relations(self) -> list[dict[str, object]]:
        return self.query(
            "MATCH (a:Article)-[:HAS_REVISION]->(r:ArticleRevision)-[:ASSERTS]->(v) "
            "WHERE a.article_id IN $articles AND r.revision_id = a.active_revision_id "
            "RETURN v.relation_id AS id, v.article_id AS article, v.evidence AS evidence "
            "ORDER BY id",
            articles=self.articles,
        )

    def profile(self, character_id: UUID) -> Character:
        return Character.model_validate(
            self.query(
                "MATCH (c:Character {character_id: $id}) "
                "RETURN c.character_id AS character_id, c.name AS name, "
                "c.description AS description, c.aliases AS aliases",
                id=str(character_id),
            )[0]
        )


@pytest.fixture
def db(neo4j_driver: Driver, neo4j_settings: Neo4jSettings) -> Iterator[Database]:
    bind_embedding_space(neo4j_driver, neo4j_settings, META)
    database = Database(neo4j_driver, neo4j_settings)
    try:
        yield database
    finally:
        database.query(
            "MATCH (n) WHERE n.article_id IN $articles OR n.character_id IN $characters "
            "OR n.relation_id IN $relations "
            "OR (n:Outbox AND n.job_id IN $jobs) DETACH DELETE n",
            articles=database.articles,
            characters=database.characters,
            relations=database.relations,
            jobs=[
                row["job"]
                for row in database.query(
                    "MATCH (j:Job) WHERE j.article_id IN $ids RETURN j.job_id AS job",
                    ids=database.articles,
                )
            ],
        )
        database.query(
            "MATCH (t:RelationType) WHERE t.kind IN $kinds "
            "AND NOT EXISTS { MATCH (v:RelationAssertion) WHERE v.kind = t.kind } DELETE t",
            kinds=database.kinds,
        )
        database.query(
            "MATCH (s:EmbeddingSpace {index_name: 'character_embedding', digest: $digest}) DELETE s",
            digest=META.digest,
        )


def test_publication_lazy_imports_at_runtime() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import sys",
                    "import article_graph.publication as publication",
                    "assert 'neo4j' not in sys.modules",
                    "assert 'httpx' not in sys.modules",
                    "assert 'pypdfium2' not in sys.modules",
                    "assert publication.Driver.__name__ == 'Driver'",
                    "assert 'neo4j' in sys.modules",
                ]
            ),
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.integration
def test_publish_all_records_and_evidence(db: Database) -> None:
    request = db.prepare()
    assert db.publisher.publish(request) == PublicationResult.COMPLETED
    state = db.state(request)
    assert state["mentions"] == 2 and state["assertions"] == 1
    article = ArticleRepository(db.driver, db.settings).get(request.article_id)
    assert article.status == "completed"
    assert article.active_revision_id == request.artifact.revision_id
    terminal = db.query(
        "MATCH (j:Job {job_id: $job}), (r:ArticleRevision {revision_id: $revision}) "
        "RETURN j.status AS job, r.status AS revision, "
        "j.lease_expires_at AS lease, j.publication_hash = r.publication_hash AS same_hash",
        job=str(request.job_id),
        revision=str(request.artifact.revision_id),
    )
    assert terminal == [
        {"job": "completed", "revision": "completed", "lease": None, "same_hash": True}
    ]
    rows = db.active_relations()
    assert len(rows) == 1
    assert rows[0]["id"] == str(request.artifact.relations[0].relation_id)
    assert isinstance(rows[0]["evidence"], str)
    assert json.loads(rows[0]["evidence"]) == [
        e.model_dump(mode="json") for e in request.artifact.relations[0].evidence
    ]
    endpoints = db.query(
        "MATCH (r:ArticleRevision {revision_id: $revision})-[:ASSERTS]->(v)"
        "-[:SUBJECT]->(s), (v)-[:OBJECT]->(o) "
        "RETURN s.character_id AS subject, o.character_id AS object, properties(v) AS assertion",
        revision=str(request.artifact.revision_id),
    )
    assert len(endpoints) == 1
    assert endpoints[0]["subject"] == str(request.artifact.relations[0].subject_id)
    assert endpoints[0]["object"] == str(request.artifact.relations[0].object_id)
    for character in request.artifact.characters:
        assert db.profile(character.character_id).name == character.source.name
    assert db.query(
        "MATCH (c:Character) WHERE c.character_id IN $ids "
        "RETURN DISTINCT c.embedding AS vector, c.embedding_digest AS digest",
        ids=db.characters,
    ) == [{"vector": list(VECTOR), "digest": META.digest}]


@pytest.mark.integration
def test_idempotent_commit_after_expiry_and_new_revision(db: Database) -> None:
    first = db.prepare()
    db.publisher.publish(first)
    before = db.state(first)
    assert db.publisher.publish(first) == PublicationResult.COMPLETED
    assert db.state(first) == before
    newer = db.prepare(article_id=first.article_id, empty=True)
    db.publisher.publish(newer)
    assert db.publisher.publish(first) == PublicationResult.COMPLETED
    assert (
        ArticleRepository(db.driver, db.settings)
        .get(first.article_id)
        .active_revision_id
        == newer.artifact.revision_id
    )
    assert not db.active_relations()


@pytest.mark.integration
def test_removed_relation_and_shared_character_keep_other_article_evidence(
    db: Database,
) -> None:
    first = db.prepare()
    db.publisher.publish(first)
    shared = db.profile(first.artifact.characters[0].character_id)
    second = db.prepare(shared=shared)
    db.publisher.publish(second)
    assert len(db.active_relations()) == 2
    replacement = db.prepare(
        article_id=first.article_id, shared=shared, with_relation=False
    )
    # Registration leaves the previous graph visible until successful commit.
    assert len(db.active_relations()) == 2
    db.publisher.publish(replacement)
    assert [row["id"] for row in db.active_relations()] == [
        str(second.artifact.relations[0].relation_id)
    ]
    assert db.state(first)["assertions"] == 1  # retained historical provenance
    assert db.profile(shared.character_id) == shared
    assert db.query(
        "MATCH (c:Character {character_id: $id}) RETURN count(c) AS count",
        id=str(shared.character_id),
    ) == [{"count": 1}]
    active_mentions = db.query(
        "MATCH (a:Article)-[:HAS_REVISION]->(r)-[:MENTIONS]->(c:Character {character_id: $id}) "
        "WHERE r.revision_id = a.active_revision_id RETURN count(DISTINCT a) AS count",
        id=str(shared.character_id),
    )
    assert active_mentions == [{"count": 2}]


@pytest.mark.integration
def test_empty_revision_hides_all_previous_characters(db: Database) -> None:
    first = db.prepare()
    db.publisher.publish(first)
    replacement = db.prepare(article_id=first.article_id, empty=True)
    db.publisher.publish(replacement)
    assert db.state(replacement)["mentions"] == 0
    assert db.query(
        "MATCH (a:Article {article_id: $id})-[:HAS_REVISION]->(r)-[:MENTIONS]->(c) "
        "WHERE r.revision_id = a.active_revision_id RETURN count(c) AS count",
        id=str(first.article_id),
    ) == [{"count": 0}]
    assert db.state(first)["mentions"] == 2


@pytest.mark.integration
@pytest.mark.parametrize("publish_new", [False, True])
def test_older_revision_finishes_later(db: Database, publish_new: bool) -> None:
    old = db.prepare()
    new = db.prepare(article_id=old.article_id)
    if publish_new:
        db.publisher.publish(new)
    before = db.state(new)
    assert db.publisher.publish(old) == PublicationResult.SUPERSEDED
    assert db.publisher.publish(old) == PublicationResult.SUPERSEDED
    assert db.state(new)["article"] == before["article"]
    assert db.state(new)["job"] == before["job"]
    assert db.state(old)["mentions"] == 0
    assert db.query(
        "MATCH (j:Job {job_id: $id}) RETURN j.status AS status", id=str(old.job_id)
    ) == [{"status": "superseded"}]


@pytest.mark.integration
@pytest.mark.parametrize("change_attempt", [False, True])
def test_stale_attempt_cannot_publish_or_terminate_superseded_job(
    db: Database, change_attempt: bool
) -> None:
    old = db.prepare()
    current = old.model_copy(
        update={
            "attempt": old.attempt + 1 if change_attempt else old.attempt,
            "attempt_token": old.attempt_token if change_attempt else uuid4(),
        }
    )
    db.claim(current)
    before = db.state(current)
    with pytest.raises(PublicationError, match="stale_attempt"):
        db.publisher.publish(old)
    assert db.state(current) == before
    db.prepare(article_id=old.article_id)
    before = db.state(current)
    with pytest.raises(PublicationError, match="stale_attempt"):
        db.publisher.publish(old)
    assert db.state(current) == before


@pytest.mark.integration
@pytest.mark.parametrize("status", ["processing", "queued", "failed"])
def test_expired_or_missing_processing_lease(db: Database, status: str) -> None:
    request = db.prepare()
    db.claim(request, expires=time() - 1)
    db.query(
        "MATCH (j:Job {job_id: $id}) SET j.status = $status",
        id=str(request.job_id),
        status=status,
    )
    before = db.state(request)
    with pytest.raises(PublicationError, match="lease_lost"):
        db.publisher.publish(request)
    assert db.state(request) == before


@pytest.mark.integration
def test_real_constraint_failure_rolls_back_partial_graph(db: Database) -> None:
    request = db.prepare()
    relation_id = str(request.artifact.relations[0].relation_id)
    # Existing UUID makes CREATE fail after Characters/MENTIONS/type writes.
    db.query("CREATE (:RelationAssertion {relation_id: $id})", id=relation_id)
    before = db.state(request)
    with pytest.raises(ConstraintError):
        db.publisher.publish(request)
    assert db.state(request) == before
    assert db.query(
        "MATCH (c:Character) WHERE c.character_id IN $ids RETURN count(c) AS count",
        ids=db.characters,
    ) == [{"count": 0}]
    assert db.query(
        "MATCH (v:RelationAssertion {relation_id: $id}) RETURN properties(v) AS properties",
        id=relation_id,
    ) == [{"properties": {"relation_id": relation_id}}]


@pytest.mark.integration
def test_failure_after_all_graph_writes_preserves_old_active_graph(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = db.prepare()
    db.publisher.publish(old)
    request = db.prepare(article_id=old.article_id)
    before, active = db.state(request), db.active_relations()
    original = GraphPublisher._write_graph

    def fail(
        tx: ManagedTransaction,
        request: Publication,
        characters: list[dict[str, str | list[str] | list[float]]],
        relations: list[dict[str, object]],
        kinds: list[dict[str, str | bool]],
    ) -> None:
        original(tx, request, characters, relations, kinds)
        raise RuntimeError("injected_mid_transaction_failure")

    monkeypatch.setattr(GraphPublisher, "_write_graph", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected_mid_transaction_failure"):
        db.publisher.publish(request)
    assert db.state(request) == before
    assert db.active_relations() == active


@pytest.mark.integration
@pytest.mark.parametrize("delete", [False, True])
def test_changed_or_deleted_candidate_rejected_in_transaction(
    db: Database, delete: bool
) -> None:
    first = db.prepare()
    db.publisher.publish(first)
    target = db.profile(first.artifact.characters[0].character_id)
    request = db.prepare(shared=target)
    if delete:
        db.query(
            "MATCH (c:Character {character_id: $id}) DETACH DELETE c",
            id=str(target.character_id),
        )
    else:
        db.query(
            "MATCH (c:Character {character_id: $id}) SET c.description = 'Changed context'",
            id=str(target.character_id),
        )
    before = db.state(request)
    with pytest.raises(PublicationError, match="candidate_changed"):
        db.publisher.publish(request)
    assert db.state(request) == before


@pytest.mark.integration
def test_type_direction_conflict_rolls_back_new_characters(db: Database) -> None:
    kind = "TEST_" + uuid4().hex.upper()
    first = db.prepare(kind=kind, directed=True)
    db.publisher.publish(first)
    db.publisher.publish(db.prepare(article_id=first.article_id, empty=True))
    assert not db.active_relations()
    request = db.prepare(kind=kind, directed=False)
    before = db.state(request)
    with pytest.raises(PublicationError, match="relation_direction_conflict"):
        db.publisher.publish(request)
    assert db.state(request) == before
    assert db.query(
        "MATCH (c:Character) WHERE c.character_id IN $ids RETURN count(c) AS count",
        ids=[str(c.character_id) for c in request.artifact.characters],
    ) == [{"count": 0}]
    assert db.query(
        "MATCH (t:RelationType {kind: $kind}) RETURN t.directed AS directed", kind=kind
    ) == [{"directed": True}]


@pytest.mark.integration
def test_concurrent_repeat_commit_creates_one_graph(db: Database) -> None:
    request = db.prepare()
    barrier = Barrier(4)

    def publish(_: int) -> PublicationResult:
        barrier.wait(timeout=10)
        return db.publisher.publish(request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(publish, range(4)))
    assert results == [PublicationResult.COMPLETED] * 4
    assert db.state(request)["mentions"] == 2
    assert db.state(request)["assertions"] == 1


@pytest.mark.integration
def test_changed_payload_replay_and_token_are_rejected(db: Database) -> None:
    request = db.prepare()
    db.publisher.publish(request)
    before = db.state(request)
    changed = request.model_copy(
        update={"artifact": request.artifact.model_copy(update={"relations": ()})}
    )
    with pytest.raises(PublicationError, match="publication_conflict"):
        db.publisher.publish(changed)
    with pytest.raises(PublicationError, match="stale_attempt"):
        db.publisher.publish(request.model_copy(update={"attempt_token": uuid4()}))
    assert db.state(request) == before


@pytest.mark.integration
def test_embedding_space_rechecked_before_writes(db: Database) -> None:
    request = db.prepare()
    changed = request.artifact.model_copy(
        update={"embedding_metadata": META.model_copy(update={"digest": "d" * 64})}
    )
    before = db.state(request)
    with pytest.raises(PublicationError, match="embedding_space_changed"):
        db.publisher.publish(request.model_copy(update={"artifact": changed}))
    assert db.state(request) == before


@pytest.mark.integration
def test_lease_expires_during_transaction_rolls_back_graph(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = db.prepare()
    original = GraphPublisher._write_graph

    def expire(
        tx: ManagedTransaction,
        request: Publication,
        characters: list[dict[str, str | list[str] | list[float]]],
        relations: list[dict[str, object]],
        kinds: list[dict[str, str | bool]],
    ) -> None:
        original(tx, request, characters, relations, kinds)
        # Real server clock advances past lease inside the real DB transaction.
        sleep(1.1)

    monkeypatch.setattr(GraphPublisher, "_write_graph", staticmethod(expire))
    db.claim(request, expires=time() + 1)
    before = db.state(request)
    with pytest.raises(PublicationError, match="lease_lost"):
        db.publisher.publish(request)
    assert db.state(request) == before
    assert db.query(
        "MATCH (c:Character) WHERE c.character_id IN $ids RETURN count(c) AS count",
        ids=db.characters,
    ) == [{"count": 0}]


@pytest.mark.integration
def test_registration_races_publication_under_same_article_lock(db: Database) -> None:
    old = db.prepare()
    barrier = Barrier(2)

    def publish() -> PublicationResult:
        barrier.wait(timeout=10)
        return db.publisher.publish(old)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(publish)
        barrier.wait(timeout=10)
        new = db.prepare(article_id=old.article_id)
        result = future.result(timeout=20)
    article = ArticleRepository(db.driver, db.settings).get(old.article_id)
    assert article.latest_revision_id == new.artifact.revision_id
    assert article.status == "processing"
    assert article.active_revision_id == (
        old.artifact.revision_id if result == PublicationResult.COMPLETED else None
    )
    db.publisher.publish(new)
    assert (
        ArticleRepository(db.driver, db.settings).get(old.article_id).active_revision_id
        == new.artifact.revision_id
    )


@pytest.mark.integration
def test_old_job_of_retried_revision_cannot_publish(db: Database) -> None:
    old = db.prepare()
    db.query(
        "MATCH (j:Job {job_id: $id}) SET j.status = 'failed', "
        "j.error_code = 'internal_error', j.error_message = 'Processing failed'",
        id=str(old.job_id),
    )
    retried = ArticleRepository(db.driver, db.settings).retry(old.article_id)
    before = db.state(old)
    with pytest.raises(PublicationError, match="job_changed"):
        db.publisher.publish(old)
    assert db.state(old) == before
    assert (
        ArticleRepository(db.driver, db.settings).get(old.article_id).job_id
        == retried.body.job_id
    )


@pytest.mark.integration
@pytest.mark.parametrize("field", ["revision_id", "article_id", "subject_id"])
def test_invalid_relation_cannot_write_any_graph(db: Database, field: str) -> None:
    request = db.prepare()
    relation = request.artifact.relations[0].model_copy(update={field: uuid4()})
    changed = request.model_copy(
        update={
            "artifact": request.artifact.model_copy(update={"relations": (relation,)})
        }
    )
    before = db.state(request)
    with pytest.raises(PublicationError, match="invalid_relation"):
        db.publisher.publish(changed)
    assert db.state(request) == before
