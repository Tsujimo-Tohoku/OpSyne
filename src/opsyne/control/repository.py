"""Control's durable registry, case workflow, approvals, and authorization authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import JsonValue

from opsyne.contracts.cases import Analysis, Case, Task
from opsyne.contracts.core import Actor, Service, canonical
from opsyne.contracts.execution import Capability, CheckConfig, ExecutionPermit, Plan, plan_digest
from opsyne.storage.database import Database


class Conflict(ValueError):
    """A requested change conflicts with the authoritative state."""


class Denied(PermissionError):
    """The current conditions do not authorize an operation."""


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Control:
    def __init__(self, path: str | Path, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("Signing key must contain at least 32 bytes")
        self.db = Database(path)
        self._key = signing_key
        self.db.initialize("""
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS case_keys (key TEXT PRIMARY KEY, case_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS case_evidence (
                case_id TEXT NOT NULL, raw_id TEXT NOT NULL, PRIMARY KEY(case_id,raw_id));
            CREATE TABLE IF NOT EXISTS interpretations (
                id INTEGER PRIMARY KEY, raw_id TEXT NOT NULL, digest TEXT NOT NULL,
                body TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(raw_id,digest));
            CREATE TABLE IF NOT EXISTS current_events (
                raw_id TEXT PRIMARY KEY, interpretation_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS claims (
                service_id TEXT PRIMARY KEY, plan_id TEXT UNIQUE NOT NULL, permit_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, object_id TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_scope (
                audit_id INTEGER PRIMARY KEY REFERENCES audit(id),
                service_id TEXT, object_kind TEXT,
                scope TEXT NOT NULL CHECK(scope IN ('service','global','unknown')));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('generation','1');
        """)

    @staticmethod
    def _put(db: sqlite3.Connection, kind: str, object_id: str, body: JsonValue) -> None:
        db.execute(
            "INSERT INTO objects VALUES (?,?,?) "
            "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
            (kind, object_id, canonical(body)),
        )

    @staticmethod
    def _get(db: sqlite3.Connection, kind: str, object_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT body FROM objects WHERE kind=? AND id=?", (kind, object_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"{kind}: {object_id}")
        value: dict[str, Any] = json.loads(row["body"])
        return value

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        actor: str,
        action: str,
        object_id: str,
        detail: str,
        *,
        service_id: str | None = None,
        object_kind: str | None = None,
    ) -> None:
        # Map only known event contracts, never IDs or prose heuristics.
        kinds = {
            "service.register": "service",
            "demo.create": "service",
            "check.register": "service",
            "capability.register": "capability",
            "case.evidence": "case",
            "case.resolve": "case",
            "plan.propose": "plan",
            "plan.approve": "plan",
            "plan.reject": "plan",
            "permit.issue": "permit",
            "execution.result": "plan",
            "recovery.unsent_release": "plan",
            "task.enqueue": "task",
            "task.finish": "task",
            "task.block": "task",
            "task.manual": "task",
            "investigation.request": "task",
            "evidence.retrieve": "task",
            "adapter.investigation.request": "task",
            "adapter.propose": "adapter",
            "adapter.approve": "adapter",
            "adapter.validate": "adapter",
            "adapter.revoke": "adapter",
            "adapter.superseded": "adapter",
            "adapter.explanation.update": "adapter",
            "source.register": "source_scope",
            "source.ingest": "source_scope",
            "source.reprocess": "source_scope",
            "adapter.discovery": "adapter_discovery",
        }
        object_kind = object_kind or kinds.get(action)
        if service_id is None and object_kind:
            try:
                obj = Control._get(db, object_kind, object_id)
                service_id = obj.get("service_id")
                if object_kind == "service":
                    service_id = obj["id"]
                elif object_kind == "adapter":
                    service_id = Control._get(db, "source_scope", obj["source_id"])["service_id"]
            except KeyError:
                pass
        scope = (
            "service"
            if service_id
            else "global"
            if action
            in {
                "authorization.revoke_all",
                "recovery.restore_acknowledged",
                "poll.failed",
                "worker.failed",
            }
            else "unknown"
        )
        row = db.execute(
            "INSERT INTO audit(at,actor,action,object_id,detail) VALUES (?,?,?,?,?)",
            (time.time(), actor, action, object_id, detail[:2000]),
        )
        db.execute(
            "INSERT INTO audit_scope VALUES (?,?,?,?)",
            (
                row.lastrowid,
                service_id,
                "source" if object_kind == "source_scope" else object_kind,
                scope,
            ),
        )

    def audit(
        self,
        actor: str,
        action: str,
        object_id: str,
        detail: str = "",
        *,
        service_id: str | None = None,
        object_kind: str | None = None,
    ) -> None:
        with self.db.connection() as db:
            self._audit(
                db, actor, action, object_id, detail, service_id=service_id, object_kind=object_kind
            )

    def bind_source_scope(self, source_id: str, service_id: str) -> None:
        with self.db.connection() as db:
            self._put(db, "source_scope", source_id, {"service_id": service_id})

    def scoped_history(self) -> list[dict[str, Any]]:
        with self.db.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT a.*, s.service_id, s.object_kind, COALESCE(s.scope,'unknown') AS scope "
                    "FROM audit a LEFT JOIN audit_scope s ON s.audit_id=a.id ORDER BY a.id DESC"
                )
            ]

    def page_cursor(self, payload: dict[str, Any]) -> str:
        body = canonical(payload).encode()
        signature = hmac.new(self._key, b"service-page:" + body, hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(body).decode() + "." + signature

    def read_page_cursor(self, cursor: str) -> dict[str, Any]:
        try:
            encoded, signature = cursor.split(".")
            body = base64.b64decode(encoded, altchars=b"-_", validate=True)
            expected = hmac.new(self._key, b"service-page:" + body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            value: dict[str, Any] = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("invalid cursor")
            return value
        except (ValueError, UnicodeError, TypeError) as exc:
            raise ValueError("ページカーソルが無効です") from exc

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 1000),)
                )
            ]

    def get(self, kind: str, object_id: str) -> dict[str, Any]:
        with self.db.connection() as db:
            return self._get(db, kind, object_id)

    def objects(self, kind: str) -> list[dict[str, Any]]:
        with self.db.connection() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM objects WHERE kind=? ORDER BY rowid DESC", (kind,)
                )
            ]

    def service(self, service_id: str) -> Service:
        return Service.model_validate(self.get("service", service_id))

    def register_service(self, service: Service, actor: str) -> Service:
        with self.db.connection() as db:
            try:
                old = Service.model_validate(self._get(db, "service", service.id))
            except KeyError:
                old = None
            if old is not None and old != service and service.version <= old.version:
                raise Conflict("対象の変更には新しいversionが必要です")
            if db.execute("SELECT 1 FROM claims WHERE service_id=?", (service.id,)).fetchone():
                raise Conflict("結果未確定の操作があるため対象を変更できません")
            self._put(db, "service", service.id, service.model_dump(mode="json"))
            self._audit(db, actor, "service.register", service.id, str(service.version))
        return service

    def register_capability(self, capability: Capability, actor: str) -> Capability:
        self.service(capability.service_id)
        with self.db.connection() as db:
            if db.execute(
                "SELECT 1 FROM claims WHERE service_id=?", (capability.service_id,)
            ).fetchone():
                raise Conflict("対象に結果未確定の操作があります")
            try:
                old = Capability.model_validate(self._get(db, "capability", capability.id))
            except KeyError:
                old = None
            if old is not None and old.service_id != capability.service_id:
                raise Conflict("既存の操作能力idを別の対象へ移動できません")
            if old is not None and old != capability and capability.version <= old.version:
                raise Conflict("操作能力の変更には新しいversionが必要です")
            self._put(db, "capability", capability.id, capability.model_dump(mode="json"))
            self._audit(db, actor, "capability.register", capability.id, str(capability.version))
        return capability

    def set_check(self, service_id: str, check: CheckConfig, actor: str) -> None:
        with self.db.connection() as db:
            service = Service.model_validate(self._get(db, "service", service_id))
            if db.execute("SELECT 1 FROM claims WHERE service_id=?", (service_id,)).fetchone():
                raise Conflict("結果未確定の操作があるため成功条件を変更できません")
            existing = db.execute(
                "SELECT body FROM objects WHERE kind='check' AND id=?", (service_id,)
            ).fetchone()
            if existing and json.loads(existing[0]) != check.model_dump(mode="json"):
                service = service.model_copy(update={"version": service.version + 1})
                self._put(db, "service", service_id, service.model_dump(mode="json"))
            self._put(db, "check", service_id, check.model_dump(mode="json"))
            self._audit(db, actor, "check.register", service_id, "独立確認条件")

    def record_event(self, raw_id: str, event: dict[str, Any]) -> None:
        text = canonical(event)
        sha = hashlib.sha256(text.encode()).hexdigest()
        with self.db.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO interpretations(raw_id,digest,body,created_at) "
                "VALUES (?,?,?,?)",
                (raw_id, sha, text, time.time()),
            )
            row = db.execute(
                "SELECT id FROM interpretations WHERE raw_id=? AND digest=?", (raw_id, sha)
            ).fetchone()
            db.execute(
                "INSERT INTO current_events VALUES (?,?) ON CONFLICT(raw_id) DO UPDATE "
                "SET interpretation_id=excluded.interpretation_id",
                (raw_id, row[0]),
            )

    def event(self, raw_id: str) -> dict[str, Any] | None:
        with self.db.connection() as db:
            row = db.execute(
                "SELECT i.body FROM current_events c JOIN interpretations i "
                "ON i.id=c.interpretation_id WHERE c.raw_id=?",
                (raw_id,),
            ).fetchone()
            if row is None:
                return None
            value: dict[str, Any] = json.loads(row[0])
            return value

    def add_finding(
        self,
        key: str,
        service_id: str,
        source_id: str,
        kind: str,
        title: str,
        severity: str,
        evidence_ids: list[str],
        now: float | None = None,
    ) -> Case:
        at = time.time() if now is None else now
        with self.db.connection() as db:
            row = db.execute("SELECT case_id FROM case_keys WHERE key=?", (key,)).fetchone()
            if row:
                case = Case.model_validate(self._get(db, "case", row[0]))
                if (case.service_id, case.source_id, case.kind) != (service_id, source_id, kind):
                    raise Conflict("案件の重複排除キーが異なる対象・観測源・種別と衝突しました")
            else:
                case = Case(
                    id=identifier("case"),
                    service_id=service_id,
                    source_id=source_id,
                    kind=kind,
                    title=title,
                    severity=severity,
                    created_at=at,
                    updated_at=at,
                )
                db.execute("INSERT INTO case_keys VALUES (?,?)", (key, case.id))
            new_evidence = False
            for raw_id in evidence_ids:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO case_evidence VALUES (?,?)", (case.id, raw_id)
                )
                new_evidence = new_evidence or cursor.rowcount > 0
            ids = [
                str(r[0])
                for r in db.execute(
                    "SELECT raw_id FROM case_evidence WHERE case_id=? "
                    "ORDER BY rowid DESC LIMIT 100",
                    (case.id,),
                )
            ]
            update: dict[str, Any] = {"evidence_ids": ids}
            if new_evidence:
                update["updated_at"] = at
                if case.status == "RESOLVED":
                    update["status"] = "OPEN"
            case = case.model_copy(update=update)
            self._put(db, "case", case.id, case.model_dump(mode="json"))
            if new_evidence or not row:
                self._audit(db, "detector", "case.evidence", case.id, kind)
        return case

    def case_status(self, case_id: str, status: str) -> None:
        with self.db.connection() as db:
            current = self._get(db, "case", case_id)
            current.update(status=status, updated_at=time.time())
            case = Case.model_validate(current)
            self._put(db, "case", case_id, case.model_dump(mode="json"))

    def create_plan(
        self,
        case_id: str,
        capability_id: str,
        reason: str,
        proposer: str,
        now: float | None = None,
        *,
        task_id: str | None = None,
        proposal_key: str | None = None,
    ) -> dict[str, Any]:
        at = time.time() if now is None else now
        with self.db.connection() as db:
            case = Case.model_validate(self._get(db, "case", case_id))
            service = Service.model_validate(self._get(db, "service", case.service_id))
            capability = Capability.model_validate(self._get(db, "capability", capability_id))
            check = self._get(db, "check", service.id)
            if capability.service_id != service.id or not service.enabled:
                raise Denied("操作対象が案件と一致しない、または無効です")
            if not case.evidence_ids:
                raise Denied("根拠のない操作計画は作成できません")
            if case.status == "RESOLVED":
                raise Conflict("解決済み案件には操作計画を作成できません")
            plan = Plan(
                id=identifier("plan"),
                case_id=case_id,
                service_id=service.id,
                target_instance_id=service.instance_id,
                target_version=service.version,
                capability_id=capability.id,
                capability_version=capability.version,
                proposer=proposer,
                reason=reason[:2000],
                evidence_ids=tuple(case.evidence_ids),
                impact=f"{capability.name} を対象 {service.name} に1回実行。変更内容: "
                + canonical(capability.model_dump(mode="json")),
                success_condition="登録された独立チェックがPASS: " + canonical(check),
                abort_condition="対象・能力・Policy・承認の変更、期限切れ、競合、事前確認不成立",
                created_at=at,
                expires_at=at + 3600,
                digest="",
            )
            plan = plan.model_copy(update={"digest": plan_digest(plan)})
            body = {**plan.model_dump(mode="json"), "status": "DRAFT", "approver": None}
            if task_id is not None:
                body.update(proposal_task_id=task_id, proposal_key=proposal_key)
            self._put(db, "plan", plan.id, body)
            self._audit(db, proposer, "plan.propose", plan.id, plan.digest)
            case = case.model_copy(update={"status": "AWAITING_APPROVAL", "updated_at": at})
            self._put(db, "case", case.id, case.model_dump(mode="json"))
        return body

    def resolve_verified_operation(self, plan: Plan) -> bool:
        """Resolve only the evidence covered by the plan, atomically with new findings."""
        with self.db.connection() as db:
            case = Case.model_validate(self._get(db, "case", plan.case_id))
            if case.kind != "operation_failure" or not set(case.evidence_ids).issubset(
                plan.evidence_ids
            ):
                return False
            case = case.model_copy(update={"status": "RESOLVED", "updated_at": time.time()})
            self._put(db, "case", case.id, case.model_dump(mode="json"))
            self._audit(db, "verification", "case.resolve", case.id, plan.id)
            return True

    @staticmethod
    def plan_model(body: dict[str, Any]) -> Plan:
        return Plan.model_validate(
            {key: value for key, value in body.items() if key in Plan.model_fields}
        )

    def _current(self, db: sqlite3.Connection, plan: Plan, now: float) -> None:
        case = Case.model_validate(self._get(db, "case", plan.case_id))
        if case.service_id != plan.service_id or case.status == "RESOLVED":
            raise Denied("案件の現在状態が操作計画と一致しません")
        service = Service.model_validate(self._get(db, "service", plan.service_id))
        capability = Capability.model_validate(self._get(db, "capability", plan.capability_id))
        if (
            not service.enabled
            or service.instance_id != plan.target_instance_id
            or service.version != plan.target_version
            or capability.version != plan.capability_version
            or capability.service_id != service.id
            or now >= plan.expires_at
            or now < plan.created_at
            or plan_digest(plan) != plan.digest
        ):
            raise Denied("計画の期限・対象・操作能力が現在の状態と一致しません")

    def approve(
        self,
        plan_id: str,
        expected_digest: str,
        actor: Actor,
        now: float | None = None,
        *,
        run_recovery: bool = False,
    ) -> dict[str, Any]:
        at = time.time() if now is None else now
        if actor.role not in {"admin", "approver"}:
            raise Denied("承認権限が必要です")
        with self.db.connection() as db:
            body = self._get(db, "plan", plan_id)
            plan = self.plan_model(body)
            if run_recovery:
                existing = db.execute(
                    "SELECT body FROM objects WHERE kind='recovery' AND id=?", (plan_id,)
                ).fetchone()
                if existing is not None:
                    job = json.loads(existing["body"])
                    if job["approver"] != actor.actor or job["digest"] != expected_digest:
                        raise Conflict("別の承認または計画の実行要求です")
                    return body
            self._current(db, plan, at)
            if plan.proposer == actor.actor:
                raise Denied("提案者本人は承認できません。別の承認者を使用してください")
            if body["status"] != "DRAFT" or not hmac.compare_digest(plan.digest, expected_digest):
                raise Conflict("固定された計画のdigestと状態を確認してください")
            generation = int(
                db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0]
            )
            body.update(
                status="APPROVED", approver=actor.actor, approved_at=at, generation=generation
            )
            self._put(db, "plan", plan_id, body)
            self._audit(db, actor.actor, "plan.approve", plan_id, plan.digest)
            if run_recovery:
                self._put(
                    db,
                    "recovery",
                    plan_id,
                    {
                        "id": plan_id,
                        "plan_id": plan_id,
                        "case_id": plan.case_id,
                        "service_id": plan.service_id,
                        "digest": plan.digest,
                        "approver": actor.actor,
                        "executor": "system:recovery",
                        "status": "QUEUED",
                        "execution_id": None,
                        "detail": "承認済み。サーバーで復旧処理を開始します。",
                        "updated_at": at,
                    },
                )
            return body

    def update_recovery(
        self, plan_id: str, status: str, detail: str, execution_id: str | None = None
    ) -> dict[str, Any]:
        with self.db.connection() as db:
            body = self._get(db, "recovery", plan_id)
            body.update(status=status, detail=detail[:2000], updated_at=time.time())
            if execution_id:
                body["execution_id"] = execution_id
            self._put(db, "recovery", plan_id, body)
            self._audit(
                db,
                "system:recovery",
                "recovery.progress",
                plan_id,
                status,
                service_id=body["service_id"],
                object_kind="plan",
            )
            return body

    def authorize_recovery(self, plan_id: str) -> None:
        with self.db.connection() as db:
            job = self._get(db, "recovery", plan_id)
            plan = self._get(db, "plan", plan_id)
            if (
                job["status"] not in {"QUEUED", "EXECUTING"}
                or job["digest"] != plan["digest"]
                or job["approver"] != plan.get("approver")
                or job["executor"] != "system:recovery"
            ):
                raise Denied("この固定計画を実行する承認がありません")

    def reject(self, plan_id: str, actor: Actor, reason: str) -> dict[str, Any]:
        if actor.role not in {"admin", "approver"}:
            raise Denied("承認権限が必要です")
        with self.db.connection() as db:
            body = self._get(db, "plan", plan_id)
            if db.execute("SELECT 1 FROM claims WHERE plan_id=?", (plan_id,)).fetchone():
                raise Conflict("操作結果の確認が必要です")
            if body["status"] not in {"DRAFT", "APPROVED"}:
                raise Conflict("この状態の計画は却下できません")
            body.update(status="REJECTED", rejection_reason=reason[:2000])
            self._put(db, "plan", plan_id, body)
            self._audit(db, actor.actor, "plan.reject", plan_id, reason)
            return body

    def issue_permit(self, plan_id: str, now: float | None = None) -> ExecutionPermit:
        at = time.time() if now is None else now
        with self.db.connection() as db:
            quarantine = db.execute(
                "SELECT value FROM metadata WHERE key='restore_quarantine'"
            ).fetchone()
            if quarantine and quarantine[0] == "1":
                raise Denied("復元後の操作履歴の照合と管理者の確認が必要です")
            body = self._get(db, "plan", plan_id)
            plan = self.plan_model(body)
            self._current(db, plan, at)
            generation = int(
                db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0]
            )
            if body["status"] != "APPROVED" or body.get("generation") != generation:
                raise Denied("有効な現在世代の承認がありません")
            permit = ExecutionPermit(
                id=identifier("permit"),
                plan_id=plan.id,
                plan_digest=plan.digest,
                service_id=plan.service_id,
                target_instance_id=plan.target_instance_id,
                target_version=plan.target_version,
                capability_id=plan.capability_id,
                capability_version=plan.capability_version,
                expires_at=min(at + 30, plan.expires_at),
                generation=generation,
                signature="unsigned",
            )
            signature = hmac.new(
                self._key,
                canonical(permit.model_dump(mode="json", exclude={"signature"})).encode(),
                hashlib.sha256,
            ).hexdigest()
            permit = permit.model_copy(update={"signature": signature})
            try:
                db.execute(
                    "INSERT INTO claims VALUES (?,?,?)", (plan.service_id, plan.id, permit.id)
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("対象に未確定の操作があります。照合が必要です") from exc
            self._put(db, "permit", permit.id, permit.model_dump(mode="json"))
            self._audit(db, "control", "permit.issue", permit.id, plan.digest)
        return permit

    def verify_permit(self, permit: ExecutionPermit, now: float | None = None) -> None:
        at = time.time() if now is None else now
        payload = canonical(permit.model_dump(mode="json", exclude={"signature"})).encode()
        if not hmac.compare_digest(
            hmac.new(self._key, payload, hashlib.sha256).hexdigest(), permit.signature
        ):
            raise Denied("許可の署名が無効です")
        with self.db.connection() as db:
            body = self._get(db, "plan", permit.plan_id)
            plan = self.plan_model(body)
            self._current(db, plan, at)
            stored = ExecutionPermit.model_validate(self._get(db, "permit", permit.id))
            generation = int(
                db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0]
            )
            claim = db.execute(
                "SELECT permit_id FROM claims WHERE plan_id=? AND service_id=?",
                (plan.id, plan.service_id),
            ).fetchone()
            if (
                stored != permit
                or at >= permit.expires_at
                or permit.generation != generation
                or body["status"] != "APPROVED"
                or not claim
                or claim[0] != permit.id
                or plan.digest != permit.plan_digest
            ):
                raise Denied("実行許可が失効したか、現在条件が一致しません")

    def execution_finished(self, plan_id: str, status: str) -> None:
        with self.db.connection() as db:
            body = self._get(db, "plan", plan_id)
            if status in {"SUCCEEDED", "FAILED"}:
                db.execute("DELETE FROM claims WHERE plan_id=?", (plan_id,))
                body["status"] = "EXECUTED"
                self._put(db, "plan", plan_id, body)
            self._audit(db, "runner", "execution.result", plan_id, status)

    def enqueue(
        self,
        case_id: str,
        role: Literal["operator", "sre", "security", "adapter", "periodic"] = "operator",
        *,
        discovery_id: str | None = None,
        evidence_ids: tuple[str, ...] = (),
        target_instance_id: str | None = None,
        target_version: int | None = None,
    ) -> Task:
        with self.db.connection() as db:
            case = Case.model_validate(self._get(db, "case", case_id))
            for row in db.execute("SELECT body FROM objects WHERE kind='task'"):
                task = Task.model_validate_json(row[0])
                if (
                    task.case_id == case_id
                    and task.role == role
                    and task.status in {"PENDING", "RUNNING"}
                ):
                    return task
            at = time.time()
            task = Task(
                id=identifier("task"),
                case_id=case_id,
                service_id=case.service_id,
                role=role,
                created_at=at,
                expires_at=at + 300,
                discovery_id=discovery_id,
                evidence_ids=evidence_ids,
                target_instance_id=target_instance_id,
                target_version=target_version,
            )
            self._put(db, "task", task.id, task.model_dump(mode="json"))
            self._audit(db, "control", "task.enqueue", task.id, case_id)
            return task

    def contains_evidence(self, case_id: str, evidence_ids: tuple[str, ...]) -> bool:
        """Check durable membership, independent of the case's recent display window."""
        with self.db.connection() as db:
            return all(
                db.execute(
                    "SELECT 1 FROM case_evidence WHERE case_id=? AND raw_id=?", (case_id, raw_id)
                ).fetchone()
                is not None
                for raw_id in evidence_ids
            )

    def claim_task(self, daily_limit: int, *, allow_auto_adapters: bool = True) -> Task | None:
        at = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(at))
        with self.db.connection() as db:
            for row in db.execute("SELECT body FROM objects WHERE kind='task' ORDER BY rowid"):
                task = Task.model_validate_json(row[0])
                if task.status == "RUNNING" and task.expires_at <= at:
                    task = task.model_copy(
                        update={
                            "status": "FAILED",
                            "detail": "期限内に結果を保存できませんでした。自動再送しません",
                        }
                    )
                    self._put(db, "task", task.id, task.model_dump(mode="json"))
                if task.status != "PENDING":
                    continue
                if task.automatic and not allow_auto_adapters:
                    continue
                budget = db.execute("SELECT calls FROM budget WHERE day=?", (day,)).fetchone()
                reason = "期限切れ" if task.expires_at <= at else ""
                if (int(budget[0]) if budget else 0) >= daily_limit:
                    reason = "日次LLM呼出上限に到達"
                if reason:
                    task = task.model_copy(update={"status": "BLOCKED", "detail": reason})
                    self._put(db, "task", task.id, task.model_dump(mode="json"))
                    continue
                db.execute(
                    "INSERT INTO budget VALUES (?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1",
                    (day,),
                )
                task = task.model_copy(update={"status": "RUNNING", "attempts": task.attempts + 1})
                self._put(db, "task", task.id, task.model_dump(mode="json"))
                return task
        return None

    def finish_task(self, task: Task, analysis: Analysis | None, error: str = "") -> None:
        if analysis is None and not error:
            error = "調査結果がありません。成功とは扱いません"
        with self.db.connection() as db:
            saved = task.model_copy(
                update={"status": "FAILED" if error else "SUCCEEDED", "detail": error[:1000]}
            )
            self._put(db, "task", task.id, saved.model_dump(mode="json"))
            if analysis is not None:
                report = {
                    **analysis.model_dump(mode="json"),
                    "task_id": task.id,
                    "task_role": task.role,
                    "created_at": time.time(),
                }
                self._put(db, "task_analysis", task.id, report)
                replace_summary = task.role != "adapter"
                if task.role == "adapter":
                    self._put(db, "adapter_analysis", task.case_id, report)
                    try:
                        previous = self._get(db, "analysis", task.case_id)
                    except KeyError:
                        replace_summary = True
                    else:
                        previous_role = previous.get("task_role")
                        if previous_role is None and previous.get("task_id"):
                            try:
                                previous_task = self._get(db, "task", previous["task_id"])
                            except KeyError:
                                pass
                            else:
                                previous_role = previous_task.get("role")
                        replace_summary = previous_role == "adapter"
                if replace_summary:
                    self._put(db, "analysis", task.case_id, report)
            self._audit(db, f"agent:{task.role}", "task.finish", task.id, saved.status)

    def recover_tasks(self) -> int:
        count = 0
        with self.db.connection() as db:
            for row in db.execute("SELECT body FROM objects WHERE kind='task'").fetchall():
                task = Task.model_validate_json(row[0])
                if task.status == "RUNNING":
                    task = task.model_copy(
                        update={
                            "status": "FAILED",
                            "detail": "処理中に停止。結果不明のため自動再送しません",
                        }
                    )
                    self._put(db, "task", task.id, task.model_dump(mode="json"))
                    count += 1
        return count

    def advance_generation(self, actor: str) -> int:
        with self.db.connection() as db:
            value = (
                int(db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0])
                + 1
            )
            db.execute("UPDATE metadata SET value=? WHERE key='generation'", (str(value),))
            self._audit(db, actor, "authorization.revoke_all", str(value), "承認と許可を失効")
            return value

    def recovery_holds(self) -> list[dict[str, Any]]:
        with self.db.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM claims")]

    def restore_quarantined(self) -> bool:
        with self.db.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='restore_quarantine'").fetchone()
            return row is not None and row[0] == "1"

    def release_unsent(self, plan_id: str, actor: Actor, evidence: str) -> None:
        if actor.role != "admin" or len(evidence.strip()) < 20:
            raise Denied("管理者と操作履歴を照合した根拠が必要です")
        with self.db.connection() as db:
            if not db.execute("SELECT 1 FROM claims WHERE plan_id=?", (plan_id,)).fetchone():
                raise Conflict("保留中の実行予約がありません")
            body = self._get(db, "plan", plan_id)
            body.update(status="REJECTED", rejection_reason="未送信の予約を管理者が照合して解消")
            self._put(db, "plan", plan_id, body)
            db.execute("DELETE FROM claims WHERE plan_id=?", (plan_id,))
            self._audit(db, actor.actor, "recovery.unsent_release", plan_id, evidence)

    def quarantine_restore(self) -> None:
        with self.db.connection() as db:
            db.execute(
                "INSERT INTO metadata VALUES ('restore_quarantine','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )

    def acknowledge_restore(self, actor: Actor, evidence: str) -> None:
        if actor.role != "admin" or len(evidence.strip()) < 20:
            raise Denied("管理者と復元前後の操作履歴を照合した根拠が必要です")
        with self.db.connection() as db:
            db.execute(
                "INSERT INTO metadata VALUES ('restore_quarantine','0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'"
            )
            self._audit(db, actor.actor, "recovery.restore_acknowledged", "system", evidence)
