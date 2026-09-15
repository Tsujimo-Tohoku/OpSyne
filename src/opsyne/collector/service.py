"""Durable intake with immutable originals, delivery receipts and file cursors."""

from __future__ import annotations

import base64
import hashlib
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

from opsyne.contracts.observations import (
    Coverage,
    CoverageStatus,
    ParseStatus,
    RawEvent,
    RawInput,
    Source,
)
from opsyne.storage.database import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observation_sources (
    id TEXT PRIMARY KEY, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation_state (
    source_id TEXT PRIMARY KEY REFERENCES observation_sources(id),
    last_received REAL, cursor INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT, anchor TEXT, generation INTEGER NOT NULL DEFAULT 0,
    gap_count INTEGER NOT NULL DEFAULT 0, last_gap_at REAL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS observation_raw (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES observation_sources(id),
    service_id TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL,
    received_at REAL NOT NULL, observed_at REAL, digest TEXT NOT NULL,
    original BLOB NOT NULL, encoding_error INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_id, external_id)
);
CREATE TABLE IF NOT EXISTS observation_processing (
    event_id TEXT PRIMARY KEY REFERENCES observation_raw(id),
    acknowledged INTEGER NOT NULL DEFAULT 0, parse_status TEXT
);
CREATE INDEX IF NOT EXISTS observation_raw_source ON observation_raw(source_id, received_at);
CREATE TRIGGER IF NOT EXISTS observation_raw_no_update
BEFORE UPDATE ON observation_raw BEGIN SELECT RAISE(ABORT, 'raw events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS observation_raw_no_delete
BEFORE DELETE ON observation_raw BEGIN SELECT RAISE(ABORT, 'raw events are immutable'); END;
"""


class DuplicateConflict(ValueError):
    """The same source identity was reused for different original content."""


def _now(value: float | None) -> float:
    result = time.time() if value is None else float(value)
    if not math.isfinite(result):
        raise ValueError("time must be finite")
    return result


def _limit(value: int) -> int:
    if not 1 <= value <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return value


def _event(row: sqlite3.Row) -> RawEvent:
    return RawEvent(
        id=row["id"],
        source_id=row["source_id"],
        service_id=row["service_id"],
        external_id=row["external_id"],
        payload=row["payload"],
        received_at=row["received_at"],
        observed_at=row["observed_at"],
        digest=row["digest"],
        raw_bytes_b64=base64.b64encode(row["original"]).decode("ascii"),
        encoding_error=bool(row["encoding_error"]),
    )


class Collector:
    def __init__(self, database: Database | str | Path) -> None:
        self.db = database if isinstance(database, Database) else Database(database)
        self.db.initialize(_SCHEMA)

    def register_source(self, source: Source) -> Source:
        with self.db.connection() as connection:
            previous = connection.execute(
                "SELECT body FROM observation_sources WHERE id=?",
                (source.id,),
            ).fetchone()
            if previous:
                old = Source.model_validate_json(previous["body"])
                if (old.service_id, old.kind, old.path) != (
                    source.service_id,
                    source.kind,
                    source.path,
                ):
                    raise ValueError(
                        "source identity, kind and path are immutable; register a new ID"
                    )
            connection.execute(
                "INSERT INTO observation_sources(id,body) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (source.id, source.model_dump_json()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO observation_state(source_id) VALUES(?)",
                (source.id,),
            )
        return source

    def sources(self) -> list[Source]:
        with self.db.connection() as connection:
            return [
                Source.model_validate_json(row["body"])
                for row in connection.execute(
                    "SELECT body FROM observation_sources ORDER BY id",
                )
            ]

    @staticmethod
    def _source(connection: sqlite3.Connection, source_id: str) -> Source:
        row = connection.execute(
            "SELECT body FROM observation_sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown observation source")
        source = Source.model_validate_json(row["body"])
        if not source.enabled:
            raise ValueError("observation source is disabled")
        return source

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        source: Source,
        event: RawInput,
        now: float,
        original: bytes | None = None,
    ) -> RawEvent:
        data = event.payload.encode("utf-8") if original is None else original
        if len(data) > 1_048_576:
            raise ValueError("raw payload exceeds one MiB")
        digest = hashlib.sha256(data).hexdigest()
        previous = connection.execute(
            "SELECT * FROM observation_raw WHERE source_id=? AND external_id=?",
            (source.id, event.external_id),
        ).fetchone()
        if previous:
            if bytes(previous["original"]) != data:
                raise DuplicateConflict("external_id conflicts with a previously received event")
            return _event(previous)
        identifier = uuid.uuid4().hex
        try:
            data.decode("utf-8", errors="strict")
            encoding_error = 0
        except UnicodeDecodeError:
            encoding_error = 1
        connection.execute(
            "INSERT INTO observation_raw "
            "(id,source_id,service_id,external_id,payload,received_at,observed_at,digest,"
            "original,encoding_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                source.id,
                source.service_id,
                event.external_id,
                event.payload,
                now,
                event.observed_at,
                digest,
                data,
                encoding_error,
            ),
        )
        connection.execute(
            "INSERT INTO observation_processing(event_id) VALUES(?)",
            (identifier,),
        )
        connection.execute(
            "UPDATE observation_state SET last_received=?,last_error=NULL WHERE source_id=?",
            (now, source.id),
        )
        row = connection.execute(
            "SELECT * FROM observation_raw WHERE id=?", (identifier,)
        ).fetchone()
        assert row is not None
        return _event(row)

    def ingest(
        self,
        source_id: str,
        events: Sequence[RawInput],
        now: float | None = None,
    ) -> list[RawEvent]:
        """Commit a whole push batch, or roll it back on conflict or invalid input."""
        if len(events) > 500:
            raise ValueError("batch exceeds 500 events")
        received_at = _now(now)
        with self.db.connection() as connection:
            source = self._source(connection, source_id)
            if source.kind != "push":
                raise ValueError("file sources are ingested only by their registered collector")
            return [self._insert(connection, source, event, received_at) for event in events]

    def pending(self, limit: int = 100) -> list[RawEvent]:
        with self.db.connection() as connection:
            return [
                _event(row)
                for row in connection.execute(
                    "SELECT r.* FROM observation_raw r "
                    "JOIN observation_processing p ON p.event_id=r.id "
                    "WHERE p.acknowledged=0 ORDER BY r.received_at,r.rowid LIMIT ?",
                    (_limit(limit),),
                )
            ]

    def ack(self, event_id: str, parse_status: ParseStatus | None = None) -> None:
        """Called only after the caller has durably stored interpretation and findings."""
        if parse_status not in (None, "KNOWN", "PARTIAL", "UNKNOWN", "INVALID"):
            raise ValueError("invalid parse status")
        with self.db.connection() as connection:
            cursor = connection.execute(
                "UPDATE observation_processing SET acknowledged=1,"
                "parse_status=COALESCE(?,parse_status) WHERE event_id=?",
                (parse_status, event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown raw event")

    def raw(self, event_id: str) -> RawEvent | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM observation_raw WHERE id=?", (event_id,)
            ).fetchone()
            return _event(row) if row else None

    def record_parse_status(self, event_id: str, parse_status: ParseStatus) -> None:
        """Update interpretation accounting without acknowledging live delivery."""
        if parse_status not in ("KNOWN", "PARTIAL", "UNKNOWN", "INVALID"):
            raise ValueError("invalid parse status")
        with self.db.connection() as connection:
            cursor = connection.execute(
                "UPDATE observation_processing SET parse_status=? WHERE event_id=?",
                (parse_status, event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown raw event")

    def search(
        self,
        source_id: str | None = None,
        service_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> list[RawEvent]:
        if query is not None and len(query) > 1000:
            raise ValueError("query exceeds 1000 characters")
        with self.db.connection() as connection:
            return [
                _event(row)
                for row in connection.execute(
                    "SELECT * FROM observation_raw WHERE (? IS NULL OR source_id=?) "
                    "AND (? IS NULL OR service_id=?) AND (? IS NULL OR instr(payload,?)>0) "
                    "ORDER BY received_at DESC,rowid DESC LIMIT ?",
                    (source_id, source_id, service_id, service_id, query, query, _limit(limit)),
                )
            ]

    def iter_raw(self, source_id: str | None = None) -> Iterator[RawEvent]:
        """Iterate a retained-log snapshot in bounded batches without a transaction over yields."""
        with self.db.connection() as connection:
            ceiling = connection.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM observation_raw"
            ).fetchone()[0]
        cursor = 0
        while cursor < ceiling:
            with self.db.connection() as connection:
                rows = connection.execute(
                    "SELECT rowid AS sequence,* FROM observation_raw "
                    "WHERE rowid>? AND rowid<=? AND (? IS NULL OR source_id=?) "
                    "ORDER BY rowid LIMIT 500",
                    (cursor, ceiling, source_id, source_id),
                ).fetchall()
            if not rows:
                break
            cursor = rows[-1]["sequence"]
            for row in rows:
                yield _event(row)

    def coverage(self, now: float | None = None) -> list[Coverage]:
        timestamp = _now(now)
        results: list[Coverage] = []
        with self.db.connection() as connection:
            for row in connection.execute(
                "SELECT s.body,t.* FROM observation_sources s "
                "JOIN observation_state t ON t.source_id=s.id ORDER BY s.id",
            ):
                source = Source.model_validate_json(row["body"])
                last = row["last_received"]
                status: CoverageStatus = "healthy"
                if not source.enabled:
                    status = "disabled"
                elif last is None or row["last_error"] is not None:
                    status = "missing"
                elif timestamp - last > source.stale_after_seconds:
                    status = "stale"
                counts = connection.execute(
                    "SELECT SUM(p.acknowledged=0) AS pending,"
                    "SUM(p.parse_status IN ('UNKNOWN','PARTIAL')) AS unknown,"
                    "SUM(p.parse_status='INVALID') AS failed FROM observation_raw r "
                    "JOIN observation_processing p ON p.event_id=r.id WHERE r.source_id=?",
                    (source.id,),
                ).fetchone()
                results.append(
                    Coverage(
                        source_id=source.id,
                        service_id=source.service_id,
                        status=status,
                        last_received=last,
                        cursor=row["cursor"] if source.kind == "file" else None,
                        pending_count=counts["pending"] or 0,
                        unknown_count=counts["unknown"] or 0,
                        parse_failures=counts["failed"] or 0,
                        gap_count=row["gap_count"],
                        last_gap_at=row["last_gap_at"],
                        last_error=row["last_error"],
                    )
                )
        return results

    def poll_files(self, now: float | None = None) -> list[RawEvent]:
        """Read bounded complete lines, atomically committing originals with each cursor."""
        timestamp = _now(now)
        collected: list[RawEvent] = []
        for source in self.sources():
            if source.kind != "file" or not source.enabled:
                continue
            try:
                collected.extend(self._poll_file(source, timestamp))
            except (OSError, ValueError) as exc:
                with self.db.connection() as connection:
                    connection.execute(
                        "UPDATE observation_state SET last_error=? WHERE source_id=?",
                        (f"{type(exc).__name__}: {exc}", source.id),
                    )
        return collected

    def _poll_file(self, source: Source, now: float) -> list[RawEvent]:
        assert source.path is not None
        with self.db.connection() as connection, Path(source.path).open("rb") as stream:
            source = self._source(connection, source.id)
            state = connection.execute(
                "SELECT * FROM observation_state WHERE source_id=?",
                (source.id,),
            ).fetchone()
            assert state is not None
            stat = os.fstat(stream.fileno())
            fingerprint = f"{stat.st_dev}:{stat.st_ino}"
            offset = int(state["cursor"])
            generation = int(state["generation"])
            stream.seek(max(0, offset - 256))
            previous_anchor = hashlib.sha256(stream.read(min(offset, 256))).hexdigest()
            gap = state["fingerprint"] is not None and (
                state["fingerprint"] != fingerprint
                or stat.st_size < offset
                or (state["anchor"] is not None and state["anchor"] != previous_anchor)
            )
            if gap:
                offset = 0
                generation += 1
            stream.seek(offset)
            chunk = stream.read(1_048_577)
            if b"\n" not in chunk and len(chunk) > 1_048_576:
                raise ValueError("file line exceeds one MiB")
            completed: list[RawEvent] = []
            for content in chunk.split(b"\n")[:-1]:
                if len(completed) >= 500:
                    break
                line = content + b"\n"
                external_id = f"file:{generation}:{offset}:{len(line)}"
                event = RawInput(external_id=external_id, payload=line.decode("utf-8", "replace"))
                completed.append(self._insert(connection, source, event, now, original=line))
                offset += len(line)
            stream.seek(max(0, offset - 256))
            anchor = hashlib.sha256(stream.read(min(offset, 256))).hexdigest()
            connection.execute(
                "UPDATE observation_state SET cursor=?,fingerprint=?,anchor=?,generation=?,"
                "gap_count=gap_count+?,last_gap_at=CASE WHEN ? THEN ? ELSE last_gap_at END,"
                "last_error=NULL WHERE source_id=?",
                (offset, fingerprint, anchor, generation, int(gap), int(gap), now, source.id),
            )
            return completed
