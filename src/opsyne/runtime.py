"""Local composition root. Product domains never import this module."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from opsyne.agents.adapter import AdapterAgent
from opsyne.agents.investigator import Investigator
from opsyne.collector.service import Collector
from opsyne.connectors.demo import DemoConnector
from opsyne.connectors.http import HttpConnector
from opsyne.contracts.adapter_reviews import AgentExplanation
from opsyne.contracts.cases import Analysis, Case, EvidenceGrant, Task
from opsyne.contracts.core import Actor, Service
from opsyne.contracts.execution import Capability, CheckConfig, Execution, VerificationResult
from opsyne.contracts.observations import AdapterDefinition, Finding, RawEvent, RawInput, Source
from opsyne.control.adapters import AdapterRegistry
from opsyne.control.discovery import AdapterDiscovery
from opsyne.control.repository import Conflict, Control, Denied
from opsyne.detection.engine import detect, detect_coverage
from opsyne.gateway.service import EvidenceGateway
from opsyne.normalization.engine import missing_adapter_paths, normalize
from opsyne.normalization.formats import format_fingerprint, supported_json
from opsyne.runner.service import Runner
from opsyne.storage.instance import InstanceLock
from opsyne.verification.service import VerificationService


class Runtime:
    def __init__(
        self,
        data_dir: Path,
        api_key: str | None = None,
        daily_call_limit: int = 20,
        auto_investigate: bool = False,
        auto_adapter_proposals: bool = True,
        adapter_coalesce_seconds: int = 5,
        adapter_retry_seconds: int = 900,
        adapter_max_attempts: int = 3,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if (self.data_dir / "restore.pending").exists():
            raise ValueError("復元が未完了です。完全なバックアップから新しい場所へ復元してください")
        databases = ("control.sqlite3", "collector.sqlite3", "runner.sqlite3", "demo.sqlite3")
        present = [name for name in databases if (self.data_dir / name).exists()]
        if present and (
            len(present) != len(databases) or not (self.data_dir / "signing.key").exists()
        ):
            raise ValueError(
                "保存状態が不完全です。DBと署名鍵を揃えたバックアップから復元してください"
            )
        if not 1 <= daily_call_limit <= 1000:
            raise ValueError("日次LLM呼出上限は1から1000の範囲で設定してください")
        key_path = self.data_dir / "signing.key"
        if not key_path.exists():
            try:
                descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as file:
                    file.write(secrets.token_bytes(32))
                    file.flush()
                    os.fsync(file.fileno())
            except FileExistsError:
                pass
        self.control = Control(self.data_dir / "control.sqlite3", key_path.read_bytes())
        self.collector = Collector(self.data_dir / "collector.sqlite3")
        for registered_source in self.collector.sources():
            self.control.bind_source_scope(registered_source.id, registered_source.service_id)
        self.runner = Runner(self.data_dir / "runner.sqlite3")
        self.demo = DemoConnector(self.data_dir / "demo.sqlite3")
        self.http = HttpConnector()
        self.verifier = VerificationService()
        self.adapters = AdapterRegistry(self.control, self.source)
        self.discoveries = AdapterDiscovery(
            self.control,
            coalesce_seconds=adapter_coalesce_seconds,
            retry_seconds=adapter_retry_seconds,
            max_attempts=adapter_max_attempts,
            source=self.source,
        )
        self.investigator = Investigator(api_key=api_key, model="gpt-5.6-luna")
        self.adapter_agent = AdapterAgent(api_key=api_key, model="gpt-5.6-luna")
        self.llm_configured = bool(api_key)
        self.daily_call_limit = daily_call_limit
        self.auto_investigate = auto_investigate
        self.auto_adapter_proposals = auto_adapter_proposals
        self.adapter_coalesce_seconds = adapter_coalesce_seconds
        self.adapter_retry_seconds = adapter_retry_seconds
        self.adapter_max_attempts = adapter_max_attempts
        self.gateway = EvidenceGateway(self.collector.raw, self.validate_grant)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._execution_lock = threading.RLock()
        self._instance = InstanceLock(self.data_dir / "instance.lock")
        self._threads: list[threading.Thread] = []
        self.last_poll: float | None = None
        self.last_poll_error: str | None = None
        self.last_task_error: str | None = None

    def source(self, source_id: str) -> Source:
        for source in self.collector.sources():
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def start(self) -> None:
        self._instance.acquire()
        try:
            self.runner.recover_in_flight()
            self.control.recover_tasks()
        except BaseException:
            self._instance.release()
            raise
        self._stop.clear()
        for target, name in [
            (self._observation_loop, "opsyne-collector"),
            (self._investigation_loop, "opsyne-investigator"),
            (self._recovery_loop, "opsyne-recovery"),
        ]:
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=35)
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("処理の停止待ちです。状態ロックを維持します")
        self._threads.clear()
        self._instance.release()

    def _observation_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as exc:
                self.last_poll_error = type(exc).__name__
                with suppress(Exception):
                    self.control.audit("collector", "poll.failed", "system", type(exc).__name__)
            self._stop.wait(2)

    def _investigation_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_task()
                self.last_task_error = None
            except Exception as exc:
                self.last_task_error = type(exc).__name__
                with suppress(Exception):
                    self.control.audit("agent", "worker.failed", "system", type(exc).__name__)
            self._stop.wait(1)

    def _finding(self, finding: Finding) -> Case:
        return self.control.add_finding(
            finding.dedup_key,
            finding.service_id,
            finding.source_id,
            finding.kind,
            finding.title,
            finding.severity,
            finding.evidence_ids,
        )

    def _recovery_loop(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                self.run_recovery()
            self._stop.wait(1)

    def run_recovery(self) -> dict[str, Any] | None:
        with self._execution_lock:
            for job in self.control.objects("recovery"):
                if job["status"] not in {"QUEUED", "EXECUTING", "VERIFYING"}:
                    continue
                plan_id = job["plan_id"]
                execution = self.runner.for_plan(plan_id)
                try:
                    if execution is None:
                        if job["status"] != "QUEUED":
                            return self.control.update_recovery(
                                plan_id,
                                "BLOCKED",
                                "実行開始中に停止しました。実行台帳と予約を照合してください。",
                            )
                        self.control.authorize_recovery(plan_id)
                        self.control.update_recovery(
                            plan_id, "EXECUTING", "登録された復旧操作を実行しています。"
                        )
                        execution = self.execute(
                            plan_id, Actor(actor="system:recovery", role="operator")
                        )
                    if execution.status != "SUCCEEDED":
                        return self.control.update_recovery(
                            plan_id,
                            "FAILED" if execution.status == "FAILED" else "UNKNOWN",
                            "操作結果を実行台帳で確認してください。自動再実行しません。",
                            execution.id,
                        )
                    self.control.update_recovery(
                        plan_id, "VERIFYING", "サービスの正常性を確認しています。", execution.id
                    )
                    verification = self.verify(
                        execution.id, Actor(actor="system:recovery", role="operator")
                    )
                    return self.control.update_recovery(
                        plan_id,
                        "COMPLETED" if verification.status == "PASS" else "CHECK_FAILED",
                        "復旧処理と正常性確認が完了しました。"
                        if verification.status == "PASS"
                        else "操作は成功しましたが正常性を確認できません。再確認してください。",
                        execution.id,
                    )
                except Exception as exc:
                    return self.control.update_recovery(
                        plan_id,
                        "BLOCKED" if execution is None else "CHECK_FAILED",
                        str(exc)
                        if isinstance(exc, (Denied, Conflict))
                        else "処理結果を確認できません。実行台帳を確認してください。",
                        execution.id if execution else None,
                    )
        return None

    def process(self, raw: RawEvent, reprocess: bool = False) -> None:
        service = self.control.service(raw.service_id)
        event = normalize(
            raw,
            self.adapters.active(),
            target_instance_id=service.instance_id,
            target_version=service.version,
        )
        self.control.record_event(raw.id, event.model_dump(mode="json"))
        if not reprocess:
            for finding in detect(event):
                if finding.kind == "interpretation":
                    fingerprint = format_fingerprint(raw, event, service)
                    finding = finding.model_copy(
                        update={"dedup_key": f"adapter-discovery:{fingerprint}"}
                    )
                    case = self._finding(finding)
                    self.discoveries.observe(case, raw, service, fingerprint, supported_json(raw))
                    continue
                case = self._finding(finding)
                if self.auto_investigate and self.llm_configured and case.status == "OPEN":
                    self.control.enqueue(
                        case.id, "security" if finding.kind == "security" else "operator"
                    )
        if reprocess:
            self.collector.record_parse_status(raw.id, event.parse_status)
        else:
            self.collector.ack(raw.id, event.parse_status)

    def poll(self) -> dict[str, Any]:
        with self._lock:
            received = self.collector.poll_files()
            processed = 0
            for raw in self.collector.pending(100):
                self.process(raw)
                processed += 1
            for coverage in self.collector.coverage():
                for finding in detect_coverage(coverage):
                    self._finding(finding)
            self.schedule_adapters()
            self.last_poll = time.time()
            self.last_poll_error = None
            return {"received": len(received), "processed": processed, "at": self.last_poll}

    def schedule_adapters(self) -> list[Task]:
        return self.discoveries.schedule(
            enabled=self.auto_adapter_proposals, configured=self.llm_configured
        )

    def reprocess(self, source_id: str) -> dict[str, int]:
        count = 0
        for raw in self.collector.iter_raw(source_id):
            with self._lock:
                self.process(raw, reprocess=True)
                count += 1
        self.control.audit("normalizer", "source.reprocess", source_id, str(count))
        return {"reprocessed": count}

    def validate_adapter(self, adapter_id: str) -> dict[str, Any]:
        """Preview a fixed draft on bounded samples without changing interpretations."""
        body = self.control.get("adapter", adapter_id)
        definition = self.adapters.definition(body)
        source = self.source(definition.source_id)
        service = self.control.service(source.service_id)
        try:
            validation = self.control.get("adapter_validation", adapter_id)
        except KeyError:
            raw_samples = self.collector.search(source_id=source.id, limit=20)
        else:
            raw_samples = [
                raw
                for raw_id in validation["evidence_ids"]
                if (raw := self.collector.raw(raw_id)) is not None and raw.source_id == source.id
            ]
        samples = [
            normalize(
                raw,
                [definition],
                target_instance_id=service.instance_id,
                target_version=service.version,
            ).model_dump(mode="json")
            for raw in raw_samples
        ]
        return {
            "digest": body["digest"],
            "sample_count": len(samples),
            "supported_count": sum(
                item["parse_status"] in {"KNOWN", "PARTIAL"} for item in samples
            ),
            "samples": samples,
        }

    def check(self, service_id: str) -> VerificationResult:
        config = CheckConfig.model_validate(self.control.get("check", service_id))
        if config.kind == "demo":
            return self.verifier.verify(lambda: self.demo.check(service_id))
        return self.verifier.verify(lambda: self.http.check(config))

    def execute(self, plan_id: str, actor: Actor) -> Execution:
        if actor.role not in {"admin", "operator"}:
            raise Denied("実行権限が必要です")
        with self._execution_lock:
            existing = self.runner.for_plan(plan_id)
            if existing is not None:
                self.control.execution_finished(plan_id, existing.status)
                return existing
            plan = self.control.plan_model(self.control.get("plan", plan_id))
            capability = Capability.model_validate(
                self.control.get("capability", plan.capability_id)
            )
            before = self.check(plan.service_id)
            if before.status != "FAIL":
                raise Denied(
                    "事前確認がFAILではありません。正常または確認不能な対象には操作しません"
                )
            permit = self.control.issue_permit(plan_id)

            def authorize() -> None:
                self.control.verify_permit(permit)
                if self.check(plan.service_id).status != "FAIL":
                    raise Denied("実行直前の事前条件が成立しません")
                self.control.verify_permit(permit)

            try:
                result = self.runner.execute(
                    plan,
                    permit,
                    capability,
                    authorize,
                    lambda operation_id: (
                        self.demo.execute(plan.service_id, operation_id)
                        if capability.kind == "demo.restore"
                        else self.http.execute(capability, operation_id)
                    ),
                )
            except Exception:
                recorded = self.runner.for_plan(plan_id)
                if recorded is not None:
                    self.control.execution_finished(plan_id, recorded.status)
                raise
            self.control.execution_finished(plan_id, result.status)
            status = {"SUCCEEDED": "VERIFYING", "FAILED": "OPEN"}.get(result.status, "EXECUTING")
            self.control.case_status(plan.case_id, status)
            self.control.audit(
                actor.actor,
                "execution.request",
                result.id,
                result.status,
                service_id=result.service_id,
                object_kind="execution",
            )
            return result

    def reconcile(self, execution_id: str, actor: Actor) -> Execution:
        if actor.role not in {"admin", "operator"}:
            raise Denied("照合権限が必要です")
        with self._execution_lock:
            execution = self.runner.get(execution_id)
            plan = self.control.plan_model(self.control.get("plan", execution.plan_id))
            capability = Capability.model_validate(
                self.control.get("capability", plan.capability_id)
            )
            result = self.runner.reconcile(
                execution_id,
                self.demo.lookup if capability.kind == "demo.restore" else self.http.lookup,
            )
            self.control.execution_finished(plan.id, result.status)
            self.control.audit(
                actor.actor,
                "execution.reconcile",
                execution_id,
                result.status,
                service_id=result.service_id,
                object_kind="execution",
            )
            return result

    def verify(self, execution_id: str, actor: Actor) -> VerificationResult:
        with self._execution_lock:
            execution = self.runner.get(execution_id)
            plan = self.control.plan_model(self.control.get("plan", execution.plan_id))
            service = self.control.service(plan.service_id)
            if (service.instance_id, service.version) != (
                plan.target_instance_id,
                plan.target_version,
            ):
                raise Denied("実行時の対象と現在の対象が異なります")
            result = self.check(plan.service_id)
            with self.control.db.connection() as db:
                self.control._put(db, "verification", execution_id, result.model_dump(mode="json"))
                self.control._audit(
                    db,
                    actor.actor,
                    "execution.verify",
                    execution_id,
                    result.status,
                    service_id=execution.service_id,
                    object_kind="execution",
                )
            # Availability recovery does not establish parsing correctness or SOC eradication.
            if result.status == "PASS" and execution.status == "SUCCEEDED":
                self.control.resolve_verified_operation(plan)
            return result

    def validate_grant(self, grant: EvidenceGrant) -> None:
        task = Task.model_validate(self.control.get("task", grant.task_id))
        case = Case.model_validate(self.control.get("case", task.case_id))
        service = self.control.service(task.service_id)
        if (
            task.status != "RUNNING"
            or task.expires_at <= time.time()
            or not service.enabled
            or (task.case_id, task.service_id, task.role)
            != (grant.case_id, grant.service_id, grant.role)
            or not self.control.contains_evidence(case.id, grant.evidence_ids)
            or (task.evidence_ids and grant.evidence_ids != task.evidence_ids)
            or (
                task.target_instance_id is not None
                and (service.instance_id, service.version)
                != (task.target_instance_id, task.target_version)
            )
        ):
            raise Denied("現在のtaskまたは案件の証拠範囲が無効です")

    def run_task(self) -> Task | None:
        self.schedule_adapters()
        task = self.control.claim_task(
            self.daily_call_limit,
            allow_auto_adapters=self.auto_adapter_proposals and self.llm_configured,
        )
        if task is None:
            self.discoveries.schedule(enabled=False, configured=self.llm_configured)
            return None
        try:
            case = Case.model_validate(self.control.get("case", task.case_id))
            grant = EvidenceGrant(
                task_id=task.id,
                case_id=task.case_id,
                service_id=task.service_id,
                role=task.role,
                evidence_ids=task.evidence_ids or tuple(case.evidence_ids[:20]),
                expires_at=task.expires_at,
            )
            evidence = self.gateway.retrieve(grant)
            self.control.audit(
                f"agent:{task.role}", "evidence.retrieve", task.id, str(len(evidence))
            )
            if task.role == "adapter":
                source = self.source(case.source_id)
                service = self.control.service(task.service_id)
                if not source.enabled or source.service_id != service.id:
                    raise Denied("変換調査の観測源が無効です")
                proposal = self.adapter_agent.propose(task, evidence, source, service)
                self.validate_grant(grant)
                if (
                    self.control.service(task.service_id) != service
                    or self.source(source.id) != source
                ):
                    raise Denied("変換調査中に対象または観測源が変更されました")
                recommendations = []
                if proposal.fields:
                    definition = proposal.to_definition(f"adapter-{task.id}", source, service)
                    with self._lock:
                        self.validate_grant(grant)
                        if self.source(source.id) != source:
                            raise Denied("変換調査中に観測源が変更されました")
                        self.adapters.validate_candidate(definition)
                        samples = []
                        for raw_id in grant.evidence_ids:
                            raw = self.collector.raw(raw_id)
                            if raw is None or raw.source_id != source.id:
                                raise Denied("変換案の標本が観測源と一致しません")
                            if missing_adapter_paths(raw, definition):
                                raise Conflict("変換案の参照パスが収集した標本に存在しません")
                            samples.append(
                                normalize(
                                    raw,
                                    [definition],
                                    target_instance_id=service.instance_id,
                                    target_version=service.version,
                                )
                            )
                        if not samples or any(
                            sample.parse_status not in {"KNOWN", "PARTIAL"} for sample in samples
                        ):
                            raise Conflict("変換案が収集した同形式の標本に対応していません")
                        with self.control.db.connection() as db:
                            self.control._put(
                                db,
                                "adapter_validation",
                                definition.id,
                                {
                                    "task_id": task.id,
                                    "evidence_ids": list(grant.evidence_ids),
                                    "sample_count": len(samples),
                                    "supported_count": len(samples),
                                },
                            )
                            self.control._audit(
                                db,
                                "normalizer",
                                "adapter.validate",
                                definition.id,
                                str(len(samples)),
                            )
                        self.adapters.propose(
                            definition,
                            "agent:adapter",
                            agent_explanation=AgentExplanation(
                                task_id=task.id,
                                suggested_use=proposal.suggested_use,
                                rationale=proposal.rationale,
                                unknowns=proposal.unknowns,
                            ),
                        )
                    recommendations.append(f"変換定義 {definition.id} の内容・意味を確認して承認")
                analysis = Analysis(
                    summary="変換定義を提案しました" if proposal.fields else "変換定義の提案を保留",
                    facts=[],
                    hypotheses=proposal.rationale,
                    unknowns=proposal.unknowns,
                    recommendations=recommendations,
                )
            else:
                service_before = self.control.service(task.service_id)
                capabilities = [
                    Capability.model_validate(item)
                    for item in self.control.objects("capability")
                    if item["service_id"] == task.service_id
                ]
                if case.kind == "operation_failure":
                    analysis = self.investigator.investigate(
                        task,
                        evidence,
                        {
                            "capabilities": [
                                {
                                    "id": item.id,
                                    "version": item.version,
                                    "name": item.name,
                                    "kind": item.kind,
                                }
                                for item in capabilities
                            ],
                            "target_instance_id": service_before.instance_id,
                            "target_version": service_before.version,
                            "verification": {
                                key: value
                                for key, value in self.control.get("check", task.service_id).items()
                                if key in {"kind", "expected_status", "body_contains"}
                            },
                        },
                    )
                else:
                    analysis = self.investigator.investigate(task, evidence)
                self.validate_grant(grant)
                if case.kind == "operation_failure":
                    self._propose_recovery(
                        task,
                        analysis,
                        service_before,
                        capabilities,
                        [str(entry["id"]) for entry in evidence],
                    )
            self.control.finish_task(task, analysis)
        except Exception as exc:
            detail = (
                "OPENAI_API_KEYが未設定です"
                if not self.llm_configured
                else str(exc)
                if isinstance(exc, (Conflict, Denied))
                else f"調査失敗: {type(exc).__name__}"
            )
            self.control.finish_task(task, None, detail)
        self.discoveries.schedule(enabled=False, configured=self.llm_configured)
        return Task.model_validate(self.control.get("task", task.id))

    def _propose_recovery(
        self,
        task: Task,
        analysis: Analysis,
        service: Service,
        capabilities: list[Capability],
        evidence_ids: list[str],
    ) -> None:
        record: dict[str, Any] = {
            "id": task.case_id,
            "task_id": task.id,
            "status": "BLOCKED",
            "detail": "実行可能な復旧操作を提案できませんでした。",
            "plan_id": None,
        }
        try:
            choice = analysis.recovery
            if choice is None:
                return
            with self._lock:
                if self.control.service(task.service_id) != service:
                    raise Denied("調査中に対象または確認条件が変更されました。再調査してください。")
                capability = next(
                    (
                        item
                        for item in capabilities
                        if item.id == choice.capability_id
                        and item.version == choice.capability_version
                    ),
                    None,
                )
                if (
                    capability is None
                    or Capability.model_validate(self.control.get("capability", capability.id))
                    != capability
                ):
                    raise Denied("提案された操作が登録内容と一致しません。")
                case = self.control.get("case", task.case_id)
                if set(case["evidence_ids"]) != set(evidence_ids):
                    raise Denied("調査した根拠と現在の案件が一致しません。再調査してください。")
                if not choice.evidence_ids or not set(choice.evidence_ids).issubset(
                    task.evidence_ids or case["evidence_ids"]
                ):
                    raise Denied("復旧提案の根拠を確認できません。")
                key = hashlib.sha256(
                    json.dumps(
                        [
                            task.case_id,
                            service.model_dump(mode="json"),
                            sorted(case["evidence_ids"]),
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                existing = next(
                    (
                        item
                        for item in self.control.objects("plan")
                        if item.get("proposal_key") == key
                    ),
                    None,
                )
                plan = existing or self.control.create_plan(
                    task.case_id,
                    capability.id,
                    choice.reason,
                    f"agent:{task.id}",
                    task_id=task.id,
                    proposal_key=key,
                )
                record.update(
                    status="PROPOSED",
                    detail="復旧計画を作成しました。内容を確認して承認してください。",
                    plan_id=plan["id"],
                )
        except (Denied, Conflict, KeyError) as exc:
            record["detail"] = str(exc)
        finally:
            with self.control.db.connection() as db:
                self.control._put(db, "recovery_proposal", task.case_id, record)

    def case_detail(self, case_id: str) -> dict[str, Any]:
        case = self.control.get("case", case_id)
        evidence = []
        for raw_id in case["evidence_ids"]:
            raw = self.collector.raw(raw_id)
            if raw is not None:
                evidence.append(
                    {**raw.model_dump(mode="json"), "interpretation": self.control.event(raw_id)}
                )
        plans = [item for item in self.control.objects("plan") if item["case_id"] == case_id]
        ids = {item["id"] for item in plans}
        try:
            analysis = self.control.get("analysis", case_id)
        except KeyError:
            analysis = None
        try:
            adapter_analysis = self.control.get("adapter_analysis", case_id)
        except KeyError:
            adapter_analysis = None
        return {
            "case": case,
            "recoveries": [
                item for item in self.control.objects("recovery") if item["case_id"] == case_id
            ],
            "recovery_proposal": next(
                (
                    item
                    for item in self.control.objects("recovery_proposal")
                    if item["id"] == case_id
                ),
                None,
            ),
            "evidence": evidence,
            "plans": plans,
            "executions": [item for item in self.execution_list() if item["plan_id"] in ids],
            "analysis": analysis,
            "adapter_analysis": adapter_analysis,
            "adapter_discovery": self.discoveries.for_case(case_id),
        }

    def execution_list(self) -> list[dict[str, Any]]:
        items = []
        for execution in self.runner.list():
            body = execution.model_dump(mode="json")
            with suppress(KeyError):
                body["verification"] = self.control.get("verification", execution.id)
            items.append(body)
        return items

    def overview(self) -> dict[str, Any]:
        return {
            "services": self.control.objects("service"),
            "sources": [source.model_dump(mode="json") for source in self.collector.sources()],
            "cases": self.control.objects("case")[:500],
            "plans": self.control.objects("plan")[:500],
            "executions": self.execution_list()[-500:],
            "adapters": self.control.objects("adapter"),
            "adapter_discoveries": self.discoveries.all(),
            "coverage": [item.model_dump(mode="json") for item in self.collector.coverage()],
            "audit": self.control.audit_log(),
            "tasks": self.control.objects("task")[:200],
            "llm": {
                "model": "gpt-5.6-luna",
                "configured": self.llm_configured,
                "daily_call_limit": self.daily_call_limit,
                "auto_adapter_proposals": self.auto_adapter_proposals,
                "adapter_coalesce_seconds": self.adapter_coalesce_seconds,
                "adapter_retry_seconds": self.adapter_retry_seconds,
                "adapter_max_attempts": self.adapter_max_attempts,
            },
            "worker": {
                "last_poll": self.last_poll,
                "error": self.last_poll_error,
                "investigation_error": self.last_task_error,
            },
        }

    def seed_demo(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            service = Service(
                id="demo-checkout",
                name="Checkout / サンプル",
                instance_id="demo-checkout-v1",
                owner=actor.actor,
                criticality="high",
            )
            self.control.register_service(service, actor.actor)
            self.control.set_check(service.id, CheckConfig(kind="demo"), actor.actor)
            self.control.register_capability(
                Capability(
                    id="demo-restore",
                    service_id=service.id,
                    name="サンプルサービスの復旧",
                    kind="demo.restore",
                ),
                actor.actor,
            )
            source = Source(
                id="demo-log",
                service_id=service.id,
                name="サンプルログ",
                kind="push",
                stale_after_seconds=86400,
            )
            self.collector.register_source(source)
            self.control.bind_source_scope(source.id, source.service_id)
            try:
                self.control.get("adapter", "demo-json-v1")
            except KeyError:
                self.adapters.propose(
                    AdapterDefinition(
                        id="demo-json-v1",
                        name="サンプルJSON",
                        source_id=source.id,
                        target_instance_id=service.instance_id,
                        target_version=1,
                        version=1,
                        fields={
                            "category": "category",
                            "message": "message",
                            "outcome": "result",
                            "severity": "level",
                        },
                        conditions={"format": "opsyne-demo-v1"},
                        outcome_map={"error": "FAILURE", "ok": "SUCCESS"},
                        severity_map={"error": "ERROR", "info": "INFO"},
                    ),
                    "system:demo",
                )
            # Idempotent setup does not reset an already restored service.
            marker = self.data_dir / "demo-seeded"
            if not marker.exists():
                self.demo.configure(service.id, healthy=False)
                marker.write_text("1", encoding="utf-8")
            raw = self.collector.ingest(
                source.id,
                [
                    RawInput(
                        external_id="sample-failure-1",
                        payload=json.dumps(
                            {
                                "format": "opsyne-demo-v1",
                                "category": "availability",
                                "result": "error",
                                "level": "error",
                                "message": "注文処理が失敗しました。合成データです。",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ],
            )[0]
            # The synthetic failure comes from an independent local business observation.
            self.control.add_finding(
                "demo-business-failure",
                service.id,
                source.id,
                "operation_failure",
                "サンプル: 注文処理を復旧してください",
                "ERROR",
                [raw.id],
            )
            self.poll()
            self.control.audit(actor.actor, "demo.create", service.id, "合成データのみ")
            return {
                "service_id": service.id,
                "message": "合成データを用意しました。変換定義は別途承認してください。",
            }
