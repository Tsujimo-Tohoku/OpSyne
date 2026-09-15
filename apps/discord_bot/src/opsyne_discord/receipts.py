"""Durable interaction deduplication, without persisting Discord interaction tokens."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, cast


class Receipts:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=0.2)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS receipts ("
                "id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT)"
            )

    def begin(self, interaction_id: str, fingerprint: str) -> tuple[bool, dict[str, Any] | None]:
        with closing(sqlite3.connect(self.path, timeout=0.2)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, response FROM receipts WHERE id = ?", (interaction_id,)
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ValueError("Interaction ID was reused with different input")
                response = None if row[1] is None else cast(dict[str, Any], json.loads(row[1]))
                return False, response
            connection.execute(
                "INSERT INTO receipts(id, fingerprint) VALUES (?, ?)",
                (interaction_id, fingerprint),
            )
            return True, None

    def finish(self, interaction_id: str, response: dict[str, Any]) -> None:
        with closing(sqlite3.connect(self.path, timeout=0.2)) as connection, connection:
            connection.execute(
                "UPDATE receipts SET response = ? WHERE id = ?",
                (json.dumps(response, ensure_ascii=False), interaction_id),
            )
