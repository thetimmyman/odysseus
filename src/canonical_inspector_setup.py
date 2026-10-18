"""Explicit schema maintenance over the application's existing session factory.

No constructor/startup creates schema, enables execution or prepares an owner.
Reauthorization checks entry/finalization snapshots; it is not an atomic lock
spanning account state and SQLite. Native operational acceptance is separate.
"""
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
import asyncio
from pathlib import Path
import sys
import time
import uuid

from src import settings
from src.canonical_inspector_admission import (
    AdmissionStore, InspectorUnavailable, admissions, inspect_inspector_schema, metadata,
)
from src.constants import (CANONICAL_INSPECTOR_WORK_ROOT, CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME,
                           CANONICAL_TMOS_MANIFEST_FILE, CANONICAL_TMOS_PACKET_IDS, LOCAL_MEASUREMENT_COMMAND)


class SetupConflict(InspectorUnavailable):
    """The expected disabled, idle, fixed schema is not available for setup."""


class InspectorSchemaSetup:
    def __init__(self, sessions, *, app_state):
        self._sessions = sessions
        self._app_state = app_state

    @staticmethod
    def _authorize(owner, reauthorize):
        if reauthorize() != owner:
            raise InspectorUnavailable("inspector setup unavailable")

    def _execution_enabled(self):
        value = settings.load_settings(strict=True)
        if type(value) is not dict:
            raise InspectorUnavailable("inspector setup unavailable")
        config = value.get("canonicalInspector", {})
        if type(config) is not dict or ("enabled" in config and type(config["enabled"]) is not bool):
            raise InspectorUnavailable("inspector setup unavailable")
        return config.get("enabled") is True

    def status(self, owner, reauthorize):
        self._authorize(owner, reauthorize)
        try:
            with self._sessions() as session:
                connection = AdmissionStore._qualified_connection(session)
                result = inspect_inspector_schema(connection)
                result["connection"] = {"dialect": "sqlite", "file_backed": True,
                    "foreign_keys": connection.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                    "synchronous": connection.exec_driver_sql("PRAGMA synchronous").scalar(),
                    "journal_mode": connection.exec_driver_sql("PRAGMA journal_mode").scalar()}
                result.update(self._maintenance_snapshot(connection, result))
                result["execution_qualification"] = "NOT_RUN"
                self._authorize(owner, reauthorize)
                return result
        except (SQLAlchemyError, OSError, ValueError, TypeError, RuntimeError):
            raise InspectorUnavailable("inspector setup unavailable") from None

    def _require_disabled_owner(self):
        if self._execution_enabled():
            raise SetupConflict("Inspector execution must be disabled")
        if any(getattr(self._app_state, name, None) is not None for name in (
                "canonical_inspector_runtime", "canonical_inspector_service")):
            raise SetupConflict("Inspector owner must be stopped")

    def _require_inactive(self, connection, state, *, transport=None):
        observed = self._maintenance_snapshot(connection, state, transport=transport)
        if not observed["maintenance_available"]:
            raise SetupConflict("Inspector maintenance refused: " + observed["maintenance_refusal"])

    def _maintenance_snapshot(self, connection, state, *, transport=None):
        enabled = self._execution_enabled()
        owner_present = any(getattr(self._app_state, name, None) is not None for name in (
            "canonical_inspector_runtime", "canonical_inspector_service"))
        unresolved = None
        if state["schema_state"] == "ABSENT":
            unresolved = False
        elif state["schema_state"] == "COMPATIBLE":
            unresolved = connection.execute(select(admissions.c.id).where(
                admissions.c.reserved_slot.is_not(None)).limit(1)).first() is not None
        try:
            (Path(CANONICAL_INSPECTOR_WORK_ROOT) / CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME).lstat()
            unresolved_transport = True
        except FileNotFoundError:
            unresolved_transport = False
        transport_blocked = unresolved_transport and (transport is None or not transport.owns_guard())
        reason = ("EXECUTION_ENABLED" if enabled else "OWNER_PRESENT" if owner_present else
                  "UNRESOLVED_TRANSPORT" if transport_blocked else
                  "SCHEMA_MISMATCH" if unresolved is None else "UNRESOLVED_ADMISSION" if unresolved else None)
        return {"execution_enabled": enabled, "owner_present": owner_present,
                "unresolved_admission": unresolved, "unresolved_transport": unresolved_transport,
                "maintenance_available": reason is None,
                "maintenance_refusal": reason}

    def qualify_transport(self, owner, expected_manifest_digest, reauthorize):
        """Two app-owned operational proofs; never dispatch or grant eligibility."""
        from src.canonical_inspector_workspace import _manifest_file, manifest_digest
        from src.local_measurement_process import SystemdProcesses
        from src.local_measurement_transport import SshTransport

        self._authorize(owner, reauthorize)
        processes = None
        completed = False
        try:
            self._require_disabled_owner()
            with self._sessions() as session:
                connection = AdmissionStore._qualified_connection(session)
                self._require_inactive(connection, inspect_inspector_schema(connection))
            manifest = _manifest_file(CANONICAL_TMOS_MANIFEST_FILE)
            if type(manifest) is not dict:
                raise InspectorUnavailable("inspector setup unavailable")
            if manifest_digest(manifest) != expected_manifest_digest:
                raise SetupConflict("Inspector manifest identity changed")
            if manifest.get("principal") != {"auth_key": owner.auth_key, "account_epoch": owner.account_epoch}:
                raise InspectorUnavailable("inspector setup unavailable")
            packet_id = next((key for key in CANONICAL_TMOS_PACKET_IDS if key in manifest.get("packages", {})), None)
            deadline = time.monotonic() + 180
            transport = SshTransport(manifest, run_id=str(uuid.uuid4()), packet_id=packet_id, deadline=deadline)

            def guard():
                self._authorize(owner, reauthorize)
                self._require_disabled_owner()
                if time.monotonic() >= deadline:
                    raise InspectorUnavailable("inspector setup unavailable")
                with self._sessions() as session:
                    connection = AdmissionStore._qualified_connection(session)
                    self._require_inactive(connection, inspect_inspector_schema(connection), transport=transport)
                return True

            guard()
            command = [sys.executable, "-I", LOCAL_MEASUREMENT_COMMAND]
            processes = SystemdProcesses(worker_command=command + ["--worker-http"], broker_worker=[], transport=transport)
            result = processes.qualify_no_provider(lease_valid=guard)
            if (result.get("provider_calls") != 0 or result.get("executor_descendant_termination") is not True
                    or [row.get("kind") for row in result.get("processes", [])] != ["proof-normal", "proof-descendant"]):
                raise InspectorUnavailable("inspector setup unavailable")
            guard()
            if processes.finish_capture() is not True or processes.finalize_ownership(allowed=guard) is not True:
                raise InspectorUnavailable("inspector setup unavailable")
            completed = True
            return {"schema": "inspector-transport-proof-v1", "status": "OBSERVED_THIS_CALL",
                    "proofs_completed": 2, "app_client_stop_confirmed": True,
                    "executor_descendant_stop_confirmed": True, "provider_calls": 0,
                    "broker_calls": 0, "execution_qualification": "NOT_GRANTED"}
        except SetupConflict:
            raise
        except (SQLAlchemyError, OSError, ValueError, TypeError, RuntimeError):
            raise InspectorUnavailable("inspector setup unavailable") from None
        finally:
            if processes is not None and not completed:
                processes.request_stop()
                # This does not clear an unresolved guard, even when stop was
                # observed. Failed authority/publication needs reconciliation.
                cleanup_observed = asyncio.run(processes.abort_capture())
                if not cleanup_observed:
                    raise InspectorUnavailable("inspector transport cleanup unresolved") from None

    def initialize(self, owner, expected_schema_hash, reauthorize):
        """Explicit fixed addition, never owner preparation or execution enablement.

        A disconnected caller may have an unknown acknowledgement. Re-read the
        schema; no automatic retry or attribution of an observed prior addition.
        """
        self._authorize(owner, reauthorize)
        if (type(expected_schema_hash) is not str or len(expected_schema_hash) != 64
                or any(value not in "0123456789abcdef" for value in expected_schema_hash)):
            raise SetupConflict("Invalid expected schema identity")
        try:
            self._require_disabled_owner()
            with self._sessions() as session:
                connection = AdmissionStore._qualified_connection(session)
                if connection.connection.driver_connection.in_transaction:
                    raise SetupConflict("A separate maintenance transaction is required")
                # SQLite's legacy transaction mode does not BEGIN for DDL.
                # This explicit transaction is essential for partial-DDL rollback.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                before = inspect_inspector_schema(connection)
                self._require_inactive(connection, before)
                if before["expected_schema_hash"] != expected_schema_hash:
                    raise SetupConflict("Inspector schema identity changed")
                if before["schema_state"] == "MISMATCH":
                    raise SetupConflict("Inspector schema is partially present or incompatible")
                self._authorize(owner, reauthorize)
                changed = before["schema_state"] == "ABSENT"
                if changed:
                    metadata.create_all(bind=connection, checkfirst=False)
                after = inspect_inspector_schema(connection)
                if after["schema_state"] != "COMPATIBLE":
                    raise SetupConflict("Inspector schema addition is incompatible")
                self._require_inactive(connection, after)
                self._authorize(owner, reauthorize)
                session.commit()
                return {**after, "changed": changed, "execution_enabled": False,
                        "execution_qualification": "NOT_RUN"}
        except SetupConflict:
            raise
        except (SQLAlchemyError, OSError, ValueError, TypeError, RuntimeError):
            raise InspectorUnavailable("inspector setup unavailable") from None
