"""Offline lifecycle, recovery quarantine, and cross-domain acceptance checks."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue

from opsyne.agents.adapter import AdapterAgent
from opsyne.agents.investigator import Investigator
from opsyne.api.app import create_app
from opsyne.cli import backup, load_env, main, restore
from opsyne.connectors.http import HttpConnector
from opsyne.contracts.adapter_proposals import AdapterProposal, FieldMapping
from opsyne.contracts.adapter_reviews import HumanExplanation
from opsyne.contracts.cases import Analysis, Case, EvidenceClaim, Task
from opsyne.contracts.core import Actor, Service
from opsyne.contracts.execution import Capability, CheckConfig
from opsyne.contracts.observations import AdapterDefinition, RawInput, Source
from opsyne.control.repository import Control, Denied
from opsyne.runtime import Runtime
from opsyne.storage.instance import InstanceLock

OWNER = Actor(actor="owner", role="admin")
REVIEWER = Actor(actor="reviewer", role="approver")
OPERATOR = Actor(actor="operator", role="operator")
DATABASES = ("control.sqlite3", "collector.sqlite3", "runner.sqlite3", "demo.sqlite3")


@pytest.fixture(autouse=True)
def no_live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPSYNE_AUTO_INVESTIGATE", raising=False)


def initialized(path: Path) -> Runtime:
    app = create_app(path, background=False)
    return cast(Runtime, app.state.runtime)


def actor_headers(path: Path, actor: str) -> dict[str, str]:
    entries = json.loads((path / "tokens.json").read_text(encoding="utf-8"))
    token = next(str(item["token"]) for item in entries if item["actor"] == actor)
    return {"Authorization": f"Bearer {token}"}


def plan_for_demo(runtime: Runtime) -> dict[str, Any]:
    runtime.seed_demo(OWNER)
    case = next(
        item for item in runtime.control.objects("case") if item["kind"] == "operation_failure"
    )
    return runtime.control.create_plan(
        case["id"], "demo-restore", "Restore the observed local target", OPERATOR.actor
    )


def test_backup_restore_preserves_data_and_invalidates_past_approval(tmp_path: Path) -> None:
    source = initialized(tmp_path / "source")
    plan = plan_for_demo(source)
    source.control.approve(plan["id"], plan["digest"], REVIEWER)
    original_raw = next(iter(source.collector.iter_raw("demo-log")))
    archive = tmp_path / "backup"
    backup(source.data_dir, archive)
    assert json.loads((archive / "backup.json").read_text()) == {"format": 1, "complete": True}
    for name in DATABASES:
        with sqlite3.connect(archive / name) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    destination = tmp_path / "restored"
    restore(archive, destination)
    restored = initialized(destination)
    assert restored.collector.raw(original_raw.id) == original_raw
    assert restored.control.get("plan", plan["id"])["status"] == "APPROVED"
    assert restored.demo.check("demo-checkout").status == "FAIL"
    with pytest.raises(Denied, match="復元"):
        restored.control.issue_permit(plan["id"])
    with pytest.raises(Denied):
        restored.control.acknowledge_restore(
            OPERATOR, "Reviewed the complete target operation history"
        )
    with pytest.raises(Denied):
        restored.control.acknowledge_restore(OWNER, "short")
    restored.control.acknowledge_restore(OWNER, "Reviewed the complete target operation history")
    # Acknowledging recovery does not revive approvals from the earlier generation.
    with pytest.raises(Denied, match="世代"):
        restored.control.issue_permit(plan["id"])
    fresh = restored.control.create_plan(
        plan["case_id"], "demo-restore", "Fresh plan after verified recovery", OPERATOR.actor
    )
    restored.control.approve(fresh["id"], fresh["digest"], REVIEWER)
    execution = restored.execute(fresh["id"], OPERATOR)
    assert execution.status == "SUCCEEDED"
    assert restored.verify(execution.id, OPERATOR).status == "PASS"
    assert restored.control.get("case", plan["case_id"])["status"] == "RESOLVED"
    assert source.demo.check("demo-checkout").status == "FAIL"
    assert source.control.get("plan", plan["id"])["status"] == "APPROVED"
    assert any(
        row["action"] == "recovery.restore_acknowledged" for row in restored.control.audit_log()
    )


def test_backup_handles_uri_reserved_characters_in_path(tmp_path: Path) -> None:
    source = initialized(tmp_path / "state # with spaces")
    plan = plan_for_demo(source)
    archive = tmp_path / "backup # with spaces"
    backup(source.data_dir, archive)
    copied = initialized(archive)
    assert copied.control.get("plan", plan["id"])["digest"] == plan["digest"]
    assert copied.demo.check("demo-checkout").status == "FAIL"


@pytest.mark.parametrize("missing", ["control.sqlite3", "runner.sqlite3", "signing.key"])
def test_incomplete_backup_is_rejected_before_creating_destination(
    tmp_path: Path, missing: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in DATABASES:
        if name != missing:
            with sqlite3.connect(source / name) as connection:
                connection.execute("CREATE TABLE marker (id INTEGER)")
    if missing != "signing.key":
        (source / "signing.key").write_bytes(b"k" * 32)
    destination = tmp_path / "not-created"
    with pytest.raises(ValueError):
        backup(source, destination)
    assert not destination.exists()


def test_backup_does_not_create_a_missing_source_or_overwrite_destination(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    destination = tmp_path / "backup"
    with pytest.raises(ValueError):
        backup(missing, destination)
    assert not missing.exists() and not destination.exists()
    source = initialized(tmp_path / "source")
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep this existing content", encoding="utf-8")
    with pytest.raises(ValueError):
        backup(source.data_dir, destination)
    assert sentinel.read_text() == "keep this existing content"


@pytest.mark.parametrize("key_size", [0, 31, 33])
def test_backup_rejects_malformed_signing_key_before_creating_destination(
    tmp_path: Path, key_size: int
) -> None:
    source = initialized(tmp_path / "source")
    (source.data_dir / "signing.key").write_bytes(b"k" * key_size)
    destination = tmp_path / "not-created"
    with pytest.raises(ValueError, match="署名鍵"):
        backup(source.data_dir, destination)
    assert not destination.exists()


def test_restore_rejects_incomplete_manifest_before_copying(tmp_path: Path) -> None:
    source = initialized(tmp_path / "source")
    (source.data_dir / "backup.json").write_text('{"format":1,"complete":false}', encoding="utf-8")
    destination = tmp_path / "restored"
    with pytest.raises(ValueError):
        restore(source.data_dir, destination)
    assert not destination.exists()


@pytest.mark.parametrize("layout", ["one-database", "databases-without-key"])
def test_runtime_rejects_partial_state_without_recreating_missing_files(
    tmp_path: Path, layout: str
) -> None:
    directory = tmp_path / "partial"
    directory.mkdir()
    names = DATABASES[:1] if layout == "one-database" else DATABASES
    for name in names:
        (directory / name).touch()
    before = {path.name for path in directory.iterdir()}
    with pytest.raises(ValueError, match="不完全"):
        Runtime(directory)
    assert {path.name for path in directory.iterdir()} == before


def test_second_process_cannot_take_live_instance_lock(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            first.acquire()
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from opsyne.storage.instance import InstanceLock; "
                "lock = InstanceLock(Path(sys.argv[1]));\n"
                "try:\n lock.acquire()\n"
                "except RuntimeError:\n print('blocked')\n"
                "else:\n lock.release(); raise SystemExit(2)\n",
                str(path),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "blocked"
    finally:
        first.release()
    next_owner = InstanceLock(path)
    next_owner.acquire()
    next_owner.release()
    next_owner.release()


def test_live_runtime_blocks_backup_and_second_startup_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "state"
    first = initialized(directory)
    first.start()
    try:
        second = initialized(directory)
        recoveries: list[str] = []

        def unexpected_recovery() -> int:
            recoveries.append("called")
            return 0

        monkeypatch.setattr(second.runner, "recover_in_flight", unexpected_recovery)
        with pytest.raises(RuntimeError):
            second.start()
        assert recoveries == []
        with pytest.raises(RuntimeError):
            backup(directory, tmp_path / "live-backup")
        assert not (tmp_path / "live-backup").exists()
    finally:
        first.stop()
    backup(directory, tmp_path / "stopped-backup")
    assert (tmp_path / "stopped-backup" / "backup.json").is_file()


def test_new_evidence_after_plan_prevents_old_execution_resolving_case(tmp_path: Path) -> None:
    runtime = initialized(tmp_path / "state")
    plan = plan_for_demo(runtime)
    runtime.control.approve(plan["id"], plan["digest"], REVIEWER)
    execution = runtime.execute(plan["id"], OPERATOR)
    assert execution.status == "SUCCEEDED"
    raw = runtime.collector.ingest(
        "demo-log", [RawInput(external_id="new-failure", payload='{"new_failure":true}')]
    )[0]
    runtime.control.add_finding(
        "demo-business-failure",
        "demo-checkout",
        "demo-log",
        "operation_failure",
        "New evidence requires its own evaluation",
        "ERROR",
        [raw.id],
    )
    assert runtime.verify(execution.id, OPERATOR).status == "PASS"
    case = Case.model_validate(runtime.control.get("case", plan["case_id"]))
    assert raw.id in case.evidence_ids and raw.id not in plan["evidence_ids"]
    assert case.status != "RESOLVED"


def test_adapter_agent_task_creates_only_a_scoped_unapproved_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = initialized(tmp_path / "state")
    runtime.seed_demo(OWNER)
    case = next(
        item for item in runtime.control.objects("case") if item["kind"] == "interpretation"
    )
    calls: list[str] = []

    def proposal(
        self: AdapterAgent,
        task: Task,
        evidence: list[dict[str, JsonValue]],
        source: Source,
        service: Service,
    ) -> AdapterProposal:
        assert task.role == "adapter" and task.status == "RUNNING"
        assert source.id == "demo-log" and service.instance_id == "demo-checkout-v1"
        assert evidence and evidence[0]["id"] in case["evidence_ids"]
        calls.append(task.id)
        return AdapterProposal(
            name="Limited message interpretation",
            suggested_use="メッセージ内容の調査",
            fields=[FieldMapping(field="message", path="message")],
            conditions=[],
            outcome_map=[],
            severity_map=[],
            rationale=[
                EvidenceClaim(text="Message text is visible", evidence_ids=case["evidence_ids"])
            ],
            unknowns=["Outcome and severity semantics are not established"],
        )

    monkeypatch.setattr(AdapterAgent, "propose", proposal)
    task = runtime.control.enqueue(case["id"], "adapter")
    finished = runtime.run_task()
    assert finished is not None and finished.id == task.id and finished.status == "SUCCEEDED"
    assert calls == [task.id]
    drafts = [item for item in runtime.control.objects("adapter") if item["id"] != "demo-json-v1"]
    assert len(drafts) == 1
    assert drafts[0]["status"] == "DRAFT"
    assert drafts[0]["source_id"] == "demo-log"
    assert drafts[0]["target_instance_id"] == "demo-checkout-v1"
    assert drafts[0]["target_version"] == 1
    explanation = drafts[0]["explanation"]
    assert explanation["human"] is None and explanation["recorded_by"] is None
    assert explanation["agent"]["task_id"] == task.id
    assert explanation["agent"]["suggested_use"] == "メッセージ内容の調査"
    assert explanation["agent"]["rationale"][0]["evidence_ids"] == case["evidence_ids"]
    assert explanation["agent"]["unknowns"] == [
        "Outcome and severity semantics are not established"
    ]
    runtime.adapters.update_explanation(
        drafts[0]["id"], drafts[0]["digest"], HumanExplanation(monitoring_purpose="ログ調査"), OWNER
    )
    saved = runtime.control.get("adapter", drafts[0]["id"])
    assert saved["explanation"]["agent"] == explanation["agent"]
    assert runtime.adapters.active() == []
    assert runtime.runner.list() == []
    assert runtime.demo.check("demo-checkout").status == "FAIL"
    assert runtime.control.objects("plan") == []


def test_adapter_task_revalidates_target_after_model_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = initialized(tmp_path / "state")
    runtime.seed_demo(OWNER)
    case = next(
        item for item in runtime.control.objects("case") if item["kind"] == "interpretation"
    )

    def proposal(
        self: AdapterAgent,
        task: Task,
        evidence: list[dict[str, JsonValue]],
        source: Source,
        service: Service,
    ) -> AdapterProposal:
        runtime.control.register_service(
            service.model_copy(update={"version": 2, "instance_id": "new-instance"}), OWNER.actor
        )
        return AdapterProposal(
            name="Old target interpretation",
            fields=[FieldMapping(field="message", path="message")],
            conditions=[],
            outcome_map=[],
            severity_map=[],
            rationale=[EvidenceClaim(text="Message seen", evidence_ids=case["evidence_ids"])],
            unknowns=["Semantics unknown"],
        )

    monkeypatch.setattr(AdapterAgent, "propose", proposal)
    runtime.control.enqueue(case["id"], "adapter")
    finished = runtime.run_task()
    assert finished is not None and finished.status == "FAILED"
    assert len(runtime.control.objects("adapter")) == 1
    assert runtime.adapters.active() == []
    assert runtime.runner.list() == []


def test_failed_restore_does_not_publish_usable_old_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = initialized(tmp_path / "source")
    plan = plan_for_demo(source)
    source.control.approve(plan["id"], plan["digest"], REVIEWER)
    archive = tmp_path / "backup"
    backup(source.data_dir, archive)

    def failed_generation(self: Control, actor: str) -> int:
        raise RuntimeError("Simulated failure while invalidating old authorization")

    monkeypatch.setattr(Control, "advance_generation", failed_generation)
    destination = tmp_path / "restored"
    with pytest.raises(RuntimeError, match="Simulated failure"):
        restore(archive, destination)
    assert (destination / "restore.pending").is_file()
    with pytest.raises(ValueError, match="復元"):
        initialized(destination)
    with pytest.raises(ValueError, match="復元"):
        backup(destination, tmp_path / "incomplete-copy")
    assert not (tmp_path / "incomplete-copy").exists()


def test_explicit_env_file_quotes_literals_and_existing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "test.env"
    keys = (
        "OPSYNE_TEST_PLAIN",
        "OPSYNE_TEST_QUOTED",
        "OPSYNE_TEST_LITERAL",
        "OPSYNE_TEST_EXISTING",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPSYNE_TEST_EXISTING", "process-setting")
    path.write_text(
        "# A comment\nOPSYNE_TEST_PLAIN = simple\n"
        'OPSYNE_TEST_QUOTED="value with spaces # preserved"\n'
        "OPSYNE_TEST_LITERAL='${OPSYNE_TEST_PLAIN} $(not-executed)'\n"
        "OPSYNE_TEST_EXISTING=file-setting\n",
        encoding="utf-8-sig",
    )
    load_env(path)
    assert os.environ["OPSYNE_TEST_PLAIN"] == "simple"
    assert os.environ["OPSYNE_TEST_QUOTED"] == "value with spaces # preserved"
    assert os.environ["OPSYNE_TEST_LITERAL"] == "${OPSYNE_TEST_PLAIN} $(not-executed)"
    assert os.environ["OPSYNE_TEST_EXISTING"] == "process-setting"


@pytest.mark.parametrize("invalid", ["not a setting", "9BAD=value", 'BAD="unclosed'])
def test_malformed_env_file_has_no_partial_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    monkeypatch.delenv("OPSYNE_TEST_ATOMIC", raising=False)
    path = tmp_path / "invalid.env"
    path.write_text(f"OPSYNE_TEST_ATOMIC=should-not-appear\n{invalid}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_env(path)
    assert "OPSYNE_TEST_ATOMIC" not in os.environ


def test_serve_env_file_is_loaded_before_app_creation_without_starting_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPSYNE_TEST_CLI", raising=False)
    settings = tmp_path / "cli.env"
    settings.write_text("OPSYNE_TEST_CLI=loaded\n", encoding="utf-8")
    data_dir = tmp_path / "state"
    captured: list[str] = []
    application = FastAPI()

    def build_app(path: Path) -> FastAPI:
        assert path == data_dir
        assert os.environ["OPSYNE_TEST_CLI"] == "loaded"
        captured.append("app")
        return application

    def run(app: FastAPI, *, host: str, port: int, log_level: str) -> None:
        assert app is application and host == "127.0.0.1" and port == 8899
        captured.append("server")

    monkeypatch.setattr("opsyne.cli.create_app", build_app)
    monkeypatch.setattr("opsyne.cli.uvicorn.run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "opsyne",
            "serve",
            "--data-dir",
            str(data_dir),
            "--env-file",
            str(settings),
            "--port",
            "8899",
        ],
    )
    assert main() == 0
    assert captured == ["app", "server"]


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 407, 408, 429])
def test_unauthorized_or_redirected_observation_is_unknown(status: int) -> None:
    connector = HttpConnector(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="not target state"))
    )
    result = connector.check(
        CheckConfig(kind="http", endpoint="https://registered.internal/health")
    )
    assert result.status == "UNKNOWN"
    explicit = connector.check(
        CheckConfig(
            kind="http", endpoint="https://registered.internal/health", expected_status=status
        )
    )
    assert explicit.status == "PASS"


def test_unauthorized_observation_cannot_trigger_runtime_operation(tmp_path: Path) -> None:
    runtime = initialized(tmp_path / "state")
    old = plan_for_demo(runtime)
    runtime.control.set_check(
        "demo-checkout",
        CheckConfig(kind="http", endpoint="https://registered.internal/health"),
        OWNER.actor,
    )
    runtime.control.register_capability(
        Capability(
            id="http-restore",
            service_id="demo-checkout",
            name="Registered operation",
            kind="http.request",
            endpoint="https://registered.internal/restore",
        ),
        OWNER.actor,
    )
    requests: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(401)

    runtime.http = HttpConnector(transport=httpx.MockTransport(transport))
    plan = runtime.control.create_plan(old["case_id"], "http-restore", "A reason", OPERATOR.actor)
    runtime.control.approve(plan["id"], plan["digest"], REVIEWER)
    with pytest.raises(Denied):
        runtime.execute(plan["id"], OPERATOR)
    assert requests == ["GET"]
    assert runtime.runner.list() == []
    assert runtime.control.recovery_holds() == []


def test_adapter_validation_previews_bounded_bound_samples_without_mutation(tmp_path: Path) -> None:
    app = create_app(tmp_path, background=False)
    runtime = cast(Runtime, app.state.runtime)
    runtime.seed_demo(OWNER)
    runtime.collector.ingest(
        "demo-log",
        [
            RawInput(
                external_id=f"preview-{index}",
                payload=json.dumps(
                    {"format": "opsyne-demo-v1", "message": "Sample", "result": "unmapped"}
                ),
            )
            for index in range(25)
        ],
    )
    before_pending = runtime.collector.pending()
    before_cases = runtime.control.objects("case")
    before_audit = runtime.control.audit_log()
    draft = runtime.control.get("adapter", "demo-json-v1")
    with TestClient(app) as client:
        assert client.post("/api/adapters/demo-json-v1/validate").status_code == 401
        response = client.post(
            "/api/adapters/demo-json-v1/validate", headers=actor_headers(tmp_path, "viewer")
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["digest"] == draft["digest"]
        assert preview["sample_count"] == 20 and preview["supported_count"] == 20
        expected = {raw.id for raw in runtime.collector.search(source_id="demo-log", limit=20)}
        assert {sample["raw_ref"] for sample in preview["samples"]} == expected
        for sample in preview["samples"]:
            assert sample["service_id"] == "demo-checkout" and sample["source_id"] == "demo-log"
            assert sample["adapter_id"] == "demo-json-v1" and sample["adapter_version"] == 1
            assert sample["parse_status"] == "PARTIAL" and sample["outcome"] == "UNKNOWN"
            assert "outcome" in sample["unknown_fields"]
        assert runtime.collector.pending() == before_pending
        assert runtime.control.objects("case") == before_cases
        assert runtime.control.audit_log() == before_audit
        assert runtime.control.get("adapter", "demo-json-v1") == draft
        assert runtime.adapters.active() == []
        assert runtime.runner.list() == []
        assert runtime.demo.check("demo-checkout").status == "FAIL"
        approval = client.post(
            "/api/adapters/demo-json-v1/approve",
            headers=actor_headers(tmp_path, "reviewer"),
            json={"digest": draft["digest"]},
        )
        assert approval.status_code == 200, approval.text
        assert len(runtime.adapters.active()) == 1
        assert runtime.collector.pending() == before_pending
        assert runtime.runner.list() == []


@pytest.mark.parametrize("unsupported", ["missing_paths", "no_samples", "changed_target"])
def test_adapter_without_usable_bound_samples_cannot_be_approved(
    tmp_path: Path, unsupported: str
) -> None:
    app = create_app(tmp_path, background=False)
    runtime = cast(Runtime, app.state.runtime)
    runtime.seed_demo(OWNER)
    definition = runtime.adapters.definition(runtime.control.get("adapter", "demo-json-v1"))
    candidate = definition.model_copy(update={"id": "candidate"})
    if unsupported == "missing_paths":
        candidate = AdapterDefinition.model_validate(
            {**candidate.model_dump(), "fields": {"message": "absent.path"}}
        )
    elif unsupported == "no_samples":
        runtime.collector.register_source(
            Source(id="empty-source", service_id="demo-checkout", name="Empty", kind="push")
        )
        candidate = candidate.model_copy(update={"source_id": "empty-source"})
    draft = runtime.adapters.propose(candidate, OPERATOR.actor)
    if unsupported == "changed_target":
        service = runtime.control.service("demo-checkout")
        runtime.control.register_service(
            service.model_copy(update={"version": 2, "instance_id": "new-instance"}), OWNER.actor
        )
    with TestClient(app) as client:
        preview = client.post(
            "/api/adapters/candidate/validate", headers=actor_headers(tmp_path, "reviewer")
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["supported_count"] == 0
        approval = client.post(
            "/api/adapters/candidate/approve",
            headers=actor_headers(tmp_path, "reviewer"),
            json={"digest": draft["digest"]},
        )
        assert approval.status_code == 409, approval.text
    assert runtime.control.get("adapter", "candidate")["status"] == "DRAFT"
    assert runtime.adapters.active() == []
    assert runtime.runner.list() == []


@pytest.mark.parametrize("worker", ["observation", "investigation"])
def test_worker_continues_when_processing_and_failure_audit_both_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker: str
) -> None:
    runtime = initialized(tmp_path / "state")
    method = "poll" if worker == "observation" else "run_task"
    error_attribute = "last_poll_error" if worker == "observation" else "last_task_error"
    original = getattr(runtime, method)
    iterations: list[int] = []
    recorded_errors: list[str | None] = []
    audit_attempts: list[str] = []

    def work() -> Any:
        iterations.append(len(iterations) + 1)
        if len(iterations) == 1:
            raise OSError("Synthetic unavailable storage")
        recorded_errors.append(getattr(runtime, error_attribute))
        runtime._stop.set()
        return original()

    def failed_audit(actor: str, action: str, target: str, detail: str = "") -> None:
        audit_attempts.append(action)
        raise sqlite3.OperationalError("Synthetic audit storage failure")

    def no_delay(timeout: float | None = None) -> bool:
        return runtime._stop.is_set()

    monkeypatch.setattr(runtime, method, work)
    monkeypatch.setattr(runtime.control, "audit", failed_audit)
    monkeypatch.setattr(runtime._stop, "wait", no_delay)
    loop = runtime._observation_loop if worker == "observation" else runtime._investigation_loop
    loop()
    assert iterations == [1, 2]
    assert recorded_errors == ["OSError"]
    assert audit_attempts == ["poll.failed" if worker == "observation" else "worker.failed"]
    assert getattr(runtime, error_attribute) is None
    assert runtime.runner.list() == []


def test_expired_task_with_unsaved_result_is_failed_without_resending_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = initialized(tmp_path / "state")
    plan = plan_for_demo(runtime)
    task = runtime.control.enqueue(plan["case_id"])
    calls: list[str] = []

    def investigate(
        self: Investigator, claimed: Task, evidence: list[dict[str, JsonValue]]
    ) -> Analysis:
        calls.append(claimed.id)
        return Analysis(
            summary="Synthetic response",
            facts=[],
            hypotheses=[],
            unknowns=["No operational conclusion"],
            recommendations=[],
        )

    def failed_finish(claimed: Task, analysis: Analysis | None, error: str = "") -> None:
        raise sqlite3.OperationalError("Synthetic result persistence failure")

    monkeypatch.setattr(Investigator, "investigate", investigate)
    with monkeypatch.context() as persistence:
        persistence.setattr(runtime.control, "finish_task", failed_finish)
        with pytest.raises(sqlite3.OperationalError):
            runtime.run_task()
    assert runtime.control.get("task", task.id)["status"] == "RUNNING"
    assert runtime.run_task() is None
    assert calls == [task.id]
    with monkeypatch.context() as clock:
        clock.setattr("opsyne.control.repository.time.time", lambda: task.expires_at + 1)
        assert runtime.run_task() is None
        failed = runtime.control.get("task", task.id)
        assert failed["status"] == "FAILED" and failed["attempts"] == 1
        assert "自動再送" in failed["detail"]
        restarted = initialized(runtime.data_dir)
        assert restarted.run_task() is None
        assert restarted.control.get("task", task.id) == failed
    assert calls == [task.id]
    assert runtime.runner.list() == []
    with runtime.control.db.connection() as connection:
        assert connection.execute("SELECT SUM(calls) FROM budget").fetchone()[0] == 1
