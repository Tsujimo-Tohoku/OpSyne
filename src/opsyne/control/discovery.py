"""Durable, bounded adapter proposal scheduling; activation still requires approval."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from opsyne.contracts.cases import Case, Task
from opsyne.contracts.core import Service
from opsyne.contracts.observations import RawEvent, Source
from opsyne.control.repository import Conflict, Control, identifier


class AdapterDiscovery:
    def __init__(
        self,
        control: Control,
        coalesce_seconds: int = 5,
        retry_seconds: int = 900,
        max_attempts: int = 3,
        *,
        source: Callable[[str], Source] | None = None,
    ) -> None:
        if not 0 <= coalesce_seconds <= 300:
            raise ValueError("変換案の集約時間は0〜300秒です")
        if not 1 <= retry_seconds <= 86400:
            raise ValueError("変換案の再試行間隔は1〜86400秒です")
        if not 1 <= max_attempts <= 20:
            raise ValueError("変換案の試行上限は1〜20回です")
        self.control = control
        self.coalesce_seconds = coalesce_seconds
        self.retry_seconds = retry_seconds
        self.max_attempts = max_attempts
        self.source = source
        control.db.initialize("""
            CREATE TABLE IF NOT EXISTS adapter_discovery_evidence (
                discovery_id TEXT NOT NULL, raw_id TEXT NOT NULL,
                PRIMARY KEY(discovery_id,raw_id));
        """)

    def all(self) -> list[dict[str, Any]]:
        return self.control.objects("adapter_discovery")

    def for_case(self, case_id: str) -> dict[str, Any] | None:
        return next((group for group in self.all() if group["case_id"] == case_id), None)

    @staticmethod
    def _groups(db: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT body FROM objects WHERE kind='adapter_discovery' ORDER BY rowid"
            )
        ]

    def _save(self, db: sqlite3.Connection, group: dict[str, Any]) -> None:
        self.control._put(db, "adapter_discovery", group["id"], group)

    def observe(
        self,
        case: Case,
        raw: RawEvent,
        service: Service,
        fingerprint: str,
        supported: bool,
        now: float | None = None,
    ) -> dict[str, Any]:
        if (case.service_id, case.source_id) != (raw.service_id, raw.source_id):
            raise Conflict("変換候補の案件と原本の対象が一致しません")
        if service.id != raw.service_id:
            raise Conflict("変換候補の対象が一致しません")
        at = time.time() if now is None else now
        group_id = "discovery_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        with self.control.db.connection() as db:
            try:
                group = self.control._get(db, "adapter_discovery", group_id)
                if (
                    group["case_id"],
                    group["service_id"],
                    group["source_id"],
                    group["target_instance_id"],
                    group["target_version"],
                    group["supported"],
                ) != (
                    case.id,
                    service.id,
                    raw.source_id,
                    service.instance_id,
                    service.version,
                    supported,
                ):
                    raise Conflict("変換形式のグループを別の対象へ移動できません")
            except KeyError:
                group = {
                    "id": group_id,
                    "source_id": raw.source_id,
                    "case_id": case.id,
                    "service_id": service.id,
                    "target_instance_id": service.instance_id,
                    "target_version": service.version,
                    "supported": supported,
                    "status": "COLLECTING" if supported else "BLOCKED",
                    "event_count": 0,
                    "evidence_ids": [],
                    "task_id": None,
                    "adapter_id": None,
                    "attempts": 0,
                    "attempt_event_count": 0,
                    "first_seen": at,
                    "last_seen": at,
                    "next_attempt_at": at + self.coalesce_seconds if supported else None,
                    "detail": "同じ形式の標本を集約中"
                    if supported
                    else "対応するJSONオブジェクトとして解析できないため自動提案を保留",
                }
                self.control._audit(db, "control", "adapter.discovery", group_id, case.id)
            inserted = db.execute(
                "INSERT OR IGNORE INTO adapter_discovery_evidence VALUES (?,?)",
                (group_id, raw.id),
            ).rowcount
            if inserted:
                group["event_count"] += 1
                group["evidence_ids"] = [*group["evidence_ids"], raw.id][-20:]
                group["last_seen"] = at
            self._save(db, group)
            return group

    def _scope_error(self, db: sqlite3.Connection, group: dict[str, Any]) -> str:
        try:
            service = Service.model_validate(self.control._get(db, "service", group["service_id"]))
            if not service.enabled or (service.instance_id, service.version) != (
                group["target_instance_id"],
                group["target_version"],
            ):
                return "対象が無効化または変更されています。現在の版の新しい観測が必要です"
            if self.source is not None:
                source = self.source(group["source_id"])
                if not source.enabled or source.service_id != service.id:
                    return "観測元が無効化または変更されています"
        except KeyError:
            return "観測元または対象を確認できません"
        return ""

    def _retry(self, group: dict[str, Any], disposition: str, detail: str, at: float) -> None:
        if group.get("terminal_disposition") != disposition:
            group["terminal_disposition"] = disposition
            group["next_attempt_at"] = at + self.retry_seconds
        if group["attempts"] >= self.max_attempts:
            group["status"] = "BLOCKED"
            group["detail"] = f"{detail}。自動試行上限に到達。必要なら手動で再提案してください"
        else:
            group["status"] = "RETRY_WAIT"
            group["detail"] = f"{detail}。再試行間隔の経過と新しい標本を待機"

    def _refresh(self, db: sqlite3.Connection, group: dict[str, Any], at: float) -> None:
        scope_error = self._scope_error(db, group)
        if scope_error or not group["supported"]:
            group["status"] = "BLOCKED"
            group["detail"] = scope_error or "対応するJSONオブジェクトとして解析できないため保留"
            if group["task_id"]:
                try:
                    pending = Task.model_validate(self.control._get(db, "task", group["task_id"]))
                except KeyError:
                    pending = None
                if pending is not None and pending.status == "PENDING":
                    pending = pending.model_copy(
                        update={"status": "BLOCKED", "detail": group["detail"]}
                    )
                    self.control._put(db, "task", pending.id, pending.model_dump(mode="json"))
                    self.control._audit(db, "control", "task.block", pending.id, group["detail"])
            return
        if not group["task_id"]:
            group["status"] = "COLLECTING"
            group["detail"] = "同じ形式の標本を集約中"
            return
        task_id = group["task_id"]
        try:
            task = Task.model_validate(self.control._get(db, "task", task_id))
        except KeyError:
            self._retry(group, f"{task_id}:missing", "提案タスクを確認できません", at)
            return
        adapter_id = f"adapter-{task.id}"
        try:
            adapter = self.control._get(db, "adapter", adapter_id)
        except KeyError:
            adapter = None
        if adapter is not None and adapter["status"] in {"DRAFT", "APPROVED"}:
            previous_id = group.get("adapter_id")
            if previous_id and previous_id != adapter_id:
                previous = self.control._get(db, "adapter", previous_id)
                if previous["status"] == "DRAFT":
                    previous["status"] = "REVOKED"
                    self.control._put(db, "adapter", previous_id, previous)
                    self.control._audit(
                        db, "control", "adapter.superseded", previous_id, adapter_id
                    )
            group["adapter_id"] = adapter_id
            group["status"] = "AWAITING_APPROVAL" if adapter["status"] == "DRAFT" else "COVERED"
            group["next_attempt_at"] = None
            group["detail"] = (
                "標本検証済み。人間の承認待ち" if adapter["status"] == "DRAFT" else "承認済み"
            )
            return
        if task.status in {"PENDING", "RUNNING"}:
            group["status"] = "QUEUED" if task.status == "PENDING" else "RUNNING"
            group["detail"] = "変換案の生成待ち" if task.status == "PENDING" else "変換案を生成中"
            return
        detail = task.detail or "標本を変換できる案が得られなかったため保留"
        if adapter is not None and adapter["status"] == "REVOKED":
            detail = "変換案は失効しています"
        disposition = f"{task.id}:{adapter['status'] if adapter else task.status}"
        # A rejected reservation never reached the model. It may be queued again safely,
        # unlike failed/unknown calls, and must not spend the per-format attempt budget.
        unsent = task.status == "BLOCKED" and task.attempts == 0
        if unsent and group.get("refunded_task_id") != task.id:
            group["attempts"] = max(0, group["attempts"] - 1)
            group["refunded_task_id"] = task.id
        first_terminal = group.get("terminal_disposition") != disposition
        self._retry(group, disposition, detail, at)
        group["retry_without_new_evidence"] = unsent
        if unsent:
            if first_terminal and "日次LLM呼出上限" in task.detail:
                group["next_attempt_at"] = max(at + self.retry_seconds, (at // 86400 + 1) * 86400)
            group["detail"] = f"{detail}。未送信のため利用可能な時刻に再度キューへ追加"

    def _queue(
        self, db: sqlite3.Connection, group: dict[str, Any], at: float, *, automatic: bool
    ) -> Task:
        for row in db.execute("SELECT body FROM objects WHERE kind='task'"):
            task = Task.model_validate_json(row[0])
            if (
                task.case_id == group["case_id"]
                and task.role == "adapter"
                and task.status in {"PENDING", "RUNNING"}
            ):
                if not automatic and task.status == "PENDING" and task.automatic:
                    task = task.model_copy(update={"automatic": False})
                    self.control._put(db, "task", task.id, task.model_dump(mode="json"))
                    self.control._audit(db, "control", "task.manual", task.id, group["id"])
                group["task_id"] = task.id
                self._refresh(db, group, at)
                self._save(db, group)
                return task
        task = Task(
            id=identifier("task"),
            case_id=group["case_id"],
            service_id=group["service_id"],
            role="adapter",
            created_at=at,
            expires_at=at + 300,
            discovery_id=group["id"],
            evidence_ids=tuple(group["evidence_ids"]),
            target_instance_id=group["target_instance_id"],
            target_version=group["target_version"],
            automatic=automatic,
        )
        self.control._put(db, "task", task.id, task.model_dump(mode="json"))
        self.control._audit(db, "control", "task.enqueue", task.id, group["case_id"])
        group.update(
            {
                "task_id": task.id,
                "status": "QUEUED",
                "attempts": group["attempts"] + 1,
                "attempt_event_count": group["event_count"],
                "retry_without_new_evidence": False,
                "next_attempt_at": None,
                "detail": "変換案の生成待ち",
            }
        )
        self._save(db, group)
        return task

    def schedule(self, *, enabled: bool, configured: bool, now: float | None = None) -> list[Task]:
        at = time.time() if now is None else now
        queued: list[Task] = []
        with self.control.db.connection() as db:
            for group in self._groups(db):
                previous = dict(group)
                self._refresh(db, group, at)
                ready = (
                    group["status"] in {"COLLECTING", "RETRY_WAIT"}
                    and group["next_attempt_at"] is not None
                    and group["next_attempt_at"] <= at
                    and group["attempts"] < self.max_attempts
                    and (
                        group["status"] == "COLLECTING"
                        or group.get("retry_without_new_evidence", False)
                        or group["event_count"] > group["attempt_event_count"]
                    )
                )
                if enabled and configured and ready:
                    queued.append(self._queue(db, group, at, automatic=True))
                elif group != previous:
                    self._save(db, group)
        return queued

    def request(self, case_id: str, now: float | None = None) -> Task:
        at = time.time() if now is None else now
        with self.control.db.connection() as db:
            group = next((item for item in self._groups(db) if item["case_id"] == case_id), None)
            if group is not None:
                self._refresh(db, group, at)
                reason = self._scope_error(db, group)
                if reason or not group["supported"]:
                    raise Conflict(reason or "JSONオブジェクトに対応しない原本からは提案できません")
                return self._queue(db, group, at, automatic=False)
        return self.control.enqueue(case_id, role="adapter")
