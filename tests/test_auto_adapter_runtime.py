"""Offline end-to-end acceptance for automatic proposals, with no automatic activation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from opsyne.contracts.adapter_proposals import AdapterProposal
from opsyne.contracts.cases import EvidenceGrant, Task
from opsyne.contracts.core import Actor, Service
from opsyne.contracts.observations import RawEvent, RawInput, Source
from opsyne.runtime import Runtime


@dataclass
class ProposalStub:
    message_path: str = "message"
    fail: bool = False
    calls: list[tuple[Task, list[dict[str, JsonValue]]]] = field(default_factory=list)

    def propose(
        self,
        task: Task,
        evidence: list[dict[str, JsonValue]],
        source: Source,
        service: Service,
    ) -> AdapterProposal:
        self.calls.append((task, evidence))
        if self.fail:
            raise TimeoutError("synthetic timeout")
        return AdapterProposal.model_validate(
            {
                "name": "Synthetic checkout JSON",
                "fields": [
                    {"field": "message", "path": self.message_path},
                    {"field": "outcome", "path": "result"},
                    {"field": "severity", "path": "level"},
                ],
                "conditions": [{"path": "schema", "value": "checkout-v1"}],
                "outcome_map": [{"input": "failed", "output": "FAILURE"}],
                "severity_map": [{"input": "error", "output": "ERROR"}],
                "rationale": [
                    {"text": "Synthetic fields are present", "evidence_ids": [evidence[0]["id"]]}
                ],
                "unknowns": [],
            }
        )


@dataclass
class Harness:
    runtime: Runtime
    stub: ProposalStub
    sequence: int = 0

    def ingest(self, count: int = 1, payload: str | None = None) -> list[RawEvent]:
        events = []
        for _ in range(count):
            self.sequence += 1
            events.append(
                RawInput(
                    external_id=f"event-{self.sequence}",
                    payload=payload
                    if payload is not None
                    else json.dumps(
                        {
                            "schema": "checkout-v1",
                            "message": f"synthetic failure {self.sequence}",
                            "result": "failed",
                            "level": "error",
                        }
                    ),
                )
            )
        raw = self.runtime.collector.ingest("checkout-log", events)
        while self.runtime.collector.pending(1):
            self.runtime.poll()
        return raw

    def group(self) -> dict[str, Any]:
        groups = self.runtime.discoveries.all()
        assert len(groups) == 1
        return groups[0]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    # A synthetic configured flag plus a stub avoids constructing an SDK or reading .env.
    runtime = Runtime(tmp_path, api_key=None, adapter_coalesce_seconds=0)
    runtime.llm_configured = True
    runtime.control.register_service(
        Service(id="checkout", name="Checkout", instance_id="checkout-local", owner="owner"),
        "owner",
    )
    runtime.collector.register_source(
        Source(id="checkout-log", service_id="checkout", name="Checkout JSON")
    )
    stub = ProposalStub()
    monkeypatch.setattr(runtime.adapter_agent, "propose", stub.propose)
    return Harness(runtime, stub)


def test_unknown_logs_group_samples_and_queue_without_llm_in_collection(harness: Harness) -> None:
    raw = harness.ingest(20)
    group = harness.group()
    assert group["event_count"] == 20
    assert group["status"] == "QUEUED"
    assert set(group["evidence_ids"]) == {item.id for item in raw}
    cases = harness.runtime.control.objects("case")
    assert len(cases) == 1 and cases[0]["kind"] == "interpretation"
    tasks = harness.runtime.control.objects("task")
    assert len(tasks) == 1 and tasks[0]["role"] == "adapter"
    assert tasks[0]["automatic"] is True
    assert harness.stub.calls == []
    assert harness.runtime.collector.pending() == []


def test_valid_proposal_waits_for_human_and_never_operates(harness: Harness) -> None:
    harness.ingest(20)
    runtime = harness.runtime
    result = runtime.run_task()
    assert result is not None and result.status == "SUCCEEDED"
    assert len(harness.stub.calls) == 1
    assert len(harness.stub.calls[0][1]) == 20
    group = harness.group()
    assert group["status"] == "AWAITING_APPROVAL"
    draft = runtime.control.get("adapter", group["adapter_id"])
    assert draft["status"] == "DRAFT" and draft["proposer"] == "agent:adapter"
    validation = runtime.validate_adapter(draft["id"])
    assert validation["sample_count"] == validation["supported_count"] == 20
    assert runtime.adapters.active() == []
    assert runtime.control.objects("plan") == []
    assert runtime.runner.list() == []
    harness.ingest(30)
    for _ in range(3):
        assert runtime.run_task() is None
    assert len(harness.stub.calls) == 1
    assert len(runtime.control.objects("task")) == 1
    assert len(harness.group()["evidence_ids"]) == 20


def test_human_approval_enables_mapping_without_more_proposals(harness: Harness) -> None:
    harness.ingest(2)
    runtime = harness.runtime
    assert runtime.run_task() is not None
    draft = runtime.control.get("adapter", harness.group()["adapter_id"])
    runtime.adapters.approve(draft["id"], draft["digest"], Actor(actor="reviewer", role="approver"))
    runtime.reprocess("checkout-log")
    raw = harness.ingest(3)
    for item in raw:
        event = runtime.control.event(item.id)
        assert event is not None and event["parse_status"] == "KNOWN"
    assert harness.group()["status"] == "COVERED"
    assert runtime.run_task() is None
    assert len(harness.stub.calls) == 1
    assert runtime.control.objects("plan") == []
    assert runtime.runner.list() == []


@pytest.mark.parametrize("payload", ["not-json", "{broken", "[]", '"scalar"'])
def test_unsupported_inputs_are_preserved_and_blocked_without_llm(
    harness: Harness, payload: str
) -> None:
    raw = harness.ingest(3, payload)
    assert harness.group()["status"] == "BLOCKED"
    assert harness.group()["event_count"] == 3
    assert harness.runtime.run_task() is None
    assert harness.stub.calls == []
    assert harness.runtime.control.objects("task") == []
    assert all(harness.runtime.collector.raw(item.id) is not None for item in raw)


def test_incorrect_declared_path_prevents_draft_even_if_other_fields_work(harness: Harness) -> None:
    harness.stub.message_path = "missing.message"
    harness.ingest(2)
    task = harness.runtime.run_task()
    assert task is not None and task.status == "FAILED"
    assert harness.runtime.control.objects("adapter") == []
    assert harness.group()["status"] == "RETRY_WAIT"
    assert len(harness.stub.calls) == 1


def test_overlapping_approved_definition_prevents_a_new_draft(harness: Harness) -> None:
    raw = harness.ingest(2)
    task = Task.model_validate(harness.runtime.control.objects("task")[0])
    proposal = harness.stub.propose(
        task,
        [{"id": raw[0].id}],
        harness.runtime.source("checkout-log"),
        harness.runtime.control.service("checkout"),
    )
    harness.stub.calls.clear()
    definition = proposal.to_definition(
        "existing",
        harness.runtime.source("checkout-log"),
        harness.runtime.control.service("checkout"),
    )
    existing = harness.runtime.adapters.propose(definition, "owner")
    harness.runtime.adapters.approve(
        existing["id"], existing["digest"], Actor(actor="reviewer", role="approver")
    )
    result = harness.runtime.run_task()
    assert result is not None and result.status == "FAILED"
    assert len(harness.runtime.control.objects("adapter")) == 1
    assert harness.group()["status"] == "RETRY_WAIT"


def test_retry_waits_for_both_new_evidence_and_elapsed_cooldown(harness: Harness) -> None:
    harness.stub.fail = True
    harness.ingest()
    runtime = harness.runtime
    first = runtime.run_task()
    assert first is not None and first.status == "FAILED"
    retry_at = float(harness.group()["next_attempt_at"])
    assert runtime.discoveries.schedule(enabled=True, configured=True, now=retry_at + 1) == []
    harness.ingest()
    assert runtime.discoveries.schedule(enabled=True, configured=True, now=retry_at - 1) == []
    tasks = runtime.discoveries.schedule(enabled=True, configured=True, now=retry_at + 1)
    assert len(tasks) == 1 and tasks[0].id != first.id
    assert len(harness.stub.calls) == 1


def test_manual_retry_can_bypass_cooldown_without_auto_activation(harness: Harness) -> None:
    harness.stub.fail = True
    harness.ingest()
    first = harness.runtime.run_task()
    assert first is not None and first.status == "FAILED"
    retry = harness.runtime.discoveries.request(harness.group()["case_id"])
    assert retry.id != first.id and not retry.automatic
    harness.stub.fail = False
    second = harness.runtime.run_task()
    assert second is not None and second.status == "SUCCEEDED"
    assert len(harness.stub.calls) == 2
    assert harness.group()["status"] == "AWAITING_APPROVAL"
    assert harness.runtime.adapters.active() == []


def test_reprocess_creates_no_discoveries_tasks_or_llm_calls(harness: Harness) -> None:
    harness.runtime.auto_adapter_proposals = False
    harness.ingest(2)
    groups = harness.runtime.discoveries.all()
    cases = harness.runtime.control.objects("case")
    result = harness.runtime.reprocess("checkout-log")
    assert result["reprocessed"] == 2
    assert harness.runtime.discoveries.all() == groups
    assert harness.runtime.control.objects("case") == cases
    assert harness.runtime.control.objects("task") == []
    assert harness.stub.calls == []


def test_frozen_grant_survives_case_recent_evidence_window(harness: Harness) -> None:
    original = harness.ingest(2)
    runtime = harness.runtime
    task = runtime.control.claim_task(runtime.daily_call_limit)
    assert task is not None and set(task.evidence_ids) == {item.id for item in original}
    harness.ingest(105)
    case = runtime.control.get("case", task.case_id)
    assert not set(task.evidence_ids).intersection(case["evidence_ids"])
    grant = EvidenceGrant(
        task_id=task.id,
        case_id=task.case_id,
        service_id=task.service_id,
        role="adapter",
        evidence_ids=task.evidence_ids,
        expires_at=task.expires_at,
    )
    runtime.validate_grant(grant)
    assert {item["id"] for item in runtime.gateway.retrieve(grant)} == set(task.evidence_ids)


@pytest.mark.parametrize("setting", ["disabled", "unconfigured"])
def test_disabled_or_unconfigured_auto_proposals_do_not_queue(
    harness: Harness, setting: str
) -> None:
    if setting == "disabled":
        harness.runtime.auto_adapter_proposals = False
    else:
        harness.runtime.llm_configured = False
    raw = harness.ingest(2)
    assert harness.runtime.control.objects("task") == []
    assert harness.runtime.run_task() is None
    assert harness.stub.calls == []
    assert all(harness.runtime.collector.raw(item.id) is not None for item in raw)


def test_queued_auto_task_pauses_after_disable_and_manual_request_can_resume(
    harness: Harness,
) -> None:
    harness.ingest()
    queued = harness.runtime.control.objects("task")[0]
    harness.runtime.auto_adapter_proposals = False
    assert harness.runtime.run_task() is None
    assert harness.stub.calls == []
    manual = harness.runtime.discoveries.request(harness.group()["case_id"])
    assert manual.id == queued["id"] and not manual.automatic
    result = harness.runtime.run_task()
    assert result is not None and result.status == "SUCCEEDED"
    assert len(harness.stub.calls) == 1


def test_operator_investigation_is_not_deduplicated_as_adapter_task(harness: Harness) -> None:
    harness.ingest()
    case_id = harness.group()["case_id"]
    operator = harness.runtime.control.enqueue(case_id, role="operator")
    adapter = next(
        item for item in harness.runtime.control.objects("task") if item["role"] == "adapter"
    )
    assert operator.id != adapter["id"] and operator.role == "operator"
    assert len(harness.runtime.control.objects("task")) == 2


def test_automatic_groups_share_the_daily_llm_call_limit(harness: Harness) -> None:
    harness.runtime.daily_call_limit = 1
    harness.ingest()
    harness.ingest(payload='{"schema":"checkout-v2","message":"different format"}')
    first = harness.runtime.run_task()
    assert first is not None and first.status == "SUCCEEDED"
    assert harness.runtime.run_task() is None
    assert len(harness.stub.calls) == 1
    tasks = harness.runtime.control.objects("task")
    assert len(tasks) == 2
    assert {item["status"] for item in tasks} == {"SUCCEEDED", "BLOCKED"}
    assert harness.runtime.collector.pending() == []


def test_changed_service_version_rejects_queued_task_before_llm(harness: Harness) -> None:
    harness.ingest()
    service = harness.runtime.control.service("checkout")
    harness.runtime.control.register_service(service.model_copy(update={"version": 2}), "owner")
    result = harness.runtime.run_task()
    assert result is None
    assert harness.runtime.control.objects("task")[0]["status"] == "BLOCKED"
    assert harness.stub.calls == []
    assert harness.runtime.control.objects("adapter") == []
    assert harness.group()["status"] == "BLOCKED"


def test_restart_retains_one_pending_task_and_its_frozen_samples(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = harness.ingest(2)
    original_task = harness.runtime.control.objects("task")[0]
    restarted = Runtime(harness.runtime.data_dir, api_key=None, adapter_coalesce_seconds=0)
    restarted.llm_configured = True
    monkeypatch.setattr(restarted.adapter_agent, "propose", harness.stub.propose)
    restarted.poll()
    assert len(restarted.control.objects("task")) == 1
    result = restarted.run_task()
    assert result is not None and result.id == original_task["id"]
    assert result.status == "SUCCEEDED"
    assert set(result.evidence_ids) == {item.id for item in raw}
    assert len(harness.stub.calls) == 1
    assert restarted.run_task() is None
