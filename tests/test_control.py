"""Acceptance checks for durable control state and current-state authorization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opsyne.contracts.cases import Analysis, Case, Task
from opsyne.contracts.core import Actor, Service, canonical
from opsyne.contracts.execution import Capability, CheckConfig, plan_digest
from opsyne.control.repository import Conflict, Control, Denied

KEY = b"a-test-only-signing-key-of-at-least-32-bytes"
APPROVER = Actor(actor="approver", role="approver")


def configured(tmp_path: Path) -> Control:
    control = Control(tmp_path / "control.sqlite3", KEY)
    control.register_service(
        Service(id="service", name="Demo service", instance_id="instance", owner="owner"), "owner"
    )
    control.register_capability(
        Capability(id="restore", service_id="service", name="Restore", kind="demo.restore"),
        "owner",
    )
    control.set_check("service", CheckConfig(kind="demo"), "owner")
    return control


def finding(control: Control, key: str = "group", evidence: str = "raw-1") -> Case:
    return control.add_finding(
        key, "service", "source", "error", "Errors detected", "high", [evidence], now=90
    )


def proposed(control: Control, key: str = "group") -> dict[str, Any]:
    case = finding(control, key)
    return control.create_plan(case.id, "restore", "Restore observed failure", "proposer", now=100)


def approved(control: Control, key: str = "group") -> dict[str, Any]:
    body = proposed(control, key)
    return control.approve(body["id"], body["digest"], APPROVER, now=110)


def analysis() -> Analysis:
    return Analysis(
        summary="Investigation completed",
        facts=[],
        hypotheses=[],
        unknowns=["Root cause unknown"],
        recommendations=["Review the registered target observation"],
    )


def test_approval_separates_humans_and_binds_digest(tmp_path: Path) -> None:
    control = configured(tmp_path)
    body = proposed(control)
    plan = control.plan_model(body)
    assert body["status"] == "DRAFT"
    assert plan.digest == plan_digest(plan)
    assert "Restore" in plan.impact
    with pytest.raises(Denied, match="本人"):
        control.approve(body["id"], body["digest"], Actor(actor="proposer", role="admin"), now=110)
    with pytest.raises(Conflict, match="digest"):
        control.approve(body["id"], "incorrect", APPROVER, now=110)
    assert control.get("plan", body["id"])["status"] == "DRAFT"
    accepted = control.approve(body["id"], body["digest"], APPROVER, now=110)
    assert accepted["approver"] == APPROVER.actor
    assert control.plan_model(accepted) == plan
    with pytest.raises(Conflict):
        control.approve(body["id"], body["digest"], APPROVER, now=111)


@pytest.mark.parametrize("role", ["operator", "viewer", "agent"])
def test_other_roles_cannot_approve(tmp_path: Path, role: str) -> None:
    control = configured(tmp_path)
    body = proposed(control)
    actor = Actor.model_validate({"actor": "other", "role": role})
    with pytest.raises(Denied, match="承認権限"):
        control.approve(body["id"], body["digest"], actor, now=110)
    with pytest.raises(Denied):
        control.issue_permit(body["id"], now=115)


def test_draft_and_approval_expire_at_plan_deadline(tmp_path: Path) -> None:
    control = configured(tmp_path)
    draft = proposed(control, "first")
    with pytest.raises(Denied):
        control.approve(draft["id"], draft["digest"], APPROVER, now=draft["expires_at"])
    body = approved(control, "second")
    with pytest.raises(Denied):
        control.issue_permit(body["id"], now=body["expires_at"])
    with pytest.raises(Denied):
        control.issue_permit(body["id"], now=99)


def test_stored_plan_tampering_is_rejected(tmp_path: Path) -> None:
    control = configured(tmp_path)
    body = approved(control)
    tampered = {**body, "reason": "Modified content after approval"}
    with control.db.connection() as connection:
        connection.execute(
            "UPDATE objects SET body=? WHERE kind='plan' AND id=?",
            (canonical(tampered), body["id"]),
        )
    with pytest.raises(Denied):
        control.issue_permit(body["id"], now=115)


@pytest.mark.parametrize("change", ["instance", "capability", "check", "disabled"])
def test_approval_is_invalidated_by_target_or_capability_change(
    tmp_path: Path, change: str
) -> None:
    control = configured(tmp_path)
    body = approved(control)
    if change == "instance":
        control.register_service(
            control.service("service").model_copy(
                update={"instance_id": "replacement", "version": 2}
            ),
            "owner",
        )
    elif change == "capability":
        old = Capability.model_validate(control.get("capability", "restore"))
        control.register_capability(
            old.model_copy(update={"name": "Changed operation", "version": 2}), "owner"
        )
    elif change == "check":
        control.set_check(
            "service", CheckConfig(kind="http", endpoint="http://127.0.0.1/health"), "owner"
        )
        assert control.service("service").version == 2
    else:
        control.register_service(
            control.service("service").model_copy(update={"enabled": False, "version": 2}), "owner"
        )
    with pytest.raises(Denied):
        control.issue_permit(body["id"], now=115)


def test_changed_configuration_requires_new_version(tmp_path: Path) -> None:
    control = configured(tmp_path)
    with pytest.raises(Conflict):
        control.register_service(
            control.service("service").model_copy(update={"instance_id": "replacement"}), "owner"
        )
    capability = Capability.model_validate(control.get("capability", "restore"))
    with pytest.raises(Conflict):
        control.register_capability(capability.model_copy(update={"name": "changed"}), "owner")


def test_capability_identity_cannot_move_away_from_active_target(tmp_path: Path) -> None:
    control = configured(tmp_path)
    body = approved(control)
    control.issue_permit(body["id"], now=115)
    control.register_service(
        Service(id="other", name="Other service", instance_id="other-instance", owner="owner"),
        "owner",
    )
    capability = Capability.model_validate(control.get("capability", "restore"))
    with pytest.raises(Conflict, match="移動"):
        control.register_capability(
            capability.model_copy(update={"service_id": "other", "version": 2}), "owner"
        )
    assert control.get("capability", "restore")["service_id"] == "service"


def test_unresolved_operation_locks_configuration_and_other_plans(tmp_path: Path) -> None:
    control = configured(tmp_path)
    first = approved(control, "first")
    second = approved(control, "second")
    permit = control.issue_permit(first["id"], now=115)
    control.verify_permit(permit, now=116)
    with pytest.raises(Conflict):
        control.issue_permit(second["id"], now=116)
    with pytest.raises(Conflict):
        control.register_service(control.service("service"), "owner")
    with pytest.raises(Conflict):
        control.set_check("service", CheckConfig(kind="demo"), "owner")
    with pytest.raises(Conflict):
        control.register_capability(
            Capability.model_validate(control.get("capability", "restore")), "owner"
        )
    with pytest.raises(Conflict):
        control.reject(first["id"], APPROVER, "withdraw")
    control.execution_finished(first["id"], "UNKNOWN")
    with pytest.raises(Conflict):
        control.issue_permit(second["id"], now=117)
    control.execution_finished(first["id"], "SUCCEEDED")
    assert control.get("plan", first["id"])["status"] == "EXECUTED"
    with pytest.raises(Denied):
        control.verify_permit(permit, now=118)
    assert control.issue_permit(second["id"], now=119).plan_id == second["id"]
    # Successful API execution alone must not resolve the case.
    assert control.get("case", first["case_id"])["status"] != "RESOLVED"


def test_resolved_case_cannot_use_stale_draft_or_approval(tmp_path: Path) -> None:
    control = configured(tmp_path)
    draft = proposed(control, "first")
    body = approved(control, "second")
    control.case_status(draft["case_id"], "RESOLVED")
    control.case_status(body["case_id"], "RESOLVED")
    with pytest.raises(Denied, match="案件"):
        control.approve(draft["id"], draft["digest"], APPROVER, now=115)
    with pytest.raises(Denied, match="案件"):
        control.issue_permit(body["id"], now=115)


def test_generation_change_invalidates_approvals_and_existing_permits(tmp_path: Path) -> None:
    control = configured(tmp_path)
    first = approved(control, "first")
    second = approved(control, "second")
    permit = control.issue_permit(first["id"], now=115)
    assert control.advance_generation("owner") == 2
    with pytest.raises(Denied):
        control.verify_permit(permit, now=116)
    with pytest.raises(Denied):
        control.issue_permit(second["id"], now=116)
    restarted = Control(tmp_path / "control.sqlite3", KEY)
    assert restarted.advance_generation("owner") == 3
    assert any(row["action"] == "authorization.revoke_all" for row in restarted.audit_log())


@pytest.mark.parametrize(
    "change", [{"signature": "forged"}, {"expires_at": 9999}, {"generation": 8}]
)
def test_hmac_covers_every_permit_field(tmp_path: Path, change: dict[str, str | int]) -> None:
    control = configured(tmp_path)
    body = approved(control)
    permit = control.issue_permit(body["id"], now=115)
    with pytest.raises(Denied, match="署名"):
        control.verify_permit(permit.model_copy(update=change), now=116)


def test_permit_expiry_and_key_rotation_are_enforced(tmp_path: Path) -> None:
    control = configured(tmp_path)
    body = approved(control)
    permit = control.issue_permit(body["id"], now=115)
    assert permit.expires_at == 145
    with pytest.raises(Denied):
        control.verify_permit(permit, now=145)
    restarted = Control(
        tmp_path / "control.sqlite3", b"a-different-signing-key-with-at-least-32-bytes"
    )
    with pytest.raises(Denied, match="署名"):
        restarted.verify_permit(permit, now=116)


def test_task_claim_is_durable_and_budget_does_not_reset_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opsyne.control.repository.time.time", lambda: 100.0)
    control = configured(tmp_path)
    case = finding(control)
    pending = control.enqueue(case.id)
    assert control.enqueue(case.id).id == pending.id
    task = control.claim_task(daily_limit=1)
    assert task is not None and task.status == "RUNNING" and task.attempts == 1
    restarted = Control(tmp_path / "control.sqlite3", KEY)
    assert restarted.recover_tasks() == 1
    assert restarted.recover_tasks() == 0
    assert restarted.get("task", task.id)["status"] == "FAILED"
    assert restarted.claim_task(daily_limit=1) is None
    new = restarted.enqueue(case.id)
    assert new.id != task.id
    assert restarted.claim_task(daily_limit=1) is None
    assert restarted.get("task", new.id)["status"] == "BLOCKED"


def test_expired_task_is_not_dispatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opsyne.control.repository.time.time", lambda: 100.0)
    control = configured(tmp_path)
    task = control.enqueue(finding(control).id)
    monkeypatch.setattr("opsyne.control.repository.time.time", lambda: 401.0)
    assert control.claim_task(daily_limit=10) is None
    assert control.get("task", task.id)["status"] == "BLOCKED"


def test_finite_task_persists_analysis_and_missing_result_is_failure(tmp_path: Path) -> None:
    control = configured(tmp_path)
    case = finding(control)
    control.enqueue(case.id, role="sre")
    running = control.claim_task(10)
    assert running is not None
    control.finish_task(running, analysis())
    assert Task.model_validate(control.get("task", running.id)).status == "SUCCEEDED"
    assert control.get("analysis", case.id)["unknowns"] == ["Root cause unknown"]
    control.enqueue(case.id)
    missing = control.claim_task(10)
    assert missing is not None
    control.finish_task(missing, None)
    assert control.get("task", missing.id)["status"] == "FAILED"


def test_raw_resend_and_reanalysis_do_not_duplicate_case_or_execute(tmp_path: Path) -> None:
    control = configured(tmp_path)
    control.record_event("raw-1", {"status": "unknown", "raw_ref": "raw-1"})
    control.record_event("raw-1", {"status": "unknown", "raw_ref": "raw-1"})
    case = finding(control)
    assert finding(control).id == case.id
    control.case_status(case.id, "RESOLVED")
    assert finding(control).status == "RESOLVED"
    reopened = finding(control, evidence="raw-2")
    assert reopened.id == case.id and reopened.status == "OPEN"
    assert set(reopened.evidence_ids) == {"raw-1", "raw-2"}
    control.record_event("raw-1", {"status": "interpreted", "raw_ref": "raw-1", "revision": 2})
    assert control.event("raw-1") == {"status": "interpreted", "raw_ref": "raw-1", "revision": 2}
    assert control.event("missing") is None
    with control.db.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0]
    assert count == 2
    assert len(control.objects("case")) == 1
    assert control.objects("permit") == []
    assert control.objects("plan") == []


@pytest.mark.parametrize(
    "scope",
    [
        ("other-service", "source", "error"),
        ("service", "other-source", "error"),
        ("service", "source", "other-kind"),
    ],
)
def test_case_key_collision_does_not_mix_evidence(
    tmp_path: Path, scope: tuple[str, str, str]
) -> None:
    control = configured(tmp_path)
    case = finding(control)
    with pytest.raises(Conflict, match="衝突"):
        control.add_finding("group", *scope, "collision", "high", ["unrelated-raw"], now=91)
    assert control.get("case", case.id)["evidence_ids"] == ["raw-1"]


def test_no_evidence_no_plan(tmp_path: Path) -> None:
    control = configured(tmp_path)
    case = control.add_finding("empty", "service", "source", "error", "No evidence", "high", [])
    with pytest.raises(Denied, match="根拠"):
        control.create_plan(case.id, "restore", "Reason", "proposer", now=100)
