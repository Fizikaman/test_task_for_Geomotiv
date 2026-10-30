"""Durable delivery claims. Broker I/O must stay outside managed transactions."""

from dataclasses import dataclass
from uuid import UUID, uuid4

lazy from neo4j import Driver, ManagedTransaction

from article_graph.db.processing_lease import LeaseRepository
from article_graph.settings import Limits, Neo4jSettings


@dataclass(frozen=True)
class Delivery:
    outbox_id: UUID
    job_id: UUID
    token: UUID
    attempt: int


class OutboxRepository:
    def __init__(self, driver: Driver, settings: Neo4jSettings, limits: Limits) -> None:
        self.driver = driver
        self.database = settings.database
        self.leases = LeaseRepository(driver, settings, limits, seconds=120)

    def recover(self, batch_size: int, redelivery_seconds: int) -> int:
        with self.driver.session(database=self.database) as session:
            jobs = session.run(
                "MATCH (j:Job) WHERE "
                "(j.status = 'processing' AND coalesce(j.lease_expires_at, 0) "
                "<= datetime.realtime().epochMillis / 1000.0) OR "
                "(j.status = 'queued' AND coalesce(j.available_at, 0) "
                "<= datetime.realtime().epochMillis / 1000.0 AND "
                "coalesce(j.last_dispatched_at, j.created_at, 0) + $redelivery "
                "<= datetime.realtime().epochMillis / 1000.0 AND NOT EXISTS { "
                "MATCH (j)-[:HAS_OUTBOX]->(o:Outbox) "
                "WHERE o.status IN ['pending', 'dispatching'] }) "
                "RETURN j.job_id AS job ORDER BY j.created_at, j.job_id LIMIT $batch",
                redelivery=redelivery_seconds,
                batch=batch_size,
            ).value("job")
        return sum(self.leases.recover(UUID(job), redelivery_seconds) for job in jobs)

    def claim(self, seconds: int) -> Delivery | None:
        with self.driver.session(database=self.database) as session:
            # A bounded candidate list avoids holding locks during broker I/O.
            candidates = session.run(
                "MATCH (o:Outbox) WHERE "
                "(o.status = 'pending' AND o.available_at "
                "<= datetime.realtime().epochMillis / 1000.0) OR "
                "(o.status = 'dispatching' AND o.claim_expires_at "
                "<= datetime.realtime().epochMillis / 1000.0) "
                "RETURN o.outbox_id AS id ORDER BY o.available_at, o.outbox_id LIMIT 100"
            ).value("id")
            for candidate in candidates:
                delivery = session.execute_write(
                    self._claim, UUID(candidate), uuid4(), seconds
                )
                if delivery is not None:
                    return delivery
        return None

    @staticmethod
    def _claim(
        tx: ManagedTransaction, outbox_id: UUID, token: UUID, seconds: int
    ) -> Delivery | None:
        tx.run(
            "MATCH (o:Outbox {outbox_id: $id}) "
            "SET o._delivery_lock = true REMOVE o._delivery_lock",
            id=str(outbox_id),
        ).consume()
        row = tx.run(
            "MATCH (o:Outbox {outbox_id: $id}) WHERE "
            "(o.status = 'pending' AND o.available_at "
            "<= datetime.realtime().epochMillis / 1000.0) OR "
            "(o.status = 'dispatching' AND o.claim_expires_at "
            "<= datetime.realtime().epochMillis / 1000.0) "
            "SET o.status = 'dispatching', o.claim_token = $token, "
            "o.claim_expires_at = datetime.realtime().epochMillis / 1000.0 + $seconds, "
            "o.delivery_attempt = coalesce(o.delivery_attempt, 0) + 1 "
            "RETURN o.job_id AS job, o.delivery_attempt AS attempt",
            id=str(outbox_id),
            token=str(token),
            seconds=seconds,
        ).single()
        if row is None:
            return None
        return Delivery(outbox_id, UUID(row["job"]), token, row["attempt"])

    def finish(self, delivery: Delivery, *, sent: bool, delay: int = 0) -> bool:
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._finish, delivery, sent, delay)

    def _finish(
        self, tx: ManagedTransaction, delivery: Delivery, sent: bool, delay: int
    ) -> bool:
        # Same lock order as workers/recovery, then Outbox. Claim holds only Outbox.
        self.leases._state(tx, delivery.job_id)
        tx.run(
            "MATCH (o:Outbox {outbox_id: $id}) "
            "SET o._delivery_lock = true REMOVE o._delivery_lock",
            id=str(delivery.outbox_id),
        ).consume()
        row = tx.run(
            "MATCH (j:Job)-[:HAS_OUTBOX]->(o:Outbox {outbox_id: $id}) "
            "WHERE o.status = 'dispatching' AND o.claim_token = $token "
            "AND o.claim_expires_at > datetime.realtime().epochMillis / 1000.0 "
            "SET o.status = CASE WHEN $sent THEN 'sent' ELSE 'pending' END, "
            "o.sent_at = CASE WHEN $sent THEN "
            "datetime.realtime().epochMillis / 1000.0 ELSE null END, "
            "o.available_at = datetime.realtime().epochMillis / 1000.0 + $delay, "
            "j.last_dispatched_at = CASE WHEN $sent THEN "
            "datetime.realtime().epochMillis / 1000.0 ELSE j.last_dispatched_at END "
            "REMOVE o.claim_token, o.claim_expires_at RETURN o.outbox_id AS id",
            id=str(delivery.outbox_id),
            token=str(delivery.token),
            sent=sent,
            delay=delay,
        ).single()
        return row is not None
