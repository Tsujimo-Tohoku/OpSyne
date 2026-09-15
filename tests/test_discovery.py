"""Automatic adapter proposals survive restart without repeated calls or activation."""

from __future__ import annotations

from pathlib import Path

import pytest

from opsyne.contracts.cases import Analysis, Case, Task
from opsyne.contracts.core import Actor, Service
from opsyne.contracts.observations import AdapterDefinition, RawEvent, Source
from opsyne.control.adapters import AdapterRegistry
from opsyne.control.discovery import AdapterDiscovery
from opsyne.control.repository import Conflict, Control

KEY = b"a-test-only-signing-key-of-at-least-32-bytes"


def setup(tmp_path: Path, *, max_attempts: int = 3) -> tuple[Control, AdapterDiscovery, Service]:
    control = Control(tmp_path / "control.sqlite3", KEY)
    service = Service(id="svc", name="Service", instance_id="instance", owner="owner")
    control.register_service(service, "owner")
    discovery = AdapterDiscovery(control, retry_seconds=10, max_attempts=max_attempts)
    return control, discovery, service


def observe(
    discovery: AdapterDiscovery,
    service: Service,
    raw_id: str = "raw-1",
    *,
    at: float = 100,
    fingerprint: str = "format-one",
    supported: bool = True,
) -> Case:
    case = discovery.control.add_finding(
        fingerprint,
        service.id,
        "source",
        "interpretation",
        "Unknown format",
        "WARNING",
        [raw_id],
        now=at,
    )
    raw = RawEvent(
        id=raw_id,
        source_id="source",
        service_id=service.id,
        external_id=raw_id,
        payload='{"message":"Unknown message"}',
        received_at=at,
        digest="digest",
        raw_bytes_b64="",
    )
    discovery.observe(case, raw, service, fingerprint, supported, now=at)
    return case


def set_task(control: Control, task: Task, status: str, detail: str = "") -> Task:
    updated = Task.model_validate({**task.model_dump(), "status": status, "detail": detail})
    with control.db.connection() as db:
        control._put(db, "task", updated.id, updated.model_dump(mode="json"))
    return updated


def draft(control: Control, task: Task) -> AdapterRegistry:
    registry = AdapterRegistry(
        control, lambda _: Source(id="source", service_id="svc", name="Logs")
    )
    registry.propose(
        AdapterDefinition(
            id=f"adapter-{task.id}",
            name="Candidate",
            source_id="source",
            target_instance_id="instance",
            target_version=1,
            version=1,
            fields={"message": "message"},
        ),
        "agent:adapter",
    )
    return registry


