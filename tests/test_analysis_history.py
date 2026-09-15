"""Automatic adapter reports must preserve human-requested investigation evidence."""

from pathlib import Path
from typing import Literal

import pytest

from opsyne.contracts.cases import Analysis
from opsyne.contracts.core import Service
from opsyne.control.repository import Control

KEY = b"synthetic-analysis-history-signing-key-32bytes"
Role = Literal["operator", "adapter"]


def configured(tmp_path: Path) -> tuple[Control, str]:
    control = Control(tmp_path / "control.sqlite3", KEY)
    control.register_service(
        Service(id="service", name="Synthetic service", instance_id="instance", owner="owner"),
        "owner",
    )
    case = control.add_finding(
        "unknown-format", "service", "source", "interpretation", "Unknown log", "high", ["raw-1"]
    )
    return control, case.id


def finish(control: Control, case_id: str, role: Role, summary: str) -> str:
    control.enqueue(case_id, role)
    task = control.claim_task(20)
    assert task is not None
    assert task.role == role
    control.finish_task(
        task,
        Analysis(summary=summary, facts=[], hypotheses=[], unknowns=[], recommendations=[]),
    )
    return task.id


@pytest.mark.parametrize("first", ["operator", "adapter"])
def test_both_completion_orders_keep_investigation_and_adapter_reports(
    tmp_path: Path, first: Role
) -> None:
    control, case_id = configured(tmp_path)
    second: Role = "adapter" if first == "operator" else "operator"
    first_task = finish(control, case_id, first, f"{first} evidence")
    second_task = finish(control, case_id, second, f"{second} evidence")

    reopened = Control(tmp_path / "control.sqlite3", KEY)
    assert reopened.get("analysis", case_id)["summary"] == "operator evidence"
    assert reopened.get("adapter_analysis", case_id)["summary"] == "adapter evidence"
    assert reopened.get("task_analysis", first_task)["summary"] == f"{first} evidence"
    assert reopened.get("task_analysis", second_task)["summary"] == f"{second} evidence"


def test_adapter_reproposal_preserves_previous_task_report_and_legacy_summary(
    tmp_path: Path,
) -> None:
    control, case_id = configured(tmp_path)
    previous_id = finish(control, case_id, "adapter", "Initial adapter rationale")
    legacy = control.get("analysis", case_id)
    legacy.pop("task_role")
    with control.db.connection() as db:
        control._put(db, "analysis", case_id, legacy)

    latest_id = finish(control, case_id, "adapter", "Revised adapter rationale")
    assert control.get("analysis", case_id)["task_id"] == latest_id
    assert control.get("adapter_analysis", case_id)["task_id"] == latest_id
    assert control.get("task_analysis", previous_id)["summary"] == "Initial adapter rationale"


def test_failed_adapter_attempt_does_not_replace_successful_reports(tmp_path: Path) -> None:
    control, case_id = configured(tmp_path)
    operator_id = finish(control, case_id, "operator", "Investigated root cause")
    adapter_id = finish(control, case_id, "adapter", "Adapter rationale")
    control.enqueue(case_id, "adapter")
    failed = control.claim_task(20)
    assert failed is not None
    control.finish_task(failed, None, "Synthetic model failure")

    assert control.get("analysis", case_id)["task_id"] == operator_id
    assert control.get("adapter_analysis", case_id)["task_id"] == adapter_id
    with pytest.raises(KeyError):
        control.get("task_analysis", failed.id)
