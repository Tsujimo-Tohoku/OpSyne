"""Service-scoped pagination, counts, attribution and unchanged authorization."""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from opsyne.api.app import create_app
from opsyne.contracts.core import Actor, Service
from opsyne.contracts.execution import Execution
from opsyne.contracts.observations import Source
from opsyne.runtime import Runtime


def headers(path: Path, actor: str = "viewer") -> dict[str, str]:
    tokens = json.loads((path / "tokens.json").read_text(encoding="utf-8"))
    return {
        "Authorization": "Bearer " + next(row["token"] for row in tokens if row["actor"] == actor)
    }


def prepare(path: Path) -> tuple[Runtime, TestClient]:
    runtime = Runtime(path)
    app = create_app(runtime=runtime, background=False)
    for name in ("b", "a"):
        runtime.control.register_service(
            Service(id=name, name=name, instance_id=name, owner="owner"), "owner"
        )
        runtime.collector.register_source(Source(id=name, service_id=name, name=name))
        runtime.control.bind_source_scope(name, name)
    # B is inserted first, then buried below A's 600 records in global views.
    with runtime.control.db.connection() as db:
        for service, count in (("b", 7), ("a", 600)):
            for index in range(count):
                for kind in ("case", "plan", "adapter"):
                    identifier = f"{service}-{kind}-{index:04}"
                    body: dict[str, Any] = {
                        "id": identifier,
                        "service_id": service,
                        "source_id": service,
                        "status": "OPEN" if kind == "case" else "DRAFT",
                        "proposer": "owner",
                    }
                    runtime.control._put(db, kind, identifier, body)
                    action = "case.evidence" if kind == "case" else f"{kind}.propose"
                    runtime.control._audit(db, "owner", action, identifier, "synthetic")
    return runtime, TestClient(app)


def test_service_counts_and_all_pages_are_independent_of_global_limits(tmp_path: Path) -> None:
    runtime, client = prepare(tmp_path)
    with client:
        auth = headers(tmp_path)
        summary = client.get("/api/services/b/overview", headers=auth).json()
        assert summary["unresolved_incidents"] == 7
        assert summary["pending_plans"] == summary["pending_adapters"] == 7
        assert summary["reviewable_by_actor"] == {"plan": 0, "adapter": 0}
        assert summary["coverage"][0]["status"] == "missing"
        for collection, count in (("cases", 7), ("approvals", 14), ("history", 22)):
            url = f"/api/services/b/items/{collection}"
            params: dict[str, Any] = {"limit": 3}
            seen: list[str] = []
            while True:
                response = client.get(url, headers=auth, params=params)
                assert response.status_code == 200, response.text
                page = response.json()
                assert page["total"] == count
                assert page["collection_state"] == "partial"
                for item in page["items"]:
                    obj = item.get("definition", item)
                    assert obj["service_id"] == "b"
                seen.extend(str(item["id"]) for item in page["items"])
                if not page["has_more"]:
                    assert page["next_cursor"] is None
                    break
                params["cursor"] = page["next_cursor"]
            assert len(seen) == len(set(seen)) == count
        assert (
            client.get("/api/services/b/items/executions", headers=auth).json()["collection_state"]
            == "empty"
        )
        assert client.get("/api/services/missing/overview", headers=auth).status_code == 404
        assert client.get("/api/services/missing/items/cases", headers=auth).status_code == 404
        assert client.get("/api/services/b/overview").status_code == 401
        assert client.get("/api/services/b/items/cases?limit=201", headers=auth).status_code == 422
        reviewer = client.get(
            "/api/services/b/overview", headers=headers(tmp_path, "reviewer")
        ).json()
        assert reviewer["reviewable_by_actor"] == {"plan": 7, "adapter": 7}
        owner = client.get("/api/services/b/overview", headers=headers(tmp_path, "owner")).json()
        assert owner["reviewable_by_actor"] == {"plan": 0, "adapter": 0}
        # Read APIs have not approved anything.
        assert all(item["status"] == "DRAFT" for item in runtime.control.objects("plan"))


def test_cursor_rejects_changes_scope_reuse_and_tampering(tmp_path: Path) -> None:
    runtime, client = prepare(tmp_path)
    with client:
        auth = headers(tmp_path)
        url = "/api/services/b/items/cases"
        cursor = client.get(url, headers=auth, params={"limit": 2}).json()["next_cursor"]
        for target, actor, limit, value in (
            ("/api/services/a/items/cases", "viewer", 2, cursor),
            (url, "reviewer", 2, cursor),
            (url, "viewer", 3, cursor),
            (url, "viewer", 2, cursor + "x"),
            (url, "viewer", 2, "e30=.署名"),
        ):
            assert (
                client.get(
                    target,
                    headers=headers(tmp_path, actor),
                    params={"limit": limit, "cursor": value},
                ).status_code
                == 400
            )
        with runtime.control.db.connection() as db:
            item = runtime.control._get(db, "case", "b-case-0000")
            item["status"] = "RESOLVED"
            runtime.control._put(db, "case", item["id"], item)
        assert (
            client.get(url, headers=auth, params={"limit": 2, "cursor": cursor}).status_code == 409
        )
        assert (
            client.get("/api/services/b/overview", headers=auth).json()["unresolved_incidents"] == 6
        )


