"""Register only OpSyne's guild command, preserving other application commands."""

from typing import Any

import httpx

from opsyne_discord.config import Settings


def command_definition() -> dict[str, Any]:
    return {
        "name": "opsyne",
        "description": "OpSyneの案件と固定計画を確認",
        "type": 1,
        "default_member_permissions": "0",
        "options": [
            {
                "type": 1,
                "name": name,
                "description": description,
                "options": [
                    {"type": 3, "name": "id", "description": "参照ID", "required": True},
                ],
            }
            for name, description in (("case", "案件の現在状態を確認"), ("plan", "固定計画を確認"))
        ],
    }


def register_commands(settings: Settings, client: httpx.Client) -> None:
    if not settings.bot_token:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    response = client.post(
        f"https://discord.com/api/v10/applications/{settings.application_id}"
        f"/guilds/{settings.guild_id}/commands",
        headers={"Authorization": "Bot " + settings.bot_token},
        json=command_definition(),
        timeout=10.0,
        follow_redirects=False,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("name") != "opsyne" or not body.get("id"):
        raise ValueError("Command registration result is unknown")
