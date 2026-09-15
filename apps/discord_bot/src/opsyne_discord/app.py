"""Signed Discord ingress and authenticated notification ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Self

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opsyne_discord.bridge import REFERENCE, forward, message
from opsyne_discord.config import SNOWFLAKE, Settings
from opsyne_discord.delivery import Outbox
from opsyne_discord.presentation import CaseView, PlanView, render_case, render_plan
from opsyne_discord.receipts import Receipts

BODY_LIMIT = 65536


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=200)
    channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    case: CaseView | None = None
    plan: PlanView | None = None

    @model_validator(mode="after")
    def one_view(self) -> Self:
        if (self.case is None) == (self.plan is None):
            raise ValueError("Supply exactly one case or plan")
        return self


async def bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > BODY_LIMIT:
            raise HTTPException(413, "Request too large")
    return bytes(body)


def action_for(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Missing interaction data")
    if payload.get("type") == 3:
        custom_id = data.get("custom_id", "")
        if not isinstance(custom_id, str):
            raise ValueError("Invalid component")
        action, _, reference = custom_id.partition(":")
        if action not in {"review", "reject", "confirm"} or not REFERENCE.fullmatch(reference):
            raise ValueError("Unsupported component")
        return action
    if payload.get("type") == 2 and data.get("name") == "opsyne":
        options = data.get("options")
        if not isinstance(options, list) or len(options) != 1:
            raise ValueError("Unsupported command")
        option = options[0]
        if (
            not isinstance(option, dict)
            or not isinstance(option.get("name"), str)
            or option["name"] not in {"case", "plan"}
        ):
            raise ValueError("Unsupported command")
        return str(option["name"])
    raise ValueError("Unsupported interaction")


def create_app(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    verify_key = VerifyKey(bytes.fromhex(settings.public_key))
    receipts = Receipts(settings.state_dir / "receipts.sqlite3")
    outbox = Outbox(settings.state_dir / "outbox.sqlite3")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if client is not None:
            app.state.bridge_client = client
            yield
        else:
            async with httpx.AsyncClient(trust_env=False) as owned_client:
                app.state.bridge_client = owned_client
                yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "control": "configured" if settings.bridge_url else "disconnected"}

    @app.post("/interactions")
    async def interactions(request: Request) -> JSONResponse:
        raw = await bounded_body(request)
        signature = request.headers.get("x-signature-ed25519", "")
        timestamp = request.headers.get("x-signature-timestamp", "")
        try:
            if not re.fullmatch(r"[0-9]{1,12}", timestamp):
                raise ValueError("Invalid timestamp")
            if abs(clock() - int(timestamp)) > 300:
                raise ValueError("Expired signature")
            verify_key.verify(timestamp.encode("ascii") + raw, bytes.fromhex(signature))
        except (ValueError, BadSignatureError):
            raise HTTPException(401, "Invalid Discord signature") from None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if (
            not isinstance(payload, dict)
            or payload.get("application_id") != settings.application_id
        ):
            raise HTTPException(403, "Unexpected application")
        if payload.get("type") == 1:
            return JSONResponse({"type": 1})
        member = payload.get("member")
        user = member.get("user") if isinstance(member, dict) else None
        interaction_id = payload.get("id")
        if (
            payload.get("guild_id") != settings.guild_id
            or not isinstance(payload.get("channel_id"), str)
            or payload["channel_id"] not in settings.channel_ids
            or not isinstance(user, dict)
            or not isinstance(user.get("id"), str)
            or not SNOWFLAKE.fullmatch(user["id"])
            or user.get("bot", False) is not False
            or not isinstance(interaction_id, str)
            or not SNOWFLAKE.fullmatch(interaction_id)
        ):
            raise HTTPException(403, "Unregistered interaction context")
        try:
            action = action_for(payload)
        except ValueError:
            return JSONResponse(
                message("対応していない操作です。登録されたコマンドを使ってください。")
            )
        try:
            first, previous = await asyncio.to_thread(
                receipts.begin, interaction_id, hashlib.sha256(raw).hexdigest()
            )
            if not first:
                return JSONResponse(
                    previous
                    or message(
                        "この操作は受付済みですが結果は未確認です。案件の最新状態を確認してください。"
                    )
                )
            response = await forward(
                settings, request.app.state.bridge_client, raw, signature, timestamp, action
            )
            await asyncio.to_thread(receipts.finish, interaction_id, response)
            return JSONResponse(response)
        except ValueError:
            raise HTTPException(409, "Interaction ID conflict") from None
        except sqlite3.Error:
            return JSONResponse(message("受付記録を確認できません。承認の成立は未確認です。"))

    @app.post("/notifications")
    async def notifications(request: Request) -> JSONResponse:
        expected = f"Bearer {settings.ingest_token}".encode()
        supplied = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(expected, supplied):
            raise HTTPException(401, "Invalid notification credential")
        raw = await bounded_body(request)
        try:
            notification = Notification.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422, "Invalid notification") from None
        if notification.channel_id not in settings.channel_ids:
            raise HTTPException(403, "Channel not registered")
        if notification.case is not None:
            rendered = render_case(notification.case)
        else:
            assert notification.plan is not None
            rendered = render_plan(notification.plan)
        try:
            inserted = await asyncio.to_thread(
                outbox.enqueue, notification.event_id, notification.channel_id, rendered, clock()
            )
            record = await asyncio.to_thread(outbox.get, notification.event_id)
        except ValueError:
            raise HTTPException(409, "Event ID conflict") from None
        except sqlite3.Error:
            raise HTTPException(503, "Notification store unavailable") from None
        if record is None:
            raise HTTPException(503, "Notification state unavailable")
        return JSONResponse(
            {"status": record.state, "event_id": notification.event_id},
            status_code=202 if inserted else 200,
        )

    return app
