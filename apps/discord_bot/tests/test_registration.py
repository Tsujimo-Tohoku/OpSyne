import json
from pathlib import Path

import httpx

from opsyne_discord.config import Settings
from opsyne_discord.registration import register_commands


def test_registers_named_command_without_bulk_overwrite(tmp_path: Path) -> None:
    settings = Settings(
        "10", "a" * 64, "20", frozenset({"30"}), tmp_path, "i" * 32, bot_token="synthetic-token"
    )

    def receive(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://discord.com/api/v10/applications/10/guilds/20/commands"
        assert request.headers["Authorization"] == "Bot synthetic-token"
        assert json.loads(request.content)["name"] == "opsyne"
        return httpx.Response(201, json={"name": "opsyne", "id": "100"})

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        register_commands(settings, client)
