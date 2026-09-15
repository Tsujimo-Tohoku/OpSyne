"""Private bridge endpoints independently verify the original Discord signature."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from opsyne.api.auth import AccessTokens
from opsyne.contracts.discord import DiscordEnvelope, DiscordInstallation
from opsyne.control.discord import DiscordControl
from opsyne.control.repository import Control


def register_discord(app: FastAPI, control: Control, tokens: AccessTokens, path: Path) -> None:
    bridge = DiscordControl(control)

    def installation(authorization: str | None) -> DiscordInstallation:
        try:
            config = DiscordInstallation.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(503, "Discord接続設定を確認してください") from None
        expected = "Bearer " + config.bridge_token.get_secret_value()
        if authorization is None or not hmac.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(401, "Discord bridge認証が必要です")
        return config

    @app.get("/api/discord/notifications")
    def notifications(
        cursor: str = "",
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        return bridge.feed(installation(authorization), cursor)

    @app.post("/api/discord/interactions")
    def interactions(
        envelope: DiscordEnvelope,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        config = installation(authorization)
        now = time.time()
        try:
            raw = base64.b64decode(envelope.raw_body_base64, validate=True)
            if len(raw) > 65536 or abs(now - int(envelope.signature_timestamp)) > 300:
                raise ValueError("Invalid timestamp or body size")
            VerifyKey(bytes.fromhex(config.public_key)).verify(
                envelope.signature_timestamp.encode("ascii") + raw,
                bytes.fromhex(envelope.signature_ed25519),
            )
            data = json.loads(raw)
            user = data["member"]["user"]
            channel = data["channel_id"]
            interaction = data["id"]
            if (
                data["application_id"] != config.application_id
                or data["guild_id"] != config.guild_id
                or user.get("bot", False) is not False
                or any(
                    not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value)
                    for value in (user["id"], channel, interaction)
                )
            ):
                raise ValueError("Invalid context")
            if data["type"] == 3 and data["data"]["component_type"] == 2:
                action, reference = data["data"]["custom_id"].split(":", 1)
                if action not in {"review", "confirm", "reject"} or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,80}", reference
                ):
                    raise ValueError("Invalid component")
            elif data["type"] == 2:
                command = data["data"]
                if (
                    command["name"] != "opsyne"
                    or command["type"] != 1
                    or len(command["options"]) != 1
                ):
                    raise ValueError("Invalid command")
                option = command["options"][0]
                action = option["name"]
                if (
                    set(option) != {"name", "type", "options"}
                    or option["type"] != 1
                    or action not in {"case", "plan"}
                    or len(option["options"]) != 1
                ):
                    raise ValueError("Invalid subcommand")
                argument = option["options"][0]
                if (
                    set(argument) != {"name", "type", "value"}
                    or argument["name"] != "id"
                    or argument["type"] != 3
                ):
                    raise ValueError("Invalid argument")
                reference = argument["value"]
                if not isinstance(reference, str) or not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,100}", reference
                ):
                    raise ValueError("Invalid reference")
            else:
                raise ValueError("Unsupported interaction")
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
            binascii.Error,
            BadSignatureError,
        ):
            raise HTTPException(403, "Discord署名または要求が無効です") from None
        binding = config.users.get(user["id"])
        if binding is None:
            raise HTTPException(403, "Discord利用者が未登録です")
        actor = tokens.named_actor(binding.actor)
        if actor is None or actor.role == "agent":
            raise HTTPException(403, "現在の担当者を確認できません")
        return bridge.interact(
            config,
            actor,
            user["id"],
            channel,
            interaction,
            hashlib.sha256(raw).hexdigest(),
            action,
            reference,
        )
