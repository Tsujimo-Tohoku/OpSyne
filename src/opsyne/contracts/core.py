"""Shared identities and canonical data; this module performs no I/O."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Service(Model):
    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=200)]
    instance_id: Identifier
    version: Annotated[int, Field(ge=1)] = 1
    owner: Annotated[str, Field(min_length=1, max_length=100)]
    criticality: Literal["low", "medium", "high", "critical"] = "medium"
    enabled: bool = True


class Actor(Model):
    actor: Identifier
    role: Literal["admin", "operator", "approver", "viewer", "agent"]


def canonical(value: JsonValue) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
