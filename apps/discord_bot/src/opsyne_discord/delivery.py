"""Durable outbound notifications; this database is not OpSyne's case authority."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import httpx

State = Literal["PENDING", "SENDING", "SENT", "UNKNOWN", "FAILED"]
_API = "https://discord.com/api/v10"
_MAX_RETRY_AFTER = 86_400.0


def _numeric_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 20
        and value.isascii()
        and value.isdecimal()
        and 0 < int(value) < 2**64
    )


def _check_now(now: float) -> None:
    if not math.isfinite(now) or now < 0:
        raise ValueError("now must be a finite nonnegative timestamp")


@dataclass(frozen=True)
class Notification:
    event_id: str
    channel_id: str
    payload: dict[str, object]
    state: State
    next_attempt: float
    message_id: str | None
    last_error: str | None


class Outbox:
    """One durable row per event. Open a separate SQLite connection per operation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        with self._connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS notifications (
                    event_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('PENDING','SENDING','SENT','UNKNOWN','FAILED')),
                    next_attempt REAL NOT NULL,
                    message_id TEXT,
                    last_error TEXT
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS delivery_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    cooldown_until REAL NOT NULL
                )"""
            )
            db.execute("INSERT OR IGNORE INTO delivery_state VALUES (1, 0)")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> Notification:
        return Notification(
            event_id=str(row["event_id"]),
            channel_id=str(row["channel_id"]),
            payload=cast(dict[str, object], json.loads(row["payload"])),
            state=cast(State, row["state"]),
            next_attempt=float(row["next_attempt"]),
            message_id=row["message_id"],
            last_error=row["last_error"],
        )

    def enqueue(
        self,
        event_id: str,
        channel_id: str,
        payload: Mapping[str, object],
        now: float,
    ) -> bool:
        """Return False for an identical event; reject reuse with different content."""
        _check_now(now)
        if not event_id or not _numeric_id(channel_id):
            raise ValueError("event_id and a numeric channel_id are required")
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(f"{channel_id}\n{encoded}".encode()).hexdigest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT fingerprint FROM notifications WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("event_id already exists with different content")
                return False
            db.execute(
                """INSERT INTO notifications
                   (event_id, channel_id, payload, fingerprint, state, next_attempt)
                   VALUES (?, ?, ?, ?, 'PENDING', ?)""",
                (event_id, channel_id, encoded, fingerprint, now),
            )
            return True

    def get(self, event_id: str) -> Notification | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM notifications WHERE event_id = ?", (event_id,)
            ).fetchone()
            return None if row is None else self._record(row)

    def claim(self, now: float) -> Notification | None:
        _check_now(now)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT event_id FROM notifications
                   WHERE state = 'PENDING' AND next_attempt <= ?
                   AND ? >= (SELECT cooldown_until FROM delivery_state WHERE singleton = 1)
                   ORDER BY next_attempt, event_id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE notifications SET state = 'SENDING' WHERE event_id = ?",
                (row["event_id"],),
            )
            claimed = db.execute(
                "SELECT * FROM notifications WHERE event_id = ?", (row["event_id"],)
            ).fetchone()
            assert claimed is not None
            return self._record(claimed)

    def finish(
        self,
        event_id: str,
        state: State,
        *,
        message_id: str | None = None,
        error: str | None = None,
        next_attempt: float | None = None,
    ) -> None:
        if state == "SENDING" or (state == "SENT" and not _numeric_id(message_id)):
            raise ValueError("invalid delivery transition")
        if state == "PENDING" and next_attempt is None:
            raise ValueError("retry requires next_attempt")
        if next_attempt is not None:
            _check_now(next_attempt)
        with self._connection() as db:
            result = db.execute(
                """UPDATE notifications SET state = ?, message_id = ?, last_error = ?,
                   next_attempt = COALESCE(?, next_attempt)
                   WHERE event_id = ? AND state = 'SENDING'""",
                (state, message_id, error, next_attempt, event_id),
            )
            if result.rowcount != 1:
                raise ValueError("notification is not SENDING")
            if state == "PENDING":
                # Conservatively pause every route, including newly enqueued events.
                # Commit the event retry and shared cooldown in one transaction.
                db.execute(
                    "UPDATE delivery_state SET cooldown_until = MAX(cooldown_until, ?)",
                    (next_attempt,),
                )

    def recover_abandoned(self) -> int:
        """Call once at startup, before workers, with no other active sender."""
        with self._connection() as db:
            return db.execute(
                """UPDATE notifications SET state = 'UNKNOWN', last_error = 'sender_restarted'
                   WHERE state = 'SENDING'"""
            ).rowcount

    def reconcile(self, event_id: str, message_id: str) -> None:
        """Record an independently confirmed Discord message; never send it again."""
        if not _numeric_id(message_id):
            raise ValueError("a numeric message_id is required")
        with self._connection() as db:
            result = db.execute(
                """UPDATE notifications SET state = 'SENT', message_id = ?, last_error = NULL
                   WHERE event_id = ? AND state = 'UNKNOWN'""",
                (message_id, event_id),
            )
            if result.rowcount != 1:
                raise ValueError("notification is not UNKNOWN")


class DiscordDelivery:
    def __init__(
        self,
        outbox: Outbox,
        client: httpx.Client,
        token: str,
        allowed_channels: Collection[str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token or any(character.isspace() for character in token):
            raise ValueError("a valid bot token is required")
        if any(not _numeric_id(channel) for channel in allowed_channels):
            raise ValueError("allowed channels must be numeric IDs")
        self._outbox = outbox
        self._client = client
        self._token = token
        self._allowed_channels = frozenset(allowed_channels)
        self._clock = clock

    def run_once(self, now: float) -> bool:
        """Process at most one event. True means work was claimed, not delivery success."""
        started = self._clock()
        item = self._outbox.claim(now)
        if item is None:
            return False
        if item.channel_id not in self._allowed_channels:
            self._outbox.finish(item.event_id, "FAILED", error="channel_not_allowed")
            return True
        payload = dict(item.payload)
        payload["allowed_mentions"] = {"parse": []}
        try:
            response = self._client.post(
                f"{_API}/channels/{item.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token}"},
                json=payload,
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
            )
        except httpx.HTTPError:
            self._outbox.finish(item.event_id, "UNKNOWN", error="transport_error")
            return True
        elapsed = self._clock() - started
        try:
            body: object = response.json()
        except ValueError:
            body = None
        if response.is_success:
            message_id = body.get("id") if isinstance(body, dict) else None
            if _numeric_id(message_id):
                self._outbox.finish(item.event_id, "SENT", message_id=cast(str, message_id))
            else:
                self._outbox.finish(item.event_id, "UNKNOWN", error="invalid_success_response")
        elif response.status_code == 429:
            retry = body.get("retry_after") if isinstance(body, dict) else None
            if (
                isinstance(retry, (int, float))
                and not isinstance(retry, bool)
                and 0 < retry <= _MAX_RETRY_AFTER
                and math.isfinite(elapsed)
                and elapsed >= 0
            ):
                self._outbox.finish(
                    item.event_id,
                    "PENDING",
                    error="rate_limited",
                    next_attempt=now + elapsed + retry,
                )
            else:
                self._outbox.finish(item.event_id, "UNKNOWN", error="invalid_retry_after")
        elif 400 <= response.status_code < 500:
            self._outbox.finish(item.event_id, "FAILED", error=f"http_{response.status_code}")
        else:
            self._outbox.finish(item.event_id, "UNKNOWN", error=f"http_{response.status_code}")
        return True
