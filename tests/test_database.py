import os
import sys
from collections.abc import Iterator
from time import monotonic, sleep
from typing import LiteralString
from uuid import uuid4
lazy from subprocess import run

import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient
lazy from neo4j import Driver, Session
lazy from neo4j.exceptions import ConstraintError

from article_graph.app import create_app
from article_graph.database import open_database
from article_graph.schema import (
    COMPOSITE_PROPERTIES,
    UNIQUE_PROPERTIES,
    VECTOR_INDEX,
    SchemaNotReady,
    init_schema,
    require_ready,
    validate_embedding,
)
from article_graph.settings import LLMSettings, Neo4jSettings, Settings


@pytest.fixture
def session(neo4j_driver: Driver, neo4j_settings: Neo4jSettings) -> Iterator[Session]:
    with neo4j_driver.session(database=neo4j_settings.database) as session:
        yield session
        # Every created node carries this test-only label; no production labels deleted.
        session.run("MATCH (n:_GeomotivP03Test) DETACH DELETE n").consume()


def test_driver_lazy_import_runtime() -> None:
    result = run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import article_graph.app\n"
            "assert 'neo4j' not in sys.modules\n"
            "from article_graph.database import open_database\n"
            "from article_graph.settings import Neo4jSettings\n"
            "from pydantic import SecretStr\n"
            "s = Neo4jSettings(password=SecretStr('test-password'))\n"
            "import article_graph.database as db\n"
            "assert db.GraphDatabase.__name__ == 'GraphDatabase'\n"
            "assert 'neo4j' in sys.modules\n",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "values", [[], [0.0, 0.0], [float("nan"), 1.0], [float("inf"), 1.0], [True, 1.0]]
)
def test_invalid_embedding(values: list[float]) -> None:
    with pytest.raises(ValueError):
        validate_embedding(values, 2)


def test_numeric_list_embedding() -> None:
    assert validate_embedding([1, 2.0], 2) == [1.0, 2.0]


def test_password_required() -> None:
    from pydantic import SecretStr

    with pytest.raises(ValueError, match="password is required"):
        with open_database(Neo4jSettings(password=SecretStr(""))):
            pytest.fail("must fail before connecting")


@pytest.mark.integration
def test_init_twice_and_online(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings, session: Session
) -> None:
    before = [
        r["name"]
        for r in session.run("SHOW INDEXES YIELD name RETURN name ORDER BY name")
    ]
    init_schema(neo4j_driver, neo4j_settings)
    init_schema(neo4j_driver, neo4j_settings)
    after = [
        r["name"]
        for r in session.run("SHOW INDEXES YIELD name RETURN name ORDER BY name")
    ]
    assert before == after
    require_ready(neo4j_driver, neo4j_settings)
    assert all(row["state"] == "ONLINE" for row in session.run("SHOW INDEXES"))


@pytest.mark.integration
@pytest.mark.parametrize(("name", "label", "prop"), UNIQUE_PROPERTIES)
def test_unique_ids(
    session: Session, name: LiteralString, label: LiteralString, prop: LiteralString
) -> None:
    value = str(uuid4())
    query = f"CREATE (n:{label}:_GeomotivP03Test {{{prop}: $value}})"
    session.run(query, value=value).consume()
    with pytest.raises(ConstraintError):
        session.run(query, value=value).consume()


@pytest.mark.integration
@pytest.mark.parametrize(("name", "label", "first", "second"), COMPOSITE_PROPERTIES)
def test_composite_uniqueness(
    session: Session,
    name: LiteralString,
    label: LiteralString,
    first: LiteralString,
    second: LiteralString,
) -> None:
    value = str(uuid4())
    query = f"CREATE (n:{label}:_GeomotivP03Test {{{first}: $value, {second}: 1}})"
    session.run(query, value=value).consume()
    with pytest.raises(ConstraintError):
        session.run(query, value=value).consume()
    session.run(query, value=str(uuid4())).consume()
    session.run(
        f"CREATE (n:{label}:_GeomotivP03Test {{{first}: $value, {second}: 2}})",
        value=value,
    ).consume()


@pytest.mark.integration
def test_transaction_rollback(session: Session) -> None:
    article = str(uuid4())
    with pytest.raises(ConstraintError):
        with session.begin_transaction() as tx:
            tx.run(
                "CREATE (:Article:_GeomotivP03Test {article_id: $id})", id=article
            ).consume()
            tx.run("CREATE (:Job:_GeomotivP03Test {job_id: $id})", id=article).consume()
            tx.run(
                "CREATE (:Article:_GeomotivP03Test {article_id: $id})", id=article
            ).consume()
            tx.commit()
    row = session.run("MATCH (n:_GeomotivP03Test) RETURN count(n) AS total").single(
        strict=True
    )
    assert row["total"] == 0


