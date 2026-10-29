"""Refresh hashes and atomic rotation, serialized on the family's root session."""

from hashlib import sha256
from time import time
from typing import Literal

lazy from neo4j import Driver, ManagedTransaction

from article_graph.auth import Claims, IssuedPair
from article_graph.settings import Neo4jSettings

type Rotation = Literal["ok", "unauthorized", "refresh_reused"]


def token_hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def _create(tx: ManagedTransaction, pair: IssuedPair) -> None:
    claims = pair.refresh
    tx.run(
        "CREATE (:RefreshSession {session_id: $id, family_id: $family, "
        "app_id: $app, token_hash: $hash, expires_at: $expires, "
        "used: false, revoked: false})",
        id=claims.jti,
        family=claims.family_id,
        app=claims.sub,
        hash=token_hash(pair.tokens.refresh_token),
        expires=claims.exp,
    ).consume()


class RefreshSessions:
    def __init__(self, driver: Driver, settings: Neo4jSettings) -> None:
        self.driver = driver
        self.database = settings.database

    def create(self, pair: IssuedPair) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(_create, pair)

    def rotate(self, old: Claims, token: str, new: IssuedPair) -> Rotation:
        def transaction(tx: ManagedTransaction) -> Rotation:
            # A write takes an exclusive node lock until commit. Read state only
            # AFTER acquiring it, in a separate query. All generations lock the
            # same root, so replay cannot race a descendant's rotation.
            root = tx.run(
                "MATCH (root:RefreshSession {session_id: $family}) "
                "SET root._refresh_lock = true REMOVE root._refresh_lock "
                "RETURN root.session_id AS id",
                family=old.family_id,
            ).single()
            if root is None:
                return "unauthorized"
            row = tx.run(
                "MATCH (s:RefreshSession {session_id: $id, family_id: $family, "
                "app_id: $app, token_hash: $hash}) "
                "RETURN s.used AS used, s.revoked AS revoked, s.expires_at AS expires",
                id=old.jti,
                family=old.family_id,
                app=old.sub,
                hash=token_hash(token),
            ).single()
            if row is None or row["expires"] <= int(time()):
                return "unauthorized"
            if row["used"]:
                tx.run(
                    "MATCH (s:RefreshSession {family_id: $family}) SET s.revoked = true",
                    family=old.family_id,
                ).consume()
                # Return, don't raise: the revocation must COMMIT before HTTP 401.
                return "refresh_reused"
            if row["revoked"]:
                return "unauthorized"
            tx.run(
                "MATCH (s:RefreshSession {session_id: $id}) SET s.used = true",
                id=old.jti,
            ).consume()
            _create(tx, new)
            return "ok"

        with self.driver.session(database=self.database) as session:
            return session.execute_write(transaction)
