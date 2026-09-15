"""Explicit, private adapter configuration; never reads the parent product's settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

SNOWFLAKE = re.compile(r"[0-9]{1,20}\Z")


@dataclass(frozen=True, slots=True)
class Settings:
    application_id: str
    public_key: str
    guild_id: str
    channel_ids: frozenset[str]
    state_dir: Path
    ingest_token: str = field(repr=False)
    bot_token: str = field(default="", repr=False)
    bridge_url: str = ""
    bridge_token: str = field(default="", repr=False)
    feed_url: str = ""

    def __post_init__(self) -> None:
        if (
            not all(
                SNOWFLAKE.fullmatch(value)
                for value in (self.application_id, self.guild_id, *self.channel_ids)
            )
            or not self.channel_ids
        ):
            raise ValueError("Application, guild and allowed channel IDs must be numeric")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.public_key):
            raise ValueError("DISCORD_PUBLIC_KEY must be a 32-byte hexadecimal public key")
        if len(self.ingest_token) < 32:
            raise ValueError("DISCORD_INGEST_TOKEN requires at least 32 characters")
        if bool(self.bridge_url) != bool(self.bridge_token):
            raise ValueError("Bridge URL and token must be configured together")
        if self.feed_url and not self.bridge_token:
            raise ValueError("Feed requires the bridge token")
        for url in (self.bridge_url, self.feed_url):
            if not url:
                continue
            parsed = urlsplit(url)
            local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in url)
                or not (parsed.scheme == "https" or (local and parsed.scheme == "http"))
            ):
                raise ValueError("Bridge URL must be HTTPS (HTTP allowed only for loopback tests)")
            _ = parsed.port


def from_environment() -> Settings:
    def required(name: str) -> str:
        value = os.environ.get(name, "")
        if not value:
            raise ValueError(f"Missing required setting: {name}")
        return value

    return Settings(
        application_id=required("DISCORD_APPLICATION_ID"),
        public_key=required("DISCORD_PUBLIC_KEY"),
        guild_id=required("DISCORD_GUILD_ID"),
        channel_ids=frozenset(part.strip() for part in required("DISCORD_CHANNEL_IDS").split(",")),
        state_dir=Path(os.environ.get("DISCORD_STATE_DIR", ".local/discord")),
        ingest_token=required("DISCORD_INGEST_TOKEN"),
        bot_token=os.environ.get("DISCORD_BOT_TOKEN", ""),
        bridge_url=os.environ.get("DISCORD_CONTROL_BRIDGE_URL", ""),
        bridge_token=os.environ.get("DISCORD_CONTROL_BRIDGE_TOKEN", ""),
        feed_url=os.environ.get("DISCORD_CONTROL_FEED_URL", ""),
    )
