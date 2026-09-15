"""Proposed v1 bridge, not an existing OpSyne API. Disabled unless explicitly configured."""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from opsyne_discord.config import Settings
from opsyne_discord.presentation import CaseView, PlanView, render_case, render_plan

REFERENCE = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def message(text: str) -> dict[str, Any]:
    return {"type": 4, "data": {"content": text, "flags": 64, "allowed_mentions": {"parse": []}}}


class BridgeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["opsyne-discord/v1"]
    kind: Literal[
        "case", "plan", "review", "approval_recorded", "rejected", "pending", "unavailable"
    ]
    case: CaseView | None = None
    plan: PlanView | None = None
    confirmation_id: str | None = None


def render_reply(reply: BridgeReply, action: str) -> dict[str, Any]:
    if reply.kind == "case" and reply.case is not None:
        payload = render_case(reply.case)
    elif reply.kind in {"plan", "review"} and reply.plan is not None:
        payload = render_plan(reply.plan)
        if reply.kind == "review":
            if (
                action != "review"
                or not reply.confirmation_id
                or not REFERENCE.fullmatch(reply.confirmation_id)
            ):
                raise ValueError("Missing user-bound confirmation reference")
            # The bridge owns the binding to user, plan digest, target and expiry.
            # Incomplete/oversized plans intentionally have no actionable components.
            if payload.get("components"):
                payload["components"] = [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 4,
                                "label": "この固定計画を承認する",
                                "custom_id": f"confirm:{reply.confirmation_id}",
                            }
                        ],
                    }
                ]
    elif reply.kind == "approval_recorded" and action == "confirm":
        return message("Controlに承認を記録しました。実行可否と復旧結果は別途確認されます。")
    elif reply.kind == "rejected":
        return message(
            "要求はControlで拒否または差し戻し処理されました。最新の案件を確認してください。"
        )
    elif reply.kind == "pending":
        return message(
            "Controlで処理中です。承認の確定は未確認です。案件の最新状態を確認してください。"
        )
    elif reply.kind == "unavailable":
        return message("Controlの状態を確認できません。承認済みとは扱いません。")
    else:
        raise ValueError("Unexpected bridge response")
    payload["flags"] = 64
    return {"type": 4, "data": payload}


async def forward(
    settings: Settings,
    client: httpx.AsyncClient,
    raw: bytes,
    signature: str,
    timestamp: str,
    action: str,
) -> dict[str, Any]:
    if not settings.bridge_url:
        return message("Control APIは未接続です。この操作による承認・実行は行っていません。")
    try:
        async with asyncio.timeout(1.8):
            response = await client.post(
                settings.bridge_url,
                headers={"Authorization": f"Bearer {settings.bridge_token}"},
                json={
                    "protocol": "opsyne-discord/v1",
                    "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                    "signature_ed25519": signature,
                    "signature_timestamp": timestamp,
                },
                timeout=1.5,
                follow_redirects=False,
            )
            response.raise_for_status()
            return render_reply(BridgeReply.model_validate_json(response.content), action)
    except (TimeoutError, httpx.HTTPError, ValueError):
        # A lost response does not prove that the backend failed to record approval.
        # Never retry this POST or expose request URLs, tokens or backend error bodies.
        return message("Controlの受付結果が不明です。再送せず、案件の最新状態を確認してください。")
