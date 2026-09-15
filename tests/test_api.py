"""Authenticated API acceptance flows, with no external service or LLM calls."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opsyne.api.app import create_app
from opsyne.connectors.http import HttpConnector
from opsyne.runtime import Runtime


@dataclass
class API:
    app: FastAPI
    client: TestClient
    path: Path

    def headers(self, actor: str) -> dict[str, str]:
        entries = json.loads((self.path / "tokens.json").read_text(encoding="utf-8"))
        token = next(str(item["token"]) for item in entries if item["actor"] == actor)
        return {"Authorization": f"Bearer {token}"}

    def get(self, path: str, actor: str = "viewer") -> dict[str, Any]:
        response = self.client.get(path, headers=self.headers(actor))
        assert response.status_code == 200, response.text
        value: dict[str, Any] = response.json()
        return value

    def post(
        self, path: str, actor: str = "operator", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self.client.post(path, headers=self.headers(actor), json=body)
        assert response.status_code == 200, response.text
        value: dict[str, Any] = response.json()
        return value

    @property
    def runtime(self) -> Runtime:
        return cast(Runtime, self.app.state.runtime)


@contextmanager
def api_session(path: Path) -> Iterator[API]:
    app = create_app(path, background=False)
    with TestClient(app) as client:
        yield API(app, client, path)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[API]:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPSYNE_AUTO_INVESTIGATE", raising=False)
    with api_session(tmp_path) as session:
        yield session


def demo_plan(api: API, proposer: str = "operator") -> tuple[dict[str, Any], dict[str, Any]]:
    api.post("/api/demo", "owner")
    case = next(
        item for item in api.get("/api/overview")["cases"] if item["kind"] == "operation_failure"
    )
    plan = api.post(
        f"/api/cases/{case['id']}/plans",
        proposer,
        {"capability_id": "demo-restore", "reason": "Restore the observed checkout failure"},
    )
    return case, plan


def approve(api: API, plan: dict[str, Any]) -> dict[str, Any]:
    return api.post(f"/api/plans/{plan['id']}/approve", "reviewer", {"digest": plan["digest"]})


def test_human_approval_execution_and_independent_resolution(api: API) -> None:
    case, plan = demo_plan(api)
    assert plan["proposer"] == "operator" and plan["status"] == "DRAFT"
    denied = api.client.post(
        f"/api/plans/{plan['id']}/approve",
        headers=api.headers("operator"),
        json={"digest": plan["digest"]},
    )
    assert denied.status_code == 403
    assert approve(api, plan)["status"] == "APPROVED"
    execution = api.post(f"/api/plans/{plan['id']}/execute")
    assert execution["status"] == "SUCCEEDED" and execution["verification"] is None
    assert api.get(f"/api/cases/{case['id']}")["case"]["status"] == "VERIFYING"
    duplicate = api.post(f"/api/plans/{plan['id']}/execute")
    assert duplicate["id"] == execution["id"]
    observed = api.post("/api/services/demo-checkout/verify")
    assert observed["evidence"]["change_count"] == 1
    verified = api.post(f"/api/executions/{execution['id']}/verify")
    assert verified["status"] == "PASS"
    detail = api.get(f"/api/cases/{case['id']}")
    assert detail["case"]["status"] == "RESOLVED"
    assert detail["executions"][0]["verification"]["status"] == "PASS"
    assert len(api.get("/api/overview")["executions"]) == 1


def test_admin_cannot_approve_own_plan_or_forge_proposer(api: API) -> None:
    case, plan = demo_plan(api, "owner")
    response = api.client.post(
        f"/api/plans/{plan['id']}/approve",
        headers=api.headers("owner"),
        json={"digest": plan["digest"]},
    )
    assert response.status_code == 403
    injected = api.client.post(
        f"/api/cases/{case['id']}/plans",
        headers=api.headers("operator"),
        json={"capability_id": "demo-restore", "reason": "Injected", "proposer": "owner"},
    )
    assert injected.status_code == 422
    assert approve(api, plan)["approver"] == "reviewer"


def test_approved_workflow_survives_api_restart_and_preserves_credentials(api: API) -> None:
    case, plan = demo_plan(api)
    approve(api, plan)
    original_operator = api.headers("operator")
    with api_session(api.path) as restarted:
        response = restarted.client.get("/api/session", headers=original_operator)
        assert response.status_code == 200
        assert response.json()["actor"] == "operator"
        execution = restarted.post(f"/api/plans/{plan['id']}/execute")
        assert execution["status"] == "SUCCEEDED"
    with api_session(api.path) as restarted_again:
        duplicate = restarted_again.post(f"/api/plans/{plan['id']}/execute")
        assert duplicate["id"] == execution["id"]
        assert (
            restarted_again.post("/api/services/demo-checkout/verify")["evidence"]["change_count"]
            == 1
        )
        restarted_again.post(f"/api/executions/{execution['id']}/verify")
        assert restarted_again.get(f"/api/cases/{case['id']}")["case"]["status"] == "RESOLVED"


@pytest.mark.parametrize(
    "path", ["/api/session", "/api/overview", "/api/openapi.json", "/api/evidence"]
)
def test_missing_or_invalid_authentication_is_rejected(api: API, path: str) -> None:
    assert api.client.get(path).status_code == 401
    assert api.client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/demo", None),
        ("/api/poll", None),
        ("/api/authorization/revoke-all", None),
        ("/api/plans/missing/execute", None),
        ("/api/plans/missing/approve", {"digest": "0" * 64}),
        ("/api/plans/missing/reject", {"reason": "Reject"}),
        ("/api/executions/missing/verify", None),
    ],
)
def test_viewer_cannot_mutate(api: API, path: str, body: dict[str, Any] | None) -> None:
    response = api.client.post(path, headers=api.headers("viewer"), json=body)
    assert response.status_code == 403


def test_origin_host_and_response_boundaries(api: API) -> None:
    cross_origin = api.client.post(
        "/api/demo",
        headers={**api.headers("owner"), "Origin": "https://untrusted.example"},
    )
    assert cross_origin.status_code == 403
    cross_host = api.client.get(
        "/api/overview", headers={**api.headers("viewer"), "Host": "untrusted.example"}
    )
    assert cross_host.status_code == 400
    same_origin = api.client.get(
        "/api/overview", headers={**api.headers("viewer"), "Origin": "http://testserver"}
    )
    assert same_origin.status_code == 200
    assert same_origin.headers["Cache-Control"] == "no-store"
    assert same_origin.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in same_origin.headers["Content-Security-Policy"]
    oversized = api.client.post(
        "/api/demo", headers={**api.headers("owner"), "Content-Length": "2000001"}
    )
    assert oversized.status_code == 413
    assert api.get("/api/overview")["services"] == []


@pytest.mark.parametrize("origin", ["http://[", "https://testserver", "null"])
def test_malformed_or_different_scheme_origin_is_rejected(api: API, origin: str) -> None:
    response = api.client.get("/api/overview", headers={**api.headers("viewer"), "Origin": origin})
    assert response.status_code == 403


def test_registry_ingestion_approved_adapter_and_reprocessing_are_separate(api: API) -> None:
    service = {
        "id": "custom",
        "name": "Registered target",
        "instance_id": "custom-v1",
        "owner": "owner",
    }
    denied = api.client.post("/api/services", headers=api.headers("operator"), json=service)
    assert denied.status_code == 403
    api.post("/api/services", "owner", service)
    api.post(
        "/api/services/custom/check",
        "owner",
        {"kind": "http", "endpoint": "http://127.0.0.1/health", "body_contains": "ready"},
    )
    api.post(
        "/api/capabilities",
        "owner",
        {
            "id": "custom-restore",
            "service_id": "custom",
            "name": "Fixed restore",
            "kind": "http.request",
            "endpoint": "http://127.0.0.1/restore",
            "method": "POST",
            "body": {"restore": True},
        },
    )
    api.post(
        "/api/sources",
        "owner",
        {"id": "custom-log", "service_id": "custom", "name": "Logs", "kind": "push"},
    )
    payload = json.dumps({"format": "example-v1", "result": "failed", "level": "error"})
    ingestion = {"events": [{"external_id": "event-1", "payload": payload}]}
    original = api.post("/api/sources/custom-log/ingest", body=ingestion)
    resent = api.post("/api/sources/custom-log/ingest", body=ingestion)
    assert original["event_ids"] == resent["event_ids"]
    before = api.get("/api/overview")
    assert before["coverage"][0]["unknown_count"] == 1
    case = next(item for item in before["cases"] if item["kind"] == "interpretation")
    detail = api.get(f"/api/cases/{case['id']}")
    assert detail["evidence"][0]["interpretation"]["parse_status"] == "UNKNOWN"
    definition = api.post(
        "/api/adapters",
        "owner",
        {
            "id": "custom-v1",
            "name": "Example format",
            "source_id": "custom-log",
            "target_instance_id": "custom-v1",
            "target_version": 1,
            "version": 1,
            "fields": {"outcome": "result", "severity": "level"},
            "conditions": {"format": "example-v1"},
            "outcome_map": {"failed": "FAILURE"},
            "severity_map": {"error": "ERROR"},
        },
    )
    self_approval = api.client.post(
        "/api/adapters/custom-v1/approve",
        headers=api.headers("owner"),
        json={"digest": definition["digest"]},
    )
    assert self_approval.status_code == 403
    api.post("/api/adapters/custom-v1/approve", "reviewer", {"digest": definition["digest"]})
    assert api.post("/api/adapters/custom-v1/reprocess")["reprocessed"] == 1
    after = api.get("/api/overview")
    assert after["coverage"][0]["unknown_count"] == 0
    assert len(after["cases"]) == len(before["cases"])
    assert after["plans"] == [] and after["executions"] == []
    interpreted = api.get(f"/api/cases/{case['id']}")["evidence"][0]["interpretation"]
    assert interpreted["parse_status"] == "KNOWN" and interpreted["outcome"] == "FAILURE"
    assert interpreted["adapter_id"] == "custom-v1"
    api.post("/api/adapters/custom-v1/revoke", "reviewer")
    assert (
        api.get(f"/api/cases/{case['id']}")["evidence"][0]["interpretation"]["parse_status"]
        == "UNKNOWN"
    )
    assert api.get("/api/overview")["executions"] == []


@pytest.mark.parametrize("change", ["generation", "target", "capability", "expiry"])
def test_stale_approval_cannot_execute(
    api: API, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, plan = demo_plan(api)
    approve(api, plan)
    if change == "generation":
        api.post("/api/authorization/revoke-all", "owner")
    elif change == "target":
        service = api.get("/api/overview")["services"][0]
        api.post("/api/services", "owner", {**service, "instance_id": "replacement", "version": 2})
    elif change == "capability":
        api.post(
            "/api/capabilities",
            "owner",
            {
                "id": "demo-restore",
                "service_id": "demo-checkout",
                "name": "Changed registered operation",
                "kind": "demo.restore",
                "version": 2,
            },
        )
    else:
        future = time.time() + 4000
        monkeypatch.setattr("opsyne.control.repository.time.time", lambda: future)
    response = api.client.post(f"/api/plans/{plan['id']}/execute", headers=api.headers("operator"))
    assert response.status_code == 403
    assert api.get("/api/overview")["executions"] == []
    assert api.post("/api/services/demo-checkout/verify")["evidence"]["change_count"] == 0


def test_http_unknown_remains_locked_and_health_pass_does_not_resolve(api: API) -> None:
    case, _ = demo_plan(api)
    api.post(
        "/api/services/demo-checkout/check",
        "owner",
        {"kind": "http", "endpoint": "https://registered.internal/health"},
    )
    api.post(
        "/api/capabilities",
        "owner",
        {
            "id": "http-restore",
            "service_id": "demo-checkout",
            "name": "Registered HTTP operation",
            "kind": "http.request",
            "endpoint": "https://registered.internal/restore",
            "body": {"fixed": True},
        },
    )
    healthy = False
    writes = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "GET":
            return httpx.Response(200 if healthy else 503)
        writes += 1
        assert request.url.path == "/restore"
        assert json.loads(request.content) == {"fixed": True}
        raise httpx.ReadTimeout("Simulated lost response", request=request)

    api.runtime.http = HttpConnector(transport=httpx.MockTransport(transport))
    plan = api.post(
        f"/api/cases/{case['id']}/plans",
        body={"capability_id": "http-restore", "reason": "Approved fixed operation"},
    )
    approve(api, plan)
    execution = api.post(f"/api/plans/{plan['id']}/execute")
    assert execution["status"] == "UNKNOWN"
    duplicate = api.post(f"/api/plans/{plan['id']}/execute")
    assert duplicate["id"] == execution["id"] and writes == 1
    changed_check = api.client.post(
        "/api/services/demo-checkout/check", headers=api.headers("owner"), json={"kind": "demo"}
    )
    assert changed_check.status_code == 409
    unsafe_release = api.client.post(
        f"/api/recovery/unsent/{plan['id']}",
        headers=api.headers("owner"),
        json={"reason": "External operation history was checked, but the ledger is UNKNOWN"},
    )
    assert unsafe_release.status_code == 409
    assert api.post(f"/api/executions/{execution['id']}/reconcile")["status"] == "UNKNOWN"
    healthy = True
    assert api.post(f"/api/executions/{execution['id']}/verify")["status"] == "PASS"
    assert api.get(f"/api/cases/{case['id']}")["case"]["status"] != "RESOLVED"
    assert writes == 1


def test_unsent_reservation_recovery_requires_admin_and_recorded_evidence(api: API) -> None:
    _, plan = demo_plan(api)
    approve(api, plan)
    # Simulate termination after Control's claim commits, before Runner is invoked.
    api.runtime.control.issue_permit(plan["id"])
    holds = api.client.get("/api/recovery/holds", headers=api.headers("owner"))
    assert holds.status_code == 200 and holds.json()[0]["plan_id"] == plan["id"]
    assert api.client.get("/api/recovery/holds", headers=api.headers("viewer")).status_code == 403
    denied = api.client.post(
        f"/api/recovery/unsent/{plan['id']}",
        headers=api.headers("operator"),
        json={
            "reason": "Reviewed both the external operation history and the local execution ledger"
        },
    )
    assert denied.status_code == 403
    no_evidence = api.client.post(
        f"/api/recovery/unsent/{plan['id']}",
        headers=api.headers("owner"),
        json={"reason": "short"},
    )
    assert no_evidence.status_code == 403
    assert (
        api.post(
            f"/api/recovery/unsent/{plan['id']}",
            "owner",
            {"reason": "Verified external operation history and the local execution ledger"},
        )["released"]
        is True
    )
    overview = api.get("/api/overview")
    assert overview["plans"][0]["status"] == "REJECTED"
    assert any(item["action"] == "recovery.unsent_release" for item in overview["audit"])
    denied_reuse = api.client.post(
        f"/api/plans/{plan['id']}/execute", headers=api.headers("operator")
    )
    assert denied_reuse.status_code == 403
    assert overview["executions"] == []


def test_missing_llm_configuration_fails_task_without_blocking_observation(api: API) -> None:
    case, _ = demo_plan(api)
    task = api.post(f"/api/cases/{case['id']}/investigate")
    assert task["status"] == "PENDING"
    completed = api.runtime.run_task()
    assert completed is not None and completed.status == "FAILED"
    api.post(
        "/api/sources/demo-log/ingest",
        body={"events": [{"external_id": "event-after-task", "payload": '{"unknown":true}'}]},
    )
    overview = api.get("/api/overview")
    assert overview["coverage"][0]["pending_count"] == 0
    assert overview["coverage"][0]["unknown_count"] >= 2
    assert overview["executions"] == []
