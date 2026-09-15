"""Persistent local demonstration target, separate from the runner's ledger."""

from __future__ import annotations

import time
from pathlib import Path

from opsyne.contracts.execution import OperationResult, VerificationResult
from opsyne.storage.database import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_services (
    service_id TEXT PRIMARY KEY, healthy INTEGER NOT NULL, change_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_operations (
    operation_id TEXT PRIMARY KEY, service_id TEXT NOT NULL, result TEXT NOT NULL
);
"""


class DemoConnector:
    def __init__(self, path: str | Path) -> None:
        self._database = Database(path)
        self._database.initialize(_SCHEMA)

    def configure(self, service_id: str, healthy: bool = False) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "INSERT INTO demo_services VALUES (?, ?, 0) "
                "ON CONFLICT(service_id) DO UPDATE SET healthy=excluded.healthy",
                (service_id, int(healthy)),
            )

    def execute(
        self, service_id: str, operation_id: str, *, timeout_after_apply: bool = False
    ) -> OperationResult:
        with self._database.connection() as connection:
            existing = connection.execute(
                "SELECT service_id, result FROM demo_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if existing["service_id"] != service_id:
                    raise ValueError("Operation id belongs to a different target")
                return OperationResult.model_validate_json(existing["result"])
            changed = connection.execute(
                "UPDATE demo_services SET healthy=1, change_count=change_count+1 "
                "WHERE service_id=?",
                (service_id,),
            ).rowcount
            result = OperationResult(
                status="SUCCEEDED" if changed else "FAILED",
                detail="Demo restore applied; independent check required"
                if changed
                else "Unknown demo target",
            )
            # Target state and operation history commit atomically.
            connection.execute(
                "INSERT INTO demo_operations VALUES (?, ?, ?)",
                (operation_id, service_id, result.model_dump_json()),
            )
        if timeout_after_apply:
            raise TimeoutError("Simulated lost response after target commit")
        return result

    def lookup(self, operation_id: str) -> OperationResult:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT result FROM demo_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            return OperationResult(
                status="UNKNOWN", detail="No authoritative operation result found"
            )
        return OperationResult.model_validate_json(row["result"])

    def check(self, service_id: str) -> VerificationResult:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT healthy, change_count FROM demo_services WHERE service_id=?", (service_id,)
            ).fetchone()
        if row is None:
            return VerificationResult(
                status="UNKNOWN", detail="Demo target is unavailable", checked_at=time.time()
            )
        healthy = bool(row["healthy"])
        return VerificationResult(
            status="PASS" if healthy else "FAIL",
            detail="Independent target observation is healthy"
            if healthy
            else "Target remains unhealthy",
            evidence={"healthy": healthy, "change_count": int(row["change_count"])},
            checked_at=time.time(),
        )