def test_execution_pages_keep_unknown_and_unverified_results(tmp_path: Path) -> None:
    runtime, client = prepare(tmp_path)
    with runtime.runner._database.connection() as db:
        for service, count in (("b", 3), ("a", 501)):
            for index in range(count):
                execution = Execution(
                    id=f"{service}-execution-{index}",
                    plan_id=f"{service}-plan-{index}",
                    service_id=service,
                    status="UNKNOWN" if service == "b" else "SUCCEEDED",
                    created_at=0,
                    updated_at=0,
                )
                db.execute(
                    "INSERT INTO execution_ledger VALUES (?,?,?,?,?)",
                    (
                        execution.id,
                        execution.plan_id,
                        service,
                        execution.status,
                        execution.model_dump_json(),
                    ),
                )
    with client:
        auth = headers(tmp_path)
        url = "/api/services/b/items/executions"
        first = client.get(url, headers=auth, params={"limit": 2}).json()
        assert first["total"] == 3 and first["has_more"]
        # A separate service changing does not invalidate B's continuation.
        with runtime.runner._database.connection() as db:
            db.execute("DELETE FROM execution_ledger WHERE service_id='a'")
        last = client.get(url, headers=auth, params={"limit": 2, "cursor": first["next_cursor"]})
        assert last.status_code == 200
        items = first["items"] + last.json()["items"]
        assert len({item["id"] for item in items}) == 3
        assert all(item["status"] == "UNKNOWN" and item["verification"] is None for item in items)


def test_unknown_legacy_and_global_audit_are_not_assigned_by_prose(tmp_path: Path) -> None:
    runtime, client = prepare(tmp_path)
    with runtime.control.db.connection() as db:
        db.execute(
            "INSERT INTO audit(at,actor,action,object_id,detail) "
            "VALUES(0,'old','plan.propose','b-plan-0000','b')"
        )
    runtime.control.audit("operator", "unrecognized", "b", "service_id=b")
    runtime.control.audit("owner", "authorization.revoke_all", "system")
    with client:
        auth = headers(tmp_path)
        unknown = client.get("/api/history?scope=unknown", headers=auth).json()
        assert unknown["total"] == 2
        assert all(item["service_id"] is None for item in unknown["items"])
        assert client.get("/api/history?scope=global", headers=auth).json()["total"] == 1
        assert (
            client.get("/api/services/b/overview", headers=auth).json()["history_unknown_count"]
            == 2
        )
    # Attribution is durable across restart; old rows remain unknown.
    reopened = Runtime(tmp_path)
    assert sum(row["scope"] == "unknown" for row in reopened.control.scoped_history()) == 2


def test_service_history_traces_approval_execution_and_verification(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    app = create_app(runtime=runtime, background=False)
    owner = Actor(actor="owner", role="admin")
    reviewer = Actor(actor="reviewer", role="approver")
    runtime.seed_demo(owner)
    case = next(
        item for item in runtime.control.objects("case") if item["kind"] == "operation_failure"
    )
    plan = runtime.control.create_plan(case["id"], "demo-restore", "復旧する", "operator")
    runtime.control.approve(plan["id"], plan["digest"], reviewer)
    execution = runtime.execute(plan["id"], owner)
    runtime.verify(execution.id, owner)
    with TestClient(app) as client:
        result = client.get(
            "/api/services/demo-checkout/items/history?limit=200", headers=headers(tmp_path)
        ).json()
        events = {row["action"]: row for row in result["items"]}
        assert events["plan.approve"]["actor"] == "reviewer"
        assert events["execution.request"]["current_object"]["status"] == "SUCCEEDED"
        assert events["execution.verify"]["detail"] == "PASS"
        assert events["execution.verify"]["related_plan"]["proposer"] == "operator"
        assert (
            events["execution.verify"]["related_plan"]["target_instance_id"] == "demo-checkout-v1"
        )
        executions = client.get(
            "/api/services/demo-checkout/items/executions", headers=headers(tmp_path)
        ).json()
        assert executions["items"][0]["verification"]["status"] == "PASS"
