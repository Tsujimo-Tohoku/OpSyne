"""CLI JSON is portable even when Windows defaults to a legacy output encoding."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["preview", "commands"])
def test_utf8_json_without_credentials(command: str) -> None:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("DISCORD_")
    }
    environment["PYTHONIOENCODING"] = "cp932"
    result = subprocess.run(
        [sys.executable, "-m", "opsyne_discord.cli", command],
        env=environment,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    document = json.loads(result.stdout)
    if command == "preview":
        assert document["case"]["allowed_mentions"] == {"parse": []}
        assert "デモ" in result.stdout
    else:
        assert document["name"] == "opsyne"
        assert document["default_member_permissions"] == "0"
