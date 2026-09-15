"""Pull committed case notifications; checkpoint only after durable local enqueue."""

from __future__ import annotations

import hashlib
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from opsyne_discord.config import Settings
from opsyne_discord.delivery import Outbox
from opsyne_discord.presentation import CaseView, render_case


class FeedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=200)
    channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    case: CaseView


class FeedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["opsyne-discord/v1"]
    events: list[FeedEvent] = Field(max_length=100)
    cursor: str = Field(min_length=1, max_length=200)


def source_id(settings: Settings) -> str:
    return hashlib.sha256(settings.feed_url.encode()).hexdigest()


def sync_once(settings: Settings, outbox: Outbox, client: httpx.Client) -> int:
    if not settings.feed_url:
        raise ValueError("DISCORD_CONTROL_FEED_URL is required")
    source = source_id(settings)
    response = client.get(
        settings.feed_url,
        params={"cursor": outbox.cursor(source)},
        headers={"Authorization": "Bearer " + settings.bridge_token},
        timeout=10.0,
        follow_redirects=False,
    )
    response.raise_for_status()
    page = FeedPage.model_validate_json(response.content)
    if any(event.channel_id not in settings.channel_ids for event in page.events):
        raise ValueError("Feed contains a channel outside the local allowlist")
    for event in page.events:
        outbox.enqueue(event.event_id, event.channel_id, render_case(event.case), time.time())
    # A crash before checkpoint replays stable IDs; Outbox rejects content conflicts.
    outbox.save_cursor(source, page.cursor)
    return len(page.events)
