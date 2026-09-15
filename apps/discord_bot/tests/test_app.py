"""Exercise the trust boundary with real local Ed25519 signatures and fake HTTP peers."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from nacl.signing import SigningKey, VerifyKey

from opsyne_discord.app import create_app
from opsyne_discord.config import Settings
from opsyne_discord.delivery import Outbox
from opsyne_discord.presentation import PlanView

NOW = 1_800_000_000
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


def settings_for(path: Path, key: SigningKey, *, bridge: bool = False) -> Settings:
    return Settings(
        application_id="101",
        public_key=key.verify_key.encode().hex(),
        guild_id="202",
        channel_ids=frozenset({"303"}),
        state_dir=path,
        ingest_token="ingest-secret-" + "x" * 32,
        bridge_url="http://127.0.0.1:9999/proposed/discord" if bridge else "",
        bridge_token="bridge-secret-" + "x" * 32 if bridge else "",
    )


def interaction(identifier: str = "404", custom_id: str = "review:request1") -> dict[str, Any]:
    return {
        "application_id": "101",
        "guild_id": "202",
        "channel_id": "303",
        "member": {"user": {"id": "505"}},
        "id": identifier,
        "type": 3,
        "data": {"custom_id": custom_id},
        "token": "sensitive-interaction-token",
    }


async def post_signed(
    client: httpx.AsyncClient,
    key: SigningKey,
    payload: dict[str, Any],
    *,
    timestamp: int = NOW,
) -> httpx.Response:
    raw = json.dumps(payload, ensure_ascii=False).encode()
    signature = key.sign(str(timestamp).encode() + raw).signature.hex()
    response = await client.post(
        "/interactions",
        content=raw,
        headers={
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": str(timestamp),
        },
    )
    assert isinstance(response, httpx.Response)
    return response


async def test_ping_and_disconnected_approval(tmp_path: Path) -> None:
    key = SigningKey.generate()
    settings = settings_for(tmp_path, key)
    async with client_for(create_app(settings, clock=lambda: NOW)) as client:
        response = await post_signed(client, key, {"application_id": "101", "type": 1})
        assert response.json() == {"type": 1}
        response = await post_signed(client, key, interaction(custom_id="confirm:anything"))
        assert response.status_code == 200
        assert "未接続" in response.json()["data"]["content"]
        assert response.json()["data"]["flags"] == 64
        assert (await client.get("/healthz")).json()["control"] == "disconnected"
    assert b"sensitive-interaction-token" not in (tmp_path / "receipts.sqlite3").read_bytes()


async def test_tampered_signature_and_expired_timestamp(tmp_path: Path) -> None:
    key = SigningKey.generate()
    async with client_for(create_app(settings_for(tmp_path, key), clock=lambda: NOW)) as client:
        assert (await client.post("/interactions", json=interaction())).status_code == 401
        assert (await post_signed(client, SigningKey.generate(), interaction())).status_code == 401
        assert (
            await post_signed(client, key, interaction(), timestamp=NOW - 301)
        ).status_code == 401
        assert (
            await post_signed(client, key, interaction(), timestamp=NOW + 301)
        ).status_code == 401
        raw = b'{"application_id":"101","type":1}'
        signature = key.sign(str(NOW).encode() + raw).signature.hex()
        response = await client.post(
            "/interactions",
            content=raw + b" ",
            headers={
                "x-signature-ed25519": signature,
                "x-signature-timestamp": str(NOW),
            },
        )
        assert response.status_code == 401


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("application_id", "999"),
        ("guild_id", "999"),
        ("channel_id", "999"),
        ("channel_id", []),
        ("member", None),
        ("member", {"user": {"id": "505", "bot": True}}),
        ("id", "not-a-number"),
    ],
)
async def test_context_mismatch_rejected(tmp_path: Path, field: str, value: object) -> None:
    key = SigningKey.generate()
    payload = interaction()
    payload[field] = value
    async with client_for(create_app(settings_for(tmp_path, key), clock=lambda: NOW)) as client:
        assert (await post_signed(client, key, payload)).status_code == 403


async def test_duplicate_interaction_survives_restart_and_rejects_reuse(tmp_path: Path) -> None:
    key = SigningKey.generate()
    settings = settings_for(tmp_path, key, bridge=True)
    seen: list[httpx.Request] = []

    def backend(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        original = base64.b64decode(body["raw_body_base64"])
        VerifyKey(bytes.fromhex(settings.public_key)).verify(
            body["signature_timestamp"].encode() + original,
            bytes.fromhex(body["signature_ed25519"]),
        )
        assert json.loads(original)["member"]["user"]["id"] == "505"
        assert request.headers["authorization"] == f"Bearer {settings.bridge_token}"
        return httpx.Response(
            200, json={"protocol": "opsyne-discord/v1", "kind": "approval_recorded"}
        )

    connection = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    payload = interaction(custom_id="confirm:bound-challenge")
    for _ in range(2):
        async with client_for(create_app(settings, client=connection, clock=lambda: NOW)) as client:
            response = await post_signed(client, key, payload)
            assert "承認を記録" in response.json()["data"]["content"]
            assert (await post_signed(client, key, payload)).json() == response.json()
    assert len(seen) == 1
    async with client_for(create_app(settings, client=connection, clock=lambda: NOW)) as client:
        changed = interaction(custom_id="confirm:different-challenge")
        assert (await post_signed(client, key, changed)).status_code == 409
    assert len(seen) == 1


async def test_lost_response_never_retried_or_claimed_as_success(tmp_path: Path) -> None:
    key = SigningKey.generate()
    count = 0

    def backend(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("SECRET FROM BACKEND", request=request)

    connection = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    settings = settings_for(tmp_path, key, bridge=True)
    async with client_for(create_app(settings, client=connection, clock=lambda: NOW)) as client:
        for _ in range(2):
            response = await post_signed(client, key, interaction(custom_id="confirm:request1"))
            assert "不明" in response.json()["data"]["content"]
            assert "SECRET" not in response.text
    assert count == 1


def plan() -> PlanView:
    return PlanView(
        "p1",
        1,
        "a" * 64,
        "instance1",
        "1",
        "demo.restore",
        "合成データのみ",
        ("demo target",),
        ("independent check passes",),
        ("target changed",),
        "2030-01-01T00:00:00+00:00",
        "request1",
    )


async def test_review_requires_backend_bound_confirmation_and_full_terms(tmp_path: Path) -> None:
    key = SigningKey.generate()
    reply: dict[str, Any] = {
        "protocol": "opsyne-discord/v1",
        "kind": "review",
        "plan": asdict(plan()),
        "confirmation_id": "user505-plan1-challenge",
    }
    connection = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply))
    )
    settings = settings_for(tmp_path, key, bridge=True)
    async with client_for(create_app(settings, client=connection, clock=lambda: NOW)) as client:
        data = (await post_signed(client, key, interaction())).json()["data"]
        assert data["components"][0]["components"][0]["custom_id"] == (
            "confirm:user505-plan1-challenge"
        )
        reply["plan"] = asdict(replace(plan(), impact="x" * 2000))
        data = (await post_signed(client, key, interaction("405"))).json()["data"]
        assert data["components"] == []
        reply.pop("confirmation_id")
        data = (await post_signed(client, key, interaction("406"))).json()["data"]
        assert "不明" in data["content"]


@pytest.mark.parametrize("kind", ["approval_recorded", "garbage"])
async def test_review_cannot_claim_approval(tmp_path: Path, kind: str) -> None:
    key = SigningKey.generate()
    connection = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"protocol": "opsyne-discord/v1", "kind": kind}
            )
        )
    )
    async with client_for(
        create_app(
            settings_for(tmp_path, key, bridge=True),
            client=connection,
            clock=lambda: NOW,
        )
    ) as client:
        assert "不明" in (await post_signed(client, key, interaction())).json()["data"]["content"]


def notification() -> dict[str, Any]:
    return {
        "event_id": "e1",
        "channel_id": "303",
        "case": {
            "case_id": "c1",
            "title": "Synthetic",
            "state": "UNKNOWN",
            "summary": "unconfirmed",
            "observed_at": "2030-01-01T00:00:00+00:00",
        },
    }


async def test_notification_ingestion_auth_dedup_and_persistence(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, SigningKey.generate())
    headers = {"Authorization": f"Bearer {settings.ingest_token}"}
    async with client_for(create_app(settings, clock=lambda: NOW)) as client:
        body = notification()
        assert (await client.post("/notifications", json=body)).status_code == 401
        assert (await client.post("/notifications", json=body, headers=headers)).status_code == 202
        assert (await client.post("/notifications", json=body, headers=headers)).status_code == 200
        body["case"]["summary"] = "changed"
        assert (await client.post("/notifications", json=body, headers=headers)).status_code == 409
        body["channel_id"] = "999"
        assert (await client.post("/notifications", json=body, headers=headers)).status_code == 403
        assert (await client.post("/notifications", json={}, headers=headers)).status_code == 422
    row = Outbox(tmp_path / "outbox.sqlite3").get("e1")
    assert row is not None and row.state == "PENDING"
    assert row.payload["allowed_mentions"] == {"parse": []}


async def test_body_limits_and_unsupported_commands(tmp_path: Path) -> None:
    key = SigningKey.generate()
    settings = settings_for(tmp_path, key)
    async with client_for(create_app(settings, clock=lambda: NOW)) as client:
        assert (await client.post("/interactions", content=b"x" * 65537)).status_code == 413
        response = await client.post(
            "/notifications",
            content=b"x" * 65537,
            headers={"Authorization": f"Bearer {settings.ingest_token}"},
        )
        assert response.status_code == 413
        response = await post_signed(client, key, interaction(custom_id="execute:arbitrary-shell"))
        assert "対応していない" in response.json()["data"]["content"]


async def test_no_product_imports_and_secret_repr(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, SigningKey.generate(), bridge=True)
    assert settings.ingest_token not in repr(settings)
    assert settings.bridge_token not in repr(settings)
    root = Path(__file__).resolve().parents[1] / "src" / "opsyne_discord"
    for path in root.glob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert "from opsyne." not in content
        assert "import opsyne." not in content


async def test_notification_database_lock_does_not_delay_discord_ping(tmp_path: Path) -> None:
    key = SigningKey.generate()
    settings = settings_for(tmp_path, key)
    app = create_app(settings, clock=lambda: NOW)
    async with client_for(app) as client:
        with sqlite3.connect(tmp_path / "outbox.sqlite3") as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            pending = asyncio.create_task(
                client.post(
                    "/notifications",
                    json=notification(),
                    headers={"Authorization": f"Bearer {settings.ingest_token}"},
                )
            )
            try:
                await asyncio.sleep(0.05)
                assert not pending.done()
                response = await asyncio.wait_for(
                    post_signed(client, key, {"application_id": "101", "type": 1}), 0.5
                )
                assert response.json() == {"type": 1}
            finally:
                blocker.rollback()
                await pending


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/bridge",
        "https://user:secret@example.com/bridge",
        "https://example.com/bridge?token=secret",
        "https://example.com/bridge#fragment",
    ],
)
async def test_bridge_destination_validation(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError):
        replace(settings_for(tmp_path, SigningKey.generate(), bridge=True), bridge_url=url)
