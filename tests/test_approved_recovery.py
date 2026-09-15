import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import JsonValue

from opsyne.contracts.cases import Analysis, RecoveryChoice
from opsyne.contracts.core import Actor
from opsyne.control.repository import Denied
from opsyne.runtime import Runtime


def proposed(path: Path) -> tuple[Runtime, dict[str, Any]]:
    runtime = Runtime(path, auto_adapter_proposals=False)
    runtime.seed_demo(Actor(actor="owner", role="admin"))
    case = next(
        item for item in runtime.control.objects("case") if item["kind"] == "operation_failure"
    )
    runtime.investigator = Mock()
    runtime.investigator.investigate.return_value = Analysis(
        summary="synthetic",
        facts=[],
        hypotheses=[],
        unknowns=[],
        recommendations=[],
        recovery=RecoveryChoice(
            capability_id="demo-restore",
            capability_version=1,
            reason="Restore synthetic failure",
            evidence_ids=case["evidence_ids"],
        ),
    )
    runtime.control.enqueue(case["id"])
    task = runtime.run_task()
    assert task is not None and task.status == "SUCCEEDED"
    return runtime, runtime.control.objects("plan")[0]


def test_ai_plan_approval_survives_restart_and_runs_once(tmp_path: Path) -> None:
    runtime, plan = proposed(tmp_path)
    reviewer = Actor(actor="reviewer", role="approver")
    runtime.control.approve(plan["id"], plan["digest"], reviewer, run_recovery=True)
    restarted = Runtime(tmp_path)
    result = restarted.run_recovery()
    assert result is not None and result["status"] == "COMPLETED"
    restarted.control.approve(plan["id"], plan["digest"], reviewer, run_recovery=True)
    assert restarted.run_recovery() is None
    assert restarted.demo.check("demo-checkout").evidence["change_count"] == 1
    assert restarted.case_detail(plan["case_id"])["case"]["status"] == "RESOLVED"


def test_no_self_approval_or_direct_approver_execution(tmp_path: Path) -> None:
    runtime, plan = proposed(tmp_path)
    with pytest.raises(Denied):
        runtime.control.approve(
            plan["id"],
            plan["digest"],
            Actor(actor=plan["proposer"], role="admin"),
            run_recovery=True,
        )
    with pytest.raises(Denied):
        runtime.execute(plan["id"], Actor(actor="reviewer", role="approver"))
    assert runtime.runner.list() == []


def test_changed_target_blocks_queued_operation(tmp_path: Path) -> None:
    runtime, plan = proposed(tmp_path)
    runtime.control.approve(
        plan["id"], plan["digest"], Actor(actor="reviewer", role="approver"), run_recovery=True
    )
    service = runtime.control.service("demo-checkout")
    runtime.control.register_service(service.model_copy(update={"version": 2}), "owner")
    result = runtime.run_recovery()
    assert result is not None and result["status"] == "BLOCKED"
    assert runtime.demo.check("demo-checkout").evidence["change_count"] == 0


def test_duplicate_analysis_does_not_duplicate_plan(tmp_path: Path) -> None:
    runtime, plan = proposed(tmp_path)
    runtime.control.enqueue(plan["case_id"])
    runtime.run_task()
    assert len(runtime.control.objects("plan")) == 1


def test_recovery_save_failure_rolls_back_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, plan = proposed(tmp_path)
    original_put = runtime.control._put

    def fail_recovery(db: sqlite3.Connection, kind: str, object_id: str, body: JsonValue) -> None:
        if kind == "recovery":
            raise OSError("synthetic recovery persistence failure")
        original_put(db, kind, object_id, body)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.control, "_put", fail_recovery)
        with pytest.raises(OSError, match="synthetic recovery persistence failure"):
            runtime.control.approve(
                plan["id"],
                plan["digest"],
                Actor(actor="reviewer", role="approver"),
                run_recovery=True,
            )

    restarted = Runtime(tmp_path)
    assert restarted.control.get("plan", plan["id"])["status"] == "DRAFT"
    assert restarted.control.objects("recovery") == []
    assert restarted.run_recovery() is None
    assert restarted.runner.list() == []
