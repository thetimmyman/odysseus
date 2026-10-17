"""Feature-specific immutable origin and append-only retained locator events.

Separate metadata deliberately has no production registration or initializer.
Importing this module never imports core.database or creates a table. An owner
must separately admit a migration against the existing native engine. Isolated
file-SQLite tests exercise source guarantees; they do not qualify production
crash durability, restore, transport or execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
import uuid

from sqlalchemy import (
    CheckConstraint, Column, DDL, ForeignKey, ForeignKeyConstraint, Integer,
    MetaData, String, Table, UniqueConstraint, event, exists, insert,
    select, update,
)
from sqlalchemy.exc import SQLAlchemyError

from src.constants import CANONICAL_PILOT_ID, CANONICAL_PILOT_KEY_BYTES


class InspectorUnavailable(ValueError):
    """No acknowledged operation; never implies safe redispatch."""


class UnknownInspection(ValueError):
    """Uniform unknown/foreign/revoked/ineligible inspection refusal."""


def _digest(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Principal:
    auth_key: str
    account_epoch: str

    def __post_init__(self):
        try:
            prefix, encoded = self.account_epoch.split(":", 1)
            stamp = float.fromhex(encoded)
            valid = (prefix == "float64_hex" and type(self.auth_key) is str
                     and bool(self.auth_key) and math.isfinite(stamp) and stamp > 0
                     and stamp.hex() == encoded)
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise InspectorUnavailable("inspector unavailable")


@dataclass(frozen=True)
class PreparedPilot:
    run_id: str
    packet_id: str
    package_hash: str
    config_digest: str

    def __post_init__(self):
        if (type(self.run_id) is not str or not 0 < len(self.run_id) <= 128
                or self.packet_id != CANONICAL_PILOT_ID
                or not _digest(self.package_hash) or not _digest(self.config_digest)):
            raise InspectorUnavailable("inspector unavailable")


@dataclass(frozen=True)
class Publication:
    bundle_hash: str
    evidence_hash: str
    attempt_hash: str
    dispatch_hash: str
    disclosure_generation: str

    def __post_init__(self):
        if (not all(_digest(value) for value in (self.bundle_hash, self.evidence_hash,
                                                self.attempt_hash, self.dispatch_hash))
                or type(self.disclosure_generation) is not str
                or not 0 < len(self.disclosure_generation) <= 128):
            raise InspectorUnavailable("inspector unavailable")


@dataclass(frozen=True)
class OwnedPublication(Publication):
    run_id: str
    packet_id: str
    package_hash: str


metadata = MetaData()
admissions = Table(
    "canonical_inspector_admission", metadata,
    Column("id", String(36), primary_key=True),
    Column("principal_key", String, nullable=False),
    Column("account_epoch", String, nullable=False),
    Column("idempotency_key", String, nullable=False),
    Column("intent_digest", String(64), nullable=False),
    Column("pilot_id", String, nullable=False),
    Column("run_id", String, nullable=False, unique=True),
    Column("packet_id", String, nullable=False),
    Column("package_hash", String(64), nullable=False),
    Column("config_digest", String(64), nullable=False),
    Column("auth_origin", String, nullable=False),
    Column("admitted_at", String, nullable=False),
    Column("reserved_slot", Integer, unique=True),
    Column("slot_released_at", String),
    UniqueConstraint("principal_key", "account_epoch", "idempotency_key"),
    UniqueConstraint("id", "run_id", "packet_id", "package_hash"),
    CheckConstraint("reserved_slot IS NULL OR reserved_slot = 1"),
    CheckConstraint("(reserved_slot IS NOT NULL AND reserved_slot=1 AND slot_released_at IS NULL) OR "
                    "(reserved_slot IS NULL AND slot_released_at IS NOT NULL)"),
    CheckConstraint("auth_origin = 'admin_cookie'"),
    CheckConstraint("pilot_id = 'synthetic-uppercase' AND packet_id = 'synthetic-uppercase'"),
)
events = Table(
    "canonical_inspector_event", metadata,
    Column("id", String(36), primary_key=True),
    Column("admission_id", String(36), nullable=False),
    Column("run_id", String, nullable=False),
    Column("packet_id", String, nullable=False),
    Column("package_hash", String(64), nullable=False),
    Column("kind", String, nullable=False),
    Column("handle", String(36), unique=True),
    Column("publication_id", String(36), ForeignKey("canonical_inspector_event.id", ondelete="RESTRICT")),
    Column("initial_admission_id", String(36), ForeignKey("canonical_inspector_admission.id", ondelete="RESTRICT"), unique=True),
    Column("supersedes_publication_id", String(36), ForeignKey("canonical_inspector_event.id", ondelete="RESTRICT"), unique=True),
    Column("bundle_hash", String(64)),
    Column("evidence_hash", String(64)),
    Column("attempt_hash", String(64), nullable=False),
    Column("dispatch_hash", String(64), nullable=False),
    Column("disclosure_generation", String, nullable=False),
    Column("actor_key", String, nullable=False),
    Column("actor_epoch", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("reason_code", String, nullable=False),
    ForeignKeyConstraint(["admission_id", "run_id", "packet_id", "package_hash"],
                         ["canonical_inspector_admission.id", "canonical_inspector_admission.run_id",
                          "canonical_inspector_admission.packet_id", "canonical_inspector_admission.package_hash"],
                         ondelete="RESTRICT"),
    UniqueConstraint("publication_id"),
    CheckConstraint("(kind='published' AND handle IS NOT NULL AND publication_id IS NULL "
                    "AND bundle_hash IS NOT NULL AND evidence_hash IS NOT NULL AND "
                    "((initial_admission_id IS NOT NULL AND initial_admission_id=admission_id "
                    "AND supersedes_publication_id IS NULL) OR "
                    "(initial_admission_id IS NULL AND supersedes_publication_id IS NOT NULL))) OR "
                    "(kind='revoked' AND handle IS NULL AND publication_id IS NOT NULL "
                    "AND bundle_hash IS NULL AND evidence_hash IS NULL "
                    "AND initial_admission_id IS NULL AND supersedes_publication_id IS NULL)"),
)


interventions = Table(
    "canonical_inspector_intervention", metadata,
    Column("id", String(36), primary_key=True),
    Column("publication_id", String(36), ForeignKey("canonical_inspector_event.id", ondelete="RESTRICT"), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("receipt", String, nullable=False),
    UniqueConstraint("publication_id", "idempotency_key"),
)


def _sqlite_trigger(table, name, action, condition, message):
    ddl = (f"CREATE TRIGGER {name} BEFORE {action} ON {table.name} "
           f"WHEN {condition} BEGIN SELECT RAISE(ABORT, '{message}'); END")
    event.listen(table, "after_create", DDL(ddl).execute_if(dialect="sqlite"))


_immutable_columns = [column.name for column in admissions.columns
                      if column.name not in {"reserved_slot", "slot_released_at"}]
_changed_origin = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in _immutable_columns)
_sqlite_trigger(admissions, "canonical_admission_immutable", "UPDATE",
                f"({_changed_origin}) OR NOT (OLD.reserved_slot=1 AND NEW.reserved_slot IS NULL "
                "AND OLD.slot_released_at IS NULL AND NEW.slot_released_at IS NOT NULL)",
                "immutable admission")
_sqlite_trigger(admissions, "canonical_admission_no_delete", "DELETE", "1", "immutable admission")
_sqlite_trigger(admissions, "canonical_admission_no_replace", "INSERT",
                "EXISTS (SELECT 1 FROM canonical_inspector_admission a WHERE a.id=NEW.id "
                "OR a.run_id=NEW.run_id OR (a.principal_key=NEW.principal_key "
                "AND a.account_epoch=NEW.account_epoch AND a.idempotency_key=NEW.idempotency_key) "
                "OR (NEW.reserved_slot IS NOT NULL AND a.reserved_slot=NEW.reserved_slot))",
                "immutable admission conflict")
_sqlite_trigger(events, "canonical_event_no_update", "UPDATE", "1", "append-only event")
_sqlite_trigger(events, "canonical_event_no_delete", "DELETE", "1", "append-only event")
_sqlite_trigger(events, "canonical_event_no_replace", "INSERT",
                "EXISTS (SELECT 1 FROM canonical_inspector_event e WHERE e.id=NEW.id "
                "OR (NEW.handle IS NOT NULL AND e.handle=NEW.handle) "
                "OR (NEW.publication_id IS NOT NULL AND e.publication_id=NEW.publication_id) "
                "OR (NEW.initial_admission_id IS NOT NULL AND e.initial_admission_id=NEW.initial_admission_id) "
                "OR (NEW.supersedes_publication_id IS NOT NULL AND e.supersedes_publication_id=NEW.supersedes_publication_id))",
                "append-only event conflict")
_sqlite_trigger(events, "canonical_event_origin", "INSERT",
                "NOT EXISTS (SELECT 1 FROM canonical_inspector_admission a "
                "WHERE a.id=NEW.admission_id AND a.principal_key=NEW.actor_key "
                "AND a.account_epoch=NEW.actor_epoch)", "immutable event origin")
_sqlite_trigger(events, "canonical_event_revocation_origin", "INSERT",
                "NEW.kind='revoked' AND NOT EXISTS (SELECT 1 FROM canonical_inspector_event p "
                "WHERE p.id=NEW.publication_id AND p.kind='published' "
                "AND p.admission_id=NEW.admission_id AND p.attempt_hash=NEW.attempt_hash "
                "AND p.dispatch_hash=NEW.dispatch_hash)", "immutable revocation origin")
_sqlite_trigger(events, "canonical_event_correction_origin", "INSERT",
                "NEW.supersedes_publication_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM canonical_inspector_event p JOIN canonical_inspector_event r "
                "ON r.publication_id=p.id AND r.kind='revoked' "
                "WHERE p.id=NEW.supersedes_publication_id AND p.kind='published' "
                "AND p.admission_id=NEW.admission_id AND p.attempt_hash=NEW.attempt_hash "
                "AND p.dispatch_hash=NEW.dispatch_hash)", "immutable correction origin")

_sqlite_trigger(interventions, "canonical_intervention_no_update", "UPDATE", "1", "append-only intervention")
_sqlite_trigger(interventions, "canonical_intervention_no_delete", "DELETE", "1", "append-only intervention")
_sqlite_trigger(interventions, "canonical_intervention_no_replace", "INSERT",
                "EXISTS (SELECT 1 FROM canonical_inspector_intervention i WHERE i.id=NEW.id "
                "OR (i.publication_id=NEW.publication_id AND i.idempotency_key=NEW.idempotency_key))",
                "append-only intervention conflict")


class _DispatchClaim:
    """Process-local identity, accepted only by its creating store once."""

    def __init__(self, store, identity, owner, prepared):
        self._id, self._owner, self._prepared = identity, owner, prepared
        self._store = store
        self._execution_started = False

    def __reduce__(self):
        raise TypeError("dispatch claims cannot be serialized")


class AdmissionStore:
    def __init__(self, session_factory):
        self._sessions = session_factory
        self._claims = {}
        self._consumed = {}
        self._claim_lock = threading.Lock()

    @staticmethod
    def _qualified_connection(session):
        connection = session.connection()
        if connection.dialect.name != "sqlite":
            raise InspectorUnavailable("inspector unavailable")
        databases = connection.exec_driver_sql("PRAGMA database_list").all()
        file_backed = any(row[1] == "main" and row[2] for row in databases)
        raw = connection.connection.driver_connection
        if (not file_backed or raw.isolation_level is None
                or connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1
                or connection.exec_driver_sql("PRAGMA synchronous").scalar() not in (2, 3)
                or connection.exec_driver_sql("PRAGMA journal_mode").scalar() not in ("delete", "wal")):
            raise InspectorUnavailable("inspector unavailable")
        return connection

    def admit(self, owner: Principal, idempotency_key: str, prepared: PreparedPilot):
        if (not isinstance(owner, Principal) or not isinstance(prepared, PreparedPilot)
                or type(idempotency_key) is not str or not idempotency_key
                or len(idempotency_key.encode("utf-8")) > CANONICAL_PILOT_KEY_BYTES):
            raise InspectorUnavailable("inspector unavailable")
        identity = str(uuid.uuid4())
        intent = hashlib.sha256((CANONICAL_PILOT_ID + ":" + prepared.config_digest).encode()).hexdigest()
        try:
            with self._sessions() as session:
                self._qualified_connection(session)
                session.execute(insert(admissions).values(
                    id=identity, principal_key=owner.auth_key, account_epoch=owner.account_epoch,
                    idempotency_key=idempotency_key, intent_digest=intent, pilot_id=CANONICAL_PILOT_ID,
                    run_id=prepared.run_id, packet_id=prepared.packet_id, package_hash=prepared.package_hash,
                    config_digest=prepared.config_digest, auth_origin="admin_cookie", admitted_at=_now(),
                    reserved_slot=1, slot_released_at=None))
                session.commit()
        except (SQLAlchemyError, OSError, InspectorUnavailable):
            # A failed COMMIT may already have persisted. Never recover a claim
            # by looking up that row, and never retry this insert automatically.
            raise InspectorUnavailable("inspector unavailable") from None
        claim = _DispatchClaim(self, identity, owner, prepared)
        with self._claim_lock:
            self._claims[identity] = claim
        return claim

    def consume(self, claim, owner):
        with self._claim_lock:
            if (not isinstance(claim, _DispatchClaim) or claim._owner != owner
                    or self._claims.get(claim._id) is not claim):
                raise InspectorUnavailable("inspector unavailable")
            del self._claims[claim._id]
            self._consumed[claim._id] = claim
            return claim._prepared

    def _consumed_origin(self, claim, owner):
        with self._claim_lock:
            if (not isinstance(claim, _DispatchClaim) or claim._owner != owner
                    or self._consumed.get(claim._id) is not claim):
                raise InspectorUnavailable("inspector unavailable")
        return claim._id

    def _begin_execution(self, claim):
        """Only the exact committed claim's one owning invocation may start."""
        with self._claim_lock:
            if (type(claim) is not _DispatchClaim or self._consumed.get(claim._id) is not claim
                    or claim._store is not self or claim._execution_started):
                raise InspectorUnavailable("inspector unavailable")
            claim._execution_started = True
            return claim._prepared

    def release_terminal(self, claim, *, cleanup_confirmed):
        identity = self._consumed_origin(claim, claim._owner)
        if cleanup_confirmed is not True:
            raise InspectorUnavailable("inspector unavailable")
        try:
            with self._sessions() as session:
                self._qualified_connection(session)
                result = session.execute(update(admissions).where(
                    admissions.c.id == identity, admissions.c.reserved_slot == 1).values(
                        reserved_slot=None, slot_released_at=_now()))
                if result.rowcount != 1:
                    raise InspectorUnavailable("inspector unavailable")
                session.commit()
        except (SQLAlchemyError, OSError):
            raise InspectorUnavailable("inspector unavailable") from None
        with self._claim_lock:
            self._consumed.pop(identity, None)

    @staticmethod
    def _event_values(origin, owner, publication, *, handle=None, revoked=None, supersedes=None):
        return dict(id=str(uuid.uuid4()), admission_id=origin["id"], run_id=origin["run_id"],
                    packet_id=origin["packet_id"], package_hash=origin["package_hash"],
                    kind="revoked" if revoked else "published", handle=handle,
                    initial_admission_id=origin["id"] if not revoked and not supersedes else None,
                    supersedes_publication_id=supersedes,
                    publication_id=revoked, bundle_hash=None if revoked else publication.bundle_hash,
                    evidence_hash=None if revoked else publication.evidence_hash,
                    attempt_hash=publication.attempt_hash, dispatch_hash=publication.dispatch_hash,
                    disclosure_generation=publication.disclosure_generation,
                    actor_key=owner.auth_key, actor_epoch=owner.account_epoch,
                    recorded_at=_now(), reason_code="owner_revoked" if revoked else "retained")

    def publish(self, claim, owner, publication):
        """Native persistence primitive; service owns canonical/durability joins.

        This is not an HTTP admission or a retention acknowledgement. Trusted
        service orchestration must verify the exact retained bytes first.
        """
        identity = self._consumed_origin(claim, owner)
        if not isinstance(publication, Publication):
            raise InspectorUnavailable("inspector unavailable")
        handle = str(uuid.uuid4())
        try:
            with self._sessions() as session:
                self._qualified_connection(session)
                origin = session.execute(select(admissions).where(admissions.c.id == identity)).mappings().one()
                if session.execute(select(events.c.id).where(events.c.admission_id == identity)).first():
                    raise InspectorUnavailable("inspector unavailable")
                session.execute(insert(events).values(**self._event_values(origin, owner, publication, handle=handle)))
                session.commit()
        except (SQLAlchemyError, OSError):
            raise InspectorUnavailable("inspector unavailable") from None
        return handle

    @staticmethod
    def _owned_query(owner, handle, generation=None):
        revocations = events.alias("revocations")
        query = select(events, admissions.c.principal_key, admissions.c.account_epoch).join(
            admissions, events.c.admission_id == admissions.c.id).where(
                events.c.kind == "published", events.c.handle == handle,
                admissions.c.principal_key == owner.auth_key,
                admissions.c.account_epoch == owner.account_epoch,
                ~exists(select(revocations.c.id).where(revocations.c.publication_id == events.c.id)))
        return query.where(events.c.disclosure_generation == generation) if generation is not None else query

    def resolve(self, owner, handle, generation):
        try:
            with self._sessions() as session:
                self._qualified_connection(session)
                row = session.execute(self._owned_query(owner, handle, generation)).mappings().one_or_none()
                if row is None:
                    raise UnknownInspection("inspection unavailable")
                return OwnedPublication(row["bundle_hash"], row["evidence_hash"], row["attempt_hash"],
                                        row["dispatch_hash"], row["disclosure_generation"],
                                        row["run_id"], row["packet_id"], row["package_hash"])
        except (SQLAlchemyError, OSError, InspectorUnavailable):
            raise UnknownInspection("inspection unavailable") from None

    def interventions(self, owner, handle, generation):
        try:
            with self._sessions() as session:
                self._qualified_connection(session)
                row = session.execute(self._owned_query(owner, handle, generation)).mappings().one_or_none()
                if row is None:
                    raise UnknownInspection("inspection unavailable")
                rows = session.execute(select(interventions.c.receipt).where(
                    interventions.c.publication_id == row["id"]).order_by(interventions.c.id).limit(65)).scalars().all()
                if len(rows) > 64:
                    raise UnknownInspection("inspection unavailable")
                return [json.loads(raw) for raw in rows]
        except (SQLAlchemyError, OSError, ValueError):
            raise UnknownInspection("inspection unavailable") from None

    def request_escalation(self, owner, handle, generation, dispatch_hash, reason, key):
        if (type(reason) is not str or not reason.strip() or len(reason.encode()) > 1024
                or type(key) is not str or not 0 < len(key.encode()) <= 128):
            raise UnknownInspection("inspection unavailable")
        try:
            with self._sessions() as session:
                self._qualified_connection(session)
                row = session.execute(self._owned_query(owner, handle, generation)).mappings().one_or_none()
                if row is None or row["dispatch_hash"] != dispatch_hash:
                    raise UnknownInspection("inspection unavailable")
                existing = session.execute(select(interventions.c.receipt).where(
                    interventions.c.publication_id == row["id"], interventions.c.idempotency_key == key)).scalar_one_or_none()
                if existing is not None:
                    receipt = json.loads(existing)
                    if receipt["reason"] != reason:
                        raise UnknownInspection("inspection unavailable")
                    return receipt
                count = session.execute(select(interventions.c.id).where(
                    interventions.c.publication_id == row["id"]).limit(64)).all()
                if len(count) >= 64:
                    raise UnknownInspection("inspection unavailable")
                receipt = {"schema": "operator-intervention-request-v1", "action": "require_escalation",
                           "actor": owner.auth_key, "account_epoch": owner.account_epoch,
                           "recorded_at": _now(), "reason": reason, "status": "REQUESTED",
                           "execution_package_hash": row["package_hash"], "run_id": row["run_id"],
                           "packet_id": row["packet_id"], "attempt_receipt_hash": row["attempt_hash"],
                           "original_dispatch_receipt_hash": row["dispatch_hash"],
                           "resulting_dispatch_receipt_hash": None}
                receipt["receipt_hash"] = hashlib.sha256(json.dumps(receipt, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                session.execute(insert(interventions).values(id=str(uuid.uuid4()), publication_id=row["id"],
                                idempotency_key=key, receipt=json.dumps(receipt)))
                session.commit()
                return receipt
        except (SQLAlchemyError, OSError, ValueError):
            raise UnknownInspection("inspection unavailable") from None

    def revoke(self, owner, handle):
        self._change_publication(owner, handle, None)

    def correct(self, owner, handle, publication):
        return self._change_publication(owner, handle, publication)

    def _change_publication(self, owner, handle, replacement):
        new_handle = str(uuid.uuid4()) if replacement else None
        try:
            with self._sessions() as session:
                self._qualified_connection(session)
                row = session.execute(self._owned_query(owner, handle)).mappings().one_or_none()
                if row is None:
                    raise UnknownInspection("inspection unavailable")
                old = Publication(row["bundle_hash"], row["evidence_hash"], row["attempt_hash"],
                                  row["dispatch_hash"], row["disclosure_generation"])
                if replacement is not None and (not isinstance(replacement, Publication)
                        or replacement.attempt_hash != old.attempt_hash
                        or replacement.dispatch_hash != old.dispatch_hash):
                    raise UnknownInspection("inspection unavailable")
                origin = {name: row[name] for name in ("run_id", "packet_id", "package_hash")}
                origin["id"] = row["admission_id"]
                session.execute(insert(events).values(**self._event_values(origin, owner, old, revoked=row["id"])))
                if replacement:
                    session.execute(insert(events).values(**self._event_values(
                        origin, owner, replacement, handle=new_handle, supersedes=row["id"])))
                session.commit()
        except (SQLAlchemyError, OSError, InspectorUnavailable):
            raise UnknownInspection("inspection unavailable") from None
        return new_handle