def test_observation_coalesces_without_key_and_survives_restart(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    observe(discovery, service, "raw-2", at=102)
    observe(discovery, service, "raw-2", at=103)
    assert discovery.schedule(enabled=True, configured=True, now=104) == []
    assert discovery.schedule(enabled=False, configured=True, now=105) == []
    assert discovery.schedule(enabled=True, configured=False, now=105) == []
    restarted = AdapterDiscovery(Control(control.db.path, KEY), retry_seconds=10)
    observe(restarted, service, "raw-2", at=105)
    group = restarted.for_case(case.id)
    assert group is not None and group["event_count"] == 2
    assert group["evidence_ids"] == ["raw-1", "raw-2"]
    (task,) = restarted.schedule(enabled=True, configured=True, now=105)
    assert task.automatic
    assert task.evidence_ids == ("raw-1", "raw-2")
    assert task.target_instance_id == service.instance_id
    assert task.target_version == service.version
    assert restarted.schedule(enabled=True, configured=True, now=106) == []
    assert len(control.objects("task")) == 1


def test_samples_are_bounded_and_queued_scope_is_frozen(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    for index in range(30):
        case = observe(discovery, service, f"raw-{index}", at=100)
    (task,) = discovery.schedule(enabled=True, configured=True, now=105)
    assert task.evidence_ids == tuple(f"raw-{index}" for index in range(10, 30))
    for index in range(30, 60):
        observe(discovery, service, f"raw-{index}", at=106)
    group = discovery.for_case(case.id)
    assert group is not None and group["event_count"] == 60
    assert group["evidence_ids"] == [f"raw-{index}" for index in range(40, 60)]
    assert Task.model_validate(control.get("task", task.id)).evidence_ids == task.evidence_ids


def test_manual_request_promotes_existing_auto_task_without_duplication(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    (task,) = discovery.schedule(enabled=True, configured=True, now=105)
    manual = discovery.request(case.id, now=106)
    assert manual.id == task.id and not manual.automatic
    operator = control.enqueue(case.id)
    assert operator.id != task.id
    assert discovery.request(case.id, now=107).id == task.id
    assert len(control.objects("task")) == 2


def test_retry_requires_new_evidence_and_interval_then_stops_at_cap(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path, max_attempts=2)
    case = observe(discovery, service)
    (task,) = discovery.schedule(enabled=True, configured=True, now=105)
    set_task(control, task, "FAILED", "Provider unavailable")
    assert discovery.schedule(enabled=True, configured=True, now=106) == []
    assert discovery.schedule(enabled=True, configured=True, now=150) == []
    observe(discovery, service, "raw-2", at=151)
    (retry,) = discovery.schedule(enabled=True, configured=True, now=151)
    assert retry.id != task.id and retry.evidence_ids == ("raw-1", "raw-2")
    set_task(control, retry, "FAILED")
    discovery.schedule(enabled=True, configured=True, now=152)
    observe(discovery, service, "raw-3", at=200)
    assert discovery.schedule(enabled=True, configured=True, now=200) == []
    group = discovery.for_case(case.id)
    assert group is not None and group["status"] == "BLOCKED" and group["attempts"] == 2
    manual = discovery.request(case.id, now=201)
    assert manual.id != retry.id and not manual.automatic


def test_new_evidence_alone_does_not_bypass_retry_interval(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    observe(discovery, service)
    (task,) = discovery.schedule(enabled=True, configured=True, now=105)
    set_task(control, task, "FAILED")
    discovery.schedule(enabled=True, configured=True, now=106)
    observe(discovery, service, "raw-2", at=107)
    assert discovery.schedule(enabled=True, configured=True, now=115) == []
    assert len(discovery.schedule(enabled=True, configured=True, now=116)) == 1


def test_interrupted_task_is_not_blindly_retried_after_restart(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    (task,) = discovery.schedule(enabled=True, configured=True, now=105)
    set_task(control, task, "RUNNING")
    restarted = Control(control.db.path, KEY)
    assert restarted.recover_tasks() == 1
    discovery = AdapterDiscovery(restarted, retry_seconds=10)
    assert discovery.schedule(enabled=True, configured=True, now=106) == []
    assert discovery.schedule(enabled=True, configured=True, now=1000) == []
    group = discovery.for_case(case.id)
    assert group is not None and group["status"] == "RETRY_WAIT"
    assert len(restarted.objects("task")) == 1


def test_persisted_draft_recovers_after_crash_and_requires_approval(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    (task,) = discovery.schedule(enabled=True, configured=True, now=105)
    set_task(control, task, "RUNNING")
    registry = draft(control, task)
    control.recover_tasks()
    assert discovery.schedule(enabled=True, configured=True, now=106) == []
    group = discovery.for_case(case.id)
    assert group is not None and group["status"] == "AWAITING_APPROVAL"
    adapter_id = f"adapter-{task.id}"
    body = control.get("adapter", adapter_id)
    assert body["status"] == "DRAFT"
    registry.approve(adapter_id, body["digest"], Actor(actor="reviewer", role="approver"))
    discovery.schedule(enabled=False, configured=False, now=107)
    group = discovery.for_case(case.id)
    assert group is not None and group["status"] == "COVERED"
    observe(discovery, service, "raw-2", at=1000)
    assert discovery.schedule(enabled=True, configured=True, now=1000) == []


def test_reproposal_keeps_previous_draft_until_replacement_is_saved(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    first = discovery.request(case.id, now=100)
    draft(control, first)
    set_task(control, first, "SUCCEEDED")
    discovery.schedule(enabled=False, configured=False, now=101)
    second = discovery.request(case.id, now=102)
    assert control.get("adapter", f"adapter-{first.id}")["status"] == "DRAFT"
    draft(control, second)
    discovery.schedule(enabled=False, configured=False, now=103)
    assert control.get("adapter", f"adapter-{first.id}")["status"] == "REVOKED"
    assert control.get("adapter", f"adapter-{second.id}")["status"] == "DRAFT"


def test_reproposal_does_not_revoke_approved_adapter(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    first = discovery.request(case.id, now=100)
    registry = draft(control, first)
    set_task(control, first, "SUCCEEDED")
    adapter_id = f"adapter-{first.id}"
    registry.approve(
        adapter_id,
        control.get("adapter", adapter_id)["digest"],
        Actor(actor="reviewer", role="approver"),
    )
    discovery.schedule(enabled=False, configured=False, now=101)
    second = discovery.request(case.id, now=102)
    draft(control, second)
    discovery.schedule(enabled=False, configured=False, now=103)
    assert control.get("adapter", adapter_id)["status"] == "APPROVED"


def test_source_disabled_and_stale_service_prevent_queue(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    source = Source(id="source", service_id=service.id, name="Logs", enabled=False)
    discovery.source = lambda _: source
    assert discovery.schedule(enabled=True, configured=True, now=105) == []
    with pytest.raises(Conflict, match="無効化"):
        discovery.request(case.id, now=105)
    discovery.source = lambda _: source.model_copy(update={"enabled": True})
    control.register_service(service.model_copy(update={"version": 2}), "owner")
    assert discovery.schedule(enabled=True, configured=True, now=200) == []
    with pytest.raises(Conflict, match="変更"):
        discovery.request(case.id, now=200)
    assert control.objects("task") == []


def test_unsupported_samples_never_queue(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service, supported=False)
    assert discovery.schedule(enabled=True, configured=True, now=105) == []
    with pytest.raises(Conflict, match="JSON"):
        discovery.request(case.id, now=106)
    assert control.objects("task") == []


def test_changed_scope_cancels_queued_task_before_budget_is_reserved(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    task = discovery.request(case.id)
    control.register_service(service.model_copy(update={"version": 2}), "owner")
    discovery.schedule(enabled=True, configured=True)
    assert control.get("task", task.id)["status"] == "BLOCKED"
    assert control.claim_task(daily_limit=20) is None
    with control.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM budget").fetchone()[0] == 0


def test_budget_block_refunds_unsent_attempt_and_retries_after_daily_reset(tmp_path: Path) -> None:
    control, discovery, service = setup(tmp_path)
    case = observe(discovery, service)
    task = discovery.request(case.id)
    assert control.claim_task(daily_limit=0) is None
    assert control.get("task", task.id)["status"] == "BLOCKED"
    discovery.schedule(enabled=True, configured=True, now=task.created_at + 1)
    group = discovery.for_case(case.id)
    assert group is not None and group["attempts"] == 0
    next_at = group["next_attempt_at"]
    assert next_at >= (task.created_at // 86400 + 1) * 86400
    assert discovery.schedule(enabled=True, configured=True, now=next_at - 1) == []
    assert len(control.objects("task")) == 1
    (retry,) = discovery.schedule(enabled=True, configured=True, now=next_at)
    assert retry.id != task.id and retry.evidence_ids == task.evidence_ids
    group = discovery.for_case(case.id)
    assert group is not None and group["attempts"] == 1


def test_manual_request_without_group_preserves_existing_workflow(tmp_path: Path) -> None:
    control, discovery, _ = setup(tmp_path)
    case = control.add_finding("other", "svc", "source", "error", "Failure", "high", [])
    task = discovery.request(case.id)
    assert task.role == "adapter" and not task.automatic and task.discovery_id is None
    control.finish_task(
        task,
        Analysis(summary="No adapter", facts=[], hypotheses=[], unknowns=[], recommendations=[]),
    )
    assert discovery.request(case.id).id != task.id


@pytest.mark.parametrize(
    ("coalesce", "retry", "attempts"), [(301, 10, 3), (-1, 10, 3), (0, 0, 3), (0, 10, 21)]
)
def test_invalid_limits_are_rejected(
    tmp_path: Path, coalesce: int, retry: int, attempts: int
) -> None:
    control = Control(tmp_path / "control.sqlite3", KEY)
    with pytest.raises(ValueError):
        AdapterDiscovery(control, coalesce, retry, attempts)