@pytest.mark.integration
def test_vector_top_k(session: Session, neo4j_settings: Neo4jSettings) -> None:
    def vector(x: float, y: float) -> list[float]:
        return validate_embedding(
            [x, y] + [0.0] * (neo4j_settings.embedding_dimensions - 2),
            neo4j_settings.embedding_dimensions,
        )

    ids = [str(uuid4()) for _ in range(3)]
    for identifier, embedding in zip(
        ids, [vector(1, 0), vector(0.8, 0.6), vector(-1, 0)], strict=True
    ):
        session.run(
            "CREATE (:Character:_GeomotivP03Test {character_id: $id, embedding: $vector})",
            id=identifier,
            vector=embedding,
        ).consume()
    deadline = monotonic() + 15
    while True:
        rows = list(
            session.run(
                "CALL db.index.vector.queryNodes($index, 2, $vector) "
                "YIELD node, score RETURN node.character_id AS id, score",
                index=VECTOR_INDEX,
                vector=vector(1, 0),
            )
        )
        if [row["id"] for row in rows] == ids[:2]:
            assert rows[0]["score"] == pytest.approx(1, abs=0.001)
            assert rows[1]["score"] == pytest.approx(0.9, abs=0.001)
            break
        assert monotonic() < deadline, (
            "Vector index did not expose expected committed vectors"
        )
        sleep(0.1)
    row = session.run(
        "MATCH (n:Character {character_id: $id}) RETURN n.embedding AS v", id=ids[0]
    ).single(strict=True)
    assert row["v"] == vector(1, 0)
    assert isinstance(row["v"], list)


@pytest.mark.integration
def test_dimension_mismatch(
    neo4j_driver: Driver, neo4j_settings: Neo4jSettings
) -> None:
    wrong = neo4j_settings.model_copy(update={"embedding_dimensions": 3})
    with pytest.raises(SchemaNotReady, match="incompatible"):
        init_schema(neo4j_driver, wrong)
    require_ready(neo4j_driver, neo4j_settings)


@pytest.mark.integration
def test_missing_index_blocks_startup(
    neo4j_driver: Driver,
    neo4j_settings: Neo4jSettings,
    session: Session,
    llm_settings: LLMSettings,
) -> None:
    session.run(f"DROP INDEX {VECTOR_INDEX}").consume()
    try:
        with pytest.raises(SchemaNotReady, match="missing"):
            require_ready(neo4j_driver, neo4j_settings)
        app = create_app(
            Settings(
                _env_file=None,
                app_token="test-token-" * 4,
                jwt_access_key="test-access-key-" * 4,
                jwt_refresh_key="test-refresh-key-" * 4,
                neo4j=neo4j_settings,
                llm=llm_settings,
            )
        )
        with pytest.raises(SchemaNotReady, match="missing"):
            with TestClient(app):
                pytest.fail("Startup must fail without the vector index")
        assert app.state.driver is None
    finally:
        init_schema(neo4j_driver, neo4j_settings)


@pytest.mark.integration
def test_cli(neo4j_settings: Neo4jSettings) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ARTICLE_GRAPH_")
    }
    env.update(
        {
            "ARTICLE_GRAPH_NEO4J__URI": neo4j_settings.uri,
            "ARTICLE_GRAPH_NEO4J__USERNAME": neo4j_settings.username,
            "ARTICLE_GRAPH_NEO4J__PASSWORD": neo4j_settings.password.get_secret_value(),
            "ARTICLE_GRAPH_NEO4J__DATABASE": neo4j_settings.database,
            "ARTICLE_GRAPH_NEO4J__EMBEDDING_DIMENSIONS": str(
                neo4j_settings.embedding_dimensions
            ),
        }
    )
    for _ in range(2):
        result = run(
            [sys.executable, "-c", "from article_graph.schema import main; main()"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert result.returncode == 0, "init-schema CLI failed"
        assert result.stdout.strip() == "Neo4j schema ready"


@pytest.mark.integration
def test_app_lifecycle(
    neo4j_settings: Neo4jSettings, neo4j_driver: Driver, llm_settings: LLMSettings
) -> None:
    settings = Settings(
        _env_file=None,
        app_token="test-token-" * 4,
        jwt_access_key="test-access-key-" * 4,
        jwt_refresh_key="test-refresh-key-" * 4,
        neo4j=neo4j_settings,
        llm=llm_settings,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.driver is not None
        assert client.get("/health/ready").status_code == http_status.HTTP_200_OK
        assert client.get("/health/live").status_code == http_status.HTTP_200_OK
        with neo4j_driver.session(database=neo4j_settings.database) as session:
            session.run(f"DROP INDEX {VECTOR_INDEX}").consume()
        try:
            assert (
                client.get("/health/ready").status_code
                == http_status.HTTP_503_SERVICE_UNAVAILABLE
            )
            assert client.get("/health/live").status_code == http_status.HTTP_200_OK
        finally:
            init_schema(neo4j_driver, neo4j_settings)
    assert app.state.driver is None
