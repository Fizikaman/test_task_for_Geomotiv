"""One driver per process; sessions/transactions belong to individual operations."""

from collections.abc import Generator
from contextlib import contextmanager

lazy from neo4j import Driver, GraphDatabase

from article_graph.settings import Neo4jSettings


@contextmanager
def open_database(settings: Neo4jSettings) -> Generator[Driver]:
    if not settings.password.get_secret_value():
        raise ValueError("Neo4j password is required")
    with GraphDatabase.driver(
        settings.uri,
        auth=(settings.username, settings.password.get_secret_value()),
        connection_timeout=5,
        connection_acquisition_timeout=10,
        max_transaction_retry_time=10,
    ) as driver:
        driver.verify_connectivity()
        yield driver
