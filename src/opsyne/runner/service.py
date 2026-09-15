"""Durable execution ledger. Recovery must run before accepting work."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from opsyne.contracts.execution import (
    Capability,
    Execution,
    ExecutionPermit,
    OperationResult,
    Plan,
    plan_digest,
)
from opsyne.storage.database import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_ledger (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE,
    service_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Runner:
    """A single runner lifecycle; the unique plan key also rejects concurrent claims.

    ``authorize`` must validate the signed permit and current authoritative state.
    Operations receive the durable execution id as their idempotency key.
    Never run startup recovery in another process while a runner is live.
    """

    def __init__(self, path: str | Path) -> None:
        self._database = Database(path)
        self._database.initialize(_SCHEMA)
        self._lifecycle_lock = threading.RLock()

    @staticmethod
    def _validate(plan: Plan, permit: ExecutionPermit, capability: Capability, now: float) -> None:
        if plan.created_at > now or min(plan.expires_at, permit.expires_at) <= now:
            raise PermissionError("Plan or execution permit is outside its validity period")
        if not plan.digest or plan.digest != plan_digest(plan):
            raise PermissionError("Plan content does not match its digest")
        expected = (
            plan.id,
            plan.digest,
            plan.service_id,
            plan.target_instance_id,
            plan.target_version,
            plan.capability_id,
            plan.capability_version,
        )
        supplied = (
            permit.plan_id,
            permit.plan_digest,
            permit.service_id,
            permit.target_instance_id,
            permit.target_version,
            permit.capability_id,
            permit.capability_version,
        )
        if expected != supplied or (
            capability.id != plan.capability_id
            or capability.version != plan.capability_version
            or capability.service_id != plan.service_id
        ):
            raise PermissionError("Permit, plan, target, and capability must match")

    def get(self, execution_id: str) -> Execution:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM execution_ledger WHERE id = ?", (execution_id,)
            ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return Execution.model_validate_json(row["payload"])

    def for_plan(self, plan_id: str) -> Execution | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM execution_ledger WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return None if row is None else Execution.model_validate_json(row["payload"])

    def list(self) -> list[Execution]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM execution_ledger ORDER BY rowid"
            ).fetchall()
        return [Execution.model_validate_json(row["payload"]) for row in rows]

    def _store(self, execution: Execution) -> Execution:
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE execution_ledger SET status = ?, payload = ? WHERE id = ?",
                (execution.status, execution.model_dump_json(), execution.id),
            )
        return execution

    def execute(
        self,
        plan: Plan,
        permit: ExecutionPermit,
        capability: Capability,
        authorize: Callable[[], None],
        operation: Callable[[str], OperationResult],
        now: float | None = None,
    ) -> Execution:
        with self._lifecycle_lock:
            timestamp = time.time() if now is None else now
            self._validate(plan, permit, capability, timestamp)
            execution = Execution(
                id=str(uuid.uuid4()),
                plan_id=plan.id,
                service_id=plan.service_id,
                status="PENDING",
                created_at=timestamp,
                updated_at=timestamp,
            )
            # Commit the intent before authorization and every external side effect.
            with self._database.connection() as connection:
                row = connection.execute(
                    "SELECT payload FROM execution_ledger WHERE plan_id = ?", (plan.id,)
                ).fetchone()
                if row is not None:
                    return Execution.model_validate_json(row["payload"])
                connection.execute(
                    "INSERT INTO execution_ledger VALUES (?, ?, ?, ?, ?)",
                    (
                        execution.id,
                        execution.plan_id,
                        execution.service_id,
                        execution.status,
                        execution.model_dump_json(),
                    ),
                )
            execution = self._store(execution.model_copy(update={"status": "IN_FLIGHT"}))
            try:
                self._validate(plan, permit, capability, time.time() if now is None else now)
                authorize()
            except Exception:
                self._store(
                    execution.model_copy(
                        update={
                            "status": "FAILED",
                            "result": OperationResult(
                                status="FAILED",
                                detail="Current authorization denied; no operation sent",
                            ),
                            "updated_at": time.time() if now is None else now,
                        }
                    )
                )
                raise PermissionError("Current authorization denied; no operation sent") from None
            try:
                result = OperationResult.model_validate(operation(execution.id))
            except Exception:
                # Do not persist exception text: transports can include credentials.
                result = OperationResult(
                    status="UNKNOWN",
                    detail="Operation outcome is unknown; reconcile before any action",
                )
            return self._store(
                execution.model_copy(
                    update={
                        "status": result.status,
                        "result": result,
                        "updated_at": time.time() if now is None else now,
                    }
                )
            )

    def recover_in_flight(self, now: float | None = None) -> int:
        """Explicit startup recovery, under exclusive ownership of this ledger."""
        with self._lifecycle_lock, self._database.connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM execution_ledger WHERE status IN ('PENDING', 'IN_FLIGHT')"
            ).fetchall()
            for row in rows:
                execution = Execution.model_validate_json(row["payload"]).model_copy(
                    update={
                        "status": "UNKNOWN",
                        "result": OperationResult(
                            status="UNKNOWN", detail="Runner restarted before recording an outcome"
                        ),
                        "updated_at": time.time() if now is None else now,
                    }
                )
                connection.execute(
                    "UPDATE execution_ledger SET status = ?, payload = ? WHERE id = ?",
                    (execution.status, execution.model_dump_json(), execution.id),
                )
        return len(rows)

    def reconcile(
        self,
        execution_id: str,
        lookup: Callable[[str], OperationResult],
        now: float | None = None,
    ) -> Execution:
        """Consult a read-only operation history; never re-send the operation."""
        with self._lifecycle_lock:
            execution = self.get(execution_id)
            if execution.status != "UNKNOWN":
                return execution
            try:
                result = OperationResult.model_validate(lookup(execution.id))
            except Exception:
                result = OperationResult(
                    status="UNKNOWN", detail="Operation history is unavailable"
                )
            return self._store(
                execution.model_copy(
                    update={
                        "status": result.status,
                        "result": result,
                        "updated_at": time.time() if now is None else now,
                    }
                )
            )
