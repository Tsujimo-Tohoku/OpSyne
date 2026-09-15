"""Signed Discord decisions against the real local Control, without Discord I/O."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from opsyne.api.app import create_app
from opsyne.contracts.core import Actor
from opsyne.contracts.discord import DiscordInstallation
from opsyne.control.discord import DiscordControl
from opsyne.runtime import Runtime


@dataclass
class DiscordTest:
    client: TestClient
    runtime: Runtime
    key: SigningKey
    config: dict[str, Any]
    path: Path
    case: dict[str, Any]
    plan: dict[str, Any]

    def write_config(self) -> None:
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def send(
        self,
        action: str,
        reference: str,
        *,
        user: str = "40",
        interaction: str = "100",
        patch: dict[str, Any] | None = None,
        age: int = 0,
        key: SigningKey | None = None,
    ) -> httpx.Response:
        data: dict[str, Any] = {
            "application_id": "10",
            "guild_id": "20",
            "channel_id": "30",
            "id": interaction,
            "type": 3,
            "member": {"user": {"id": user}},
            "data": {"component_type": 2, "custom_id": action + ":" + reference},
        }
        if action in {"case", "plan"}:
            data.update(
                type=2,
                data={
                    "name": "opsyne",
                    "type": 1,
                    "options": [
                        {
                            "name": action,
                            "type": 1,
                            "options": [{"name": "id", "type": 3, "value": reference}],
                        }
                    ],
                },
            )
        data.update(patch or {})
        raw = json.dumps(data).encode()
        timestamp = str(int(time.time()) - age)
        signed = (key or self.key).sign(timestamp.encode() + raw).signature.hex()
        return self.client.post(
            "/api/discord/interactions",
            headers={
                "Authorization": "Bearer " + self.config["bridge_token"],
            },
            json={
                "protocol": "opsyne-discord/v1",
                "raw_body_base64": base64.b64encode(raw).decode(),
                "signature_ed25519": signed,
                "signature_timestamp": timestamp,
            },
        )

    def review(self) -> str:
        reply = self.send("review", self.plan["id"])
        assert reply.status_code == 200, reply.text
        assert reply.json()["kind"] == "review"
        return str(reply.json()["confirmation_id"])

    def feed(self, cursor: str = "") -> httpx.Response:
        return self.client.get(
            "/api/discord/notifications",
            params={"cursor": cursor},
            headers={
                "Authorization": "Bearer " + self.config["bridge_token"],
            },
        )


@pytest.fixture
def discord(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DiscordTest]:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "discord.json"
    monkeypatch.setenv("OPSYNE_DISCORD_CONFIG", str(path))
    app = create_app(tmp_path / "state", background=False)
    runtime = app.state.runtime
    runtime.seed_demo(Actor(actor="owner", role="admin"))
    case = next(
        item for item in runtime.control.objects("case") if item["kind"] == "operation_failure"
    )
    plan = runtime.control.create_plan(case["id"], "demo-restore", "合成データの復旧", "operator")
    key = SigningKey.generate()
    config = {
        "application_id": "10",
        "guild_id": "20",
        "public_key": key.verify_key.encode().hex(),
        "bridge_token": "t" * 40,
        "routes": {case["service_id"]: {"channel_id": "30", "disclose_plan_details": True}},
        "users": {
            "40": {"actor": "reviewer", "service_ids": [case["service_id"]]},
            "41": {"actor": "owner", "service_ids": [case["service_id"]]},
            "42": {"actor": "operator", "service_ids": [case["service_id"]]},
            "43": {"actor": "viewer", "service_ids": [case["service_id"]]},
        },
    }
    with TestClient(app) as client:
        result = DiscordTest(client, runtime, key, config, path, case, plan)
        result.write_config()
        yield result


def test_review_confirm_duplicate_and_restart(discord: DiscordTest) -> None:
    nonce = discord.review()
    assert discord.runtime.control.get("plan", discord.plan["id"])["status"] == "DRAFT"
    reply = discord.send("confirm", nonce, interaction="101")
    assert reply.status_code == 200, reply.text
    assert reply.json()["kind"] == "approval_recorded"
    assert discord.runtime.control.get("plan", discord.plan["id"])["approver"] == "reviewer"
    assert not discord.runtime.execution_list()
    # Reopen handlers/storage, preserving only durable state.
    app = create_app(discord.runtime.data_dir, background=False)
    with TestClient(app) as client:
        discord.client = client
        assert discord.send("confirm", nonce, interaction="101").json() == reply.json()
        assert discord.send("confirm", nonce, interaction="102").status_code == 403


@pytest.mark.parametrize("user", ["41", "42", "43", "99"])
def test_confirmation_bound_to_person(discord: DiscordTest, user: str) -> None:
    nonce = discord.review()
    assert discord.send("confirm", nonce, user=user, interaction="101").status_code == 403
    assert discord.runtime.control.get("plan", discord.plan["id"])["status"] == "DRAFT"


@pytest.mark.parametrize(
    "patch",
    [
        {"guild_id": "99"},
        {"channel_id": "99"},
        {"application_id": "99"},
        {"member": {"user": {"id": "40", "bot": True}}},
    ],
)
def test_wrong_context(discord: DiscordTest, patch: dict[str, Any]) -> None:
    assert discord.send("review", discord.plan["id"], patch=patch).status_code == 403


def test_signature_expiry_and_identity_authority(discord: DiscordTest) -> None:
    assert discord.send("review", discord.plan["id"], age=301).status_code == 403
    assert discord.send("review", discord.plan["id"], key=SigningKey.generate()).status_code == 403
    assert discord.send("review", discord.plan["id"], user="43").status_code == 403
    assert discord.send("review", discord.plan["id"], user="42").status_code == 403
    assert discord.client.get("/api/discord/notifications").status_code == 401


@pytest.mark.parametrize("change", ["generation", "binding", "target", "expiry", "role", "digest"])
def test_changed_authority_or_plan_denies_confirm(discord: DiscordTest, change: str) -> None:
    nonce = discord.review()
    control = discord.runtime.control
    if change == "generation":
        control.advance_generation("owner")
    elif change == "binding":
        discord.config["users"]["40"]["actor"] = "owner"
        discord.write_config()
    elif change == "role":
        path = discord.runtime.data_dir / "tokens.json"
        entries = json.loads(path.read_text())
        for item in entries:
            if item["actor"] == "reviewer":
                item["role"] = "viewer"
        path.write_text(json.dumps(entries), encoding="utf-8")
    else:
        with control.db.connection() as db:
            if change == "expiry":
                db.execute("UPDATE discord_challenges SET expires_at=0")
            elif change == "target":
                body = control._get(db, "service", discord.case["service_id"])
                body["version"] += 1
                control._put(db, "service", body["id"], body)
            else:
                body = control._get(db, "plan", discord.plan["id"])
                body["reason"] = "changed"
                control._put(db, "plan", body["id"], body)
    assert discord.send("confirm", nonce, interaction="101").status_code == 403
    assert control.get("plan", discord.plan["id"])["status"] == "DRAFT"


def test_receipt_insert_failure_rolls_back_approval(discord: DiscordTest) -> None:
    nonce = discord.review()
    control = discord.runtime.control
    with control.db.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON discord_receipts "
            "BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        discord.send("confirm", nonce, interaction="101")
    assert control.get("plan", discord.plan["id"])["status"] == "DRAFT"
    with control.db.connection() as db:
        assert (
            db.execute("SELECT consumed FROM discord_challenges WHERE id=?", (nonce,)).fetchone()[0]
            == 0
        )
        db.execute("DROP TRIGGER fail_receipt")
    assert discord.send("confirm", nonce, interaction="101").json()["kind"] == "approval_recorded"


def test_feed_durable_minimal_and_transactional(discord: DiscordTest) -> None:
    first = discord.feed()
    assert first.status_code == 200
    events = first.json()["events"]
    assert events
    assert all(event["channel_id"] == "30" for event in events)
    assert "impact" not in first.text and "合成データの復旧" not in first.text
    assert discord.feed(first.json()["cursor"]).json()["events"] == []
    control = discord.runtime.control
    with pytest.raises(RuntimeError), control.db.connection() as db:
        body = control._get(db, "case", discord.case["id"])
        body["status"] = "RESOLVED"
        control._put(db, "case", body["id"], body)
        raise RuntimeError("simulated crash")
    assert discord.feed(first.json()["cursor"]).json()["events"] == []
    assert (
        DiscordControl(control).feed(
            DiscordInstallation.model_validate(discord.config),
            "",
        )["events"]
        == events
    )
    discord.config["routes"] = {}
    discord.write_config()
    assert discord.feed().json()["events"] == []
    assert discord.feed(first.json()["cursor"]).status_code == 403


def test_plan_disclosure_is_opt_in(discord: DiscordTest) -> None:
    discord.config["routes"][discord.case["service_id"]]["disclose_plan_details"] = False
    discord.write_config()
    assert discord.send("plan", discord.plan["id"]).status_code == 403
    assert discord.send("case", discord.case["id"]).json()["case"]["case_id"] == discord.case["id"]


def test_approval_then_execution_then_independent_verification(discord: DiscordTest) -> None:
    nonce = discord.review()
    assert discord.send("confirm", nonce, interaction="101").json()["kind"] == "approval_recorded"
    view = discord.send("plan", discord.plan["id"], interaction="102").json()["plan"]
    assert view["state"] == "APPROVED" and view["approver"] == "reviewer"
    actor = Actor(actor="operator", role="operator")
    execution = discord.runtime.execute(discord.plan["id"], actor)
    assert execution.status == "SUCCEEDED"
    assert discord.runtime.control.get("case", discord.case["id"])["status"] != "RESOLVED"
    assert discord.runtime.verify(execution.id, actor).status == "PASS"
    assert discord.runtime.control.get("case", discord.case["id"])["status"] == "RESOLVED"
    assert any(event["case"]["state"] == "RESOLVED" for event in discord.feed().json()["events"])


def test_self_approval_and_extra_command_argument_denied(discord: DiscordTest) -> None:
    control = discord.runtime.control
    plan = control.create_plan(discord.case["id"], "demo-restore", "self proposal", "reviewer")
    assert discord.send("review", plan["id"]).status_code == 403
    response = discord.send(
        "case",
        discord.case["id"],
        interaction="101",
        patch={
            "data": {
                "name": "opsyne",
                "type": 1,
                "options": [
                    {
                        "name": "case",
                        "type": 1,
                        "options": [
                            {"name": "id", "type": 3, "value": discord.case["id"]},
                            {"name": "actor", "type": 3, "value": "owner"},
                        ],
                    }
                ],
            },
        },
    )
    assert response.status_code == 403


def test_same_interaction_changed_body_rejected(discord: DiscordTest) -> None:
    discord.review()
    assert discord.send("reject", discord.plan["id"]).status_code == 403
    assert discord.runtime.control.get("plan", discord.plan["id"])["status"] == "DRAFT"
