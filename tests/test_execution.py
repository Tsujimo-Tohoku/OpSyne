"""Execution failures, duplicate delivery, and independent target observations."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from opsyne.connectors.demo import DemoConnector
from opsyne.connectors.http import HttpConnector
from opsyne.contracts.execution import (
    Capability,
    CheckConfig,
    ExecutionPermit,
    OperationResult,
    Plan,
    VerificationResult,
    plan_digest,
)
from opsyne.runner.service import Runner
from opsyne.storage.database import Database
from opsyne.verification.service import VerificationService


def fixtures() -> tuple[Plan, ExecutionPermit, Capability]:
    capability = Capability(
        id="restore", service_id="svc", name="Restore demo", kind="demo.restore"
    )
    plan = Plan(
        id="plan-1",
        case_id="case-1",
        service_id="svc",
        target_instance_id="instance-1",
        target_version=1,
        capability_id=capability.id,
        capability_version=1,
        proposer="operator",
        reason="Service is unhealthy",
        evidence_ids=("evidence-1",),
        impact="Restores the demo service",
        success_condition="Independent health observation passes",
        abort_condition="Target changed",
        created_at=90,
        expires_at=200,
    )
    plan = plan.model_copy(update={"digest": plan_digest(plan)})
    permit = ExecutionPermit(
        id="permit-1",
        plan_id=plan.id,
        plan_digest=plan.digest,
        service_id=plan.service_id,
        target_instance_id=plan.target_instance_id,
        target_version=plan.target_version,
        capability_id=plan.capability_id,
        capability_version=plan.capability_version,
        expires_at=150,
        generation=1,
        signature="signature-validated-by-control",
    )
    return plan, permit, capability


def allow() -> None:
    pass


def test_intent_is_durable_before_authorization_and_operation(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    path = tmp_path / "runner.sqlite3"
    runner = Runner(path)
    demo = DemoConnector(tmp_path / "target.sqlite3")
    demo.configure("svc")
    calls: list[str] = []

    def authorize() -> None:
        record = Runner(path).for_plan(plan.id)
        assert record is not None and record.status == "IN_FLIGHT"
        calls.append("authorize")

    def operation(operation_id: str) -> OperationResult:
        assert Runner(path).get(operation_id).status == "IN_FLIGHT"
        calls.append("operate")
        return demo.execute("svc", operation_id)

    result = runner.execute(plan, permit, capability, authorize, operation, now=100)
    assert calls == ["authorize", "operate"]
    assert result.status == "SUCCEEDED"
    assert result.verification is None
    assert VerificationService().verify(lambda: demo.check("svc")).status == "PASS"
    assert Runner(path).get(result.id) == result


def test_duplicate_plan_survives_restart_without_reexecution(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    path = tmp_path / "runner.sqlite3"
    demo = DemoConnector(tmp_path / "target.sqlite3")
    demo.configure("svc")
    first = Runner(path).execute(
        plan, permit, capability, allow, lambda key: demo.execute("svc", key), now=100
    )

    def must_not_run(_: str) -> OperationResult:
        raise AssertionError("Duplicate execution was attempted")

    second = Runner(path).execute(plan, permit, capability, allow, must_not_run, now=100)
    assert second == first
    assert demo.check("svc").evidence["change_count"] == 1


def test_concurrent_runner_instances_claim_one_plan(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    path = tmp_path / "runner.sqlite3"
    first, second = Runner(path), Runner(path)
    demo = DemoConnector(tmp_path / "target.sqlite3")
    demo.configure("svc")

    def execute(runner: Runner) -> str:
        return runner.execute(
            plan, permit, capability, allow, lambda key: demo.execute("svc", key), now=100
        ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, [first, second]))
    assert len(set(results)) == 1
    assert demo.check("svc").evidence["change_count"] == 1


def test_lost_response_reconciles_without_resending(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    runner = Runner(tmp_path / "runner.sqlite3")
    target_path = tmp_path / "target.sqlite3"
    demo = DemoConnector(target_path)
    demo.configure("svc")
    unknown = runner.execute(
        plan,
        permit,
        capability,
        allow,
        lambda key: demo.execute("svc", key, timeout_after_apply=True),
        now=100,
    )
    assert unknown.status == "UNKNOWN"
    assert (
        runner.execute(
            plan, permit, capability, allow, lambda key: demo.execute("svc", key), now=101
        )
        == unknown
    )
    recovered = runner.reconcile(unknown.id, DemoConnector(target_path).lookup, now=102)
    assert recovered.status == "SUCCEEDED"
    assert recovered.verification is None
    assert demo.check("svc").evidence["change_count"] == 1
    demo.configure("svc", healthy=False)
    assert VerificationService().verify(lambda: demo.check("svc")).status == "FAIL"


def test_startup_recovery_retains_unknown_and_does_not_send(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    path = tmp_path / "runner.sqlite3"
    runner = Runner(path)

    def crash(_: str) -> OperationResult:
        raise SystemExit("Simulated process crash")

    with pytest.raises(SystemExit):
        runner.execute(plan, permit, capability, allow, crash, now=100)
    restarted = Runner(path)
    assert restarted.recover_in_flight(now=101) == 1
    assert restarted.recover_in_flight(now=102) == 0
    unknown = restarted.for_plan(plan.id)
    assert unknown is not None and unknown.status == "UNKNOWN"
    result = restarted.reconcile(
        unknown.id, lambda _: OperationResult(status="UNKNOWN", detail="No history"), now=103
    )
    assert result.status == "UNKNOWN"


def test_authorization_denial_commits_failure_without_operation(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    runner = Runner(tmp_path / "runner.sqlite3")
    calls: list[str] = []

    def deny() -> None:
        raise PermissionError("SECRET must not appear in ledger")

    def operation(key: str) -> OperationResult:
        calls.append(key)
        return OperationResult(status="SUCCEEDED", detail="bad")

    with pytest.raises(PermissionError, match="Current authorization denied"):
        runner.execute(plan, permit, capability, deny, operation, now=100)
    record = runner.for_plan(plan.id)
    assert record is not None and record.status == "FAILED"
    assert "SECRET" not in record.model_dump_json()
    assert not calls


@pytest.mark.parametrize(
    "changed",
    [
        {"plan_digest": "changed"},
        {"target_instance_id": "replacement"},
        {"target_version": 2},
        {"service_id": "other"},
        {"capability_id": "other"},
        {"capability_version": 2},
        {"expires_at": 100},
    ],
)
def test_mismatched_permit_never_creates_intent(
    tmp_path: Path, changed: dict[str, str | int]
) -> None:
    plan, permit, capability = fixtures()
    runner = Runner(tmp_path / "runner.sqlite3")
    with pytest.raises(PermissionError):
        runner.execute(
            plan,
            permit.model_copy(update=changed),
            capability,
            allow,
            lambda _: OperationResult(status="SUCCEEDED", detail="bad"),
            now=100,
        )
    assert runner.list() == []


def test_plan_content_tampering_is_rejected(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    runner = Runner(tmp_path / "runner.sqlite3")
    with pytest.raises(PermissionError, match="digest"):
        runner.execute(
            plan.model_copy(update={"reason": "Altered after approval"}),
            permit,
            capability,
            allow,
            lambda _: OperationResult(status="SUCCEEDED", detail="bad"),
            now=100,
        )


def test_failed_ledger_claim_prevents_side_effect(tmp_path: Path) -> None:
    plan, permit, capability = fixtures()
    path = tmp_path / "runner.sqlite3"
    runner = Runner(path)
    Database(path).initialize(
        "CREATE TRIGGER fail_claim BEFORE INSERT ON execution_ledger "
        "BEGIN SELECT RAISE(FAIL, 'disk write unavailable'); END;"
    )
    calls: list[str] = []

    def operation(key: str) -> OperationResult:
        calls.append(key)
        return OperationResult(status="SUCCEEDED", detail="bad")

    with pytest.raises(Exception, match="disk write unavailable"):
        runner.execute(plan, permit, capability, allow, operation, now=100)
    assert calls == []


def http_capability() -> Capability:
    return Capability(
        id="http-restore",
        service_id="svc",
        name="Registered restore",
        kind="http.request",
        endpoint="https://service.internal/restore",
        method="POST",
        body={"mode": "safe"},
        auth_env="OPSYNE_TEST_CONNECTOR_TOKEN",
    )


def test_http_uses_fixed_payload_and_independent_get_without_secret_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSYNE_TEST_CONNECTOR_TOKEN", "never-serialize-me")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.headers["Idempotency-Key"] == "execution-1"
            assert request.headers["Authorization"] == "Bearer never-serialize-me"
            assert json.loads(request.content) == {"mode": "safe"}
            return httpx.Response(202, text="Accepted never-serialize-me")
        return httpx.Response(200, text="unhealthy")

    connector = HttpConnector(transport=httpx.MockTransport(handler))
    result = connector.execute(http_capability(), "execution-1")
    assert result.status == "SUCCEEDED"
    assert "never-serialize-me" not in result.model_dump_json()
    check = connector.check(
        CheckConfig(
            kind="http", endpoint="https://service.internal/health", body_contains='"ok":true'
        )
    )
    assert check.status == "FAIL"
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/restore"),
        ("GET", "/health"),
    ]
    assert connector.lookup("execution-1").status == "UNKNOWN"


def test_http_does_not_follow_redirect_or_forward_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSYNE_TEST_CONNECTOR_TOKEN", "private")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://other.example/steal"})

    result = HttpConnector(transport=httpx.MockTransport(handler)).execute(
        http_capability(), "execution-1"
    )
    assert result.status == "UNKNOWN"
    assert calls == ["https://service.internal/restore"]


def test_http_timeout_remains_unknown_without_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSYNE_TEST_CONNECTOR_TOKEN", "private")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("private", request=request)

    connector = HttpConnector(transport=httpx.MockTransport(handler))
    result = connector.execute(http_capability(), "execution-1")
    assert result.status == "UNKNOWN"
    assert "private" not in result.model_dump_json()
    assert connector.lookup("execution-1").status == "UNKNOWN"
    assert len(calls) == 1


def test_http_oversized_observation_is_unknown() -> None:
    connector = HttpConnector(
        max_response_bytes=32,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="x" * 33)),
    )
    assert (
        connector.check(CheckConfig(kind="http", endpoint="http://127.0.0.1/health")).status
        == "UNKNOWN"
    )


def test_missing_connector_secret_sends_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSYNE_TEST_CONNECTOR_TOKEN", raising=False)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    connector = HttpConnector(transport=httpx.MockTransport(handler))
    assert connector.execute(http_capability(), "execution-1").status == "FAILED"
    assert calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///etc/passwd",
        "https://user:pass@host/",
        "https://host/#fragment",
        "http://host:99999",
        "http://host/\nfoo",
    ],
)
def test_invalid_registered_urls_are_rejected(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        CheckConfig(kind="http", endpoint=endpoint)


def test_unavailable_verification_is_unknown() -> None:
    def fail() -> None:
        raise RuntimeError("secret")

    # A real observation callback can fail before producing any result.
    def check() -> VerificationResult:
        fail()
        raise AssertionError("unreachable")

    result = VerificationService().verify(check)
    assert result.status == "UNKNOWN"
    assert "secret" not in result.model_dump_json()
