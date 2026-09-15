"""Private feed polling preserves the checkpoint across crashes and bad responses."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from opsyne_discord.config import Settings
from opsyne_discord.delivery import Outbox
from opsyne_discord.feed import source_id, sync_once


def settings(path: Path) -> Settings:
    return Settings(
        "10",
        "a" * 64,
        "20",
        frozenset({"30"}),
        path,
        "i" * 32,
        bridge_url="http://127.0.0.1:8000/api/discord/interactions",
        bridge_token="t" * 40,
        feed_url="http://127.0.0.1:8000/api/discord/notifications",
    )


def page() -> dict[str, Any]:
    return {
        "protocol": "opsyne-discord/v1",
        "cursor": "epoch:1",
        "events": [
            {
                "event_id": "discord_1",
                "channel_id": "30",
                "case": {
                    "case_id": "case_1",
                    "title": "OpSyne 案件更新",
                    "state": "OPEN",
                    "summary": "本体で確認",
                    "observed_at": "2026-09-15T00:00:00+00:00",
                    "evidence_refs": [],
                },
            }
        ],
    }


def test_checkpoint_after_enqueue_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = settings(tmp_path)
    outbox = Outbox(tmp_path / "outbox.sqlite3")

    def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer " + config.bridge_token
        assert request.url.params["cursor"] == ""
        return httpx.Response(200, json=page())

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        original = outbox.save_cursor

        def fail(source: str, value: str) -> None:
            raise OSError("crash before checkpoint")

        monkeypatch.setattr(outbox, "save_cursor", fail)
        with pytest.raises(OSError):
            sync_once(config, outbox, client)
        assert outbox.cursor(source_id(config)) == ""
        assert outbox.get("discord_1") is not None
        monkeypatch.setattr(outbox, "save_cursor", original)
        assert sync_once(config, outbox, client) == 1
    reopened = Outbox(outbox.path)
    assert reopened.cursor(source_id(config)) == "epoch:1"
    with reopened._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["channel", "malformed", "redirect", "denied", "timeout"])
def test_bad_feed_does_not_advance_cursor(tmp_path: Path, failure: str) -> None:
    config = settings(tmp_path)
    outbox = Outbox(tmp_path / "outbox.sqlite3")

    def receive(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.invalid"})
        if failure == "denied":
            return httpx.Response(403)
        body = page()
        if failure == "channel":
            body["events"][0]["channel_id"] = "99"
        else:
            body["events"][0]["plan"] = {}
        return httpx.Response(200, json=body)

    with (
        httpx.Client(transport=httpx.MockTransport(receive)) as client,
        pytest.raises((ValueError, httpx.HTTPError)),
    ):
        sync_once(config, outbox, client)
    assert outbox.cursor(source_id(config)) == ""
    assert outbox.get("discord_1") is None


@pytest.mark.parametrize(
    "url", ["http://external.invalid/feed", "https://x.invalid/?token=x", "https://u:p@x.invalid/"]
)
def test_feed_url_restrictions(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError):
        replace(settings(tmp_path), feed_url=url)
