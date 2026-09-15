"""Explicit single-installation Discord trust and disclosure configuration."""

from typing import Annotated, Literal

from pydantic import Field, SecretStr

from opsyne.contracts.core import Identifier, Model

Snowflake = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


class DiscordRoute(Model):
    channel_id: Snowflake
    # Includes capability parameters and check configuration: opt in deliberately.
    disclose_plan_details: bool = False


class DiscordBinding(Model):
    actor: Identifier
    service_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class DiscordInstallation(Model):
    application_id: Snowflake
    public_key: Annotated[str, Field(pattern=r"^[a-fA-F0-9]{64}$")]
    guild_id: Snowflake
    bridge_token: SecretStr = Field(min_length=32)
    routes: dict[Identifier, DiscordRoute]
    users: dict[Snowflake, DiscordBinding]


class DiscordEnvelope(Model):
    protocol: Literal["opsyne-discord/v1"]
    raw_body_base64: Annotated[str, Field(max_length=87384)]
    signature_ed25519: Annotated[str, Field(pattern=r"^[a-fA-F0-9]{128}$")]
    signature_timestamp: Annotated[str, Field(pattern=r"^[0-9]{1,12}$")]
