"""Immutable contracts for approved execution and independent observation."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def validate_endpoint(endpoint: str) -> str:
    """Allow explicitly registered HTTP services, including private networks."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(ord(character) < 32 or character.isspace() for character in endpoint)
    ):
        raise ValueError("Endpoint must be an HTTP(S) URL without credentials or fragments")
    _ = parsed.port  # Reject malformed or out-of-range ports.
    return endpoint


class Capability(FrozenModel):
    id: str = Field(min_length=1, max_length=200)
    service_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["demo.restore", "http.request"]
    version: int = Field(default=1, ge=1)
    endpoint: str | None = Field(default=None, max_length=2048)
    method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST"
    body: dict[str, JsonValue] = Field(default_factory=dict)
    auth_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @model_validator(mode="after")
    def check_configuration(self) -> Self:
        if self.kind == "http.request":
            if self.endpoint is None:
                raise ValueError("HTTP capability requires an endpoint")
            validate_endpoint(self.endpoint)
        elif self.endpoint is not None or self.auth_env is not None or self.body:
            raise ValueError("Demo capability does not accept HTTP configuration")
        return self


class CheckConfig(FrozenModel):
    kind: Literal["demo", "http"]
    endpoint: str | None = Field(default=None, max_length=2048)
    expected_status: int = Field(default=200, ge=100, le=599)
    body_contains: str | None = Field(default=None, max_length=4096)
    auth_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @model_validator(mode="after")
    def check_configuration(self) -> Self:
        if self.kind == "http":
            if self.endpoint is None:
                raise ValueError("HTTP check requires an endpoint")
            validate_endpoint(self.endpoint)
        elif self.endpoint is not None or self.auth_env is not None:
            raise ValueError("Demo check does not accept an HTTP endpoint or credentials")
        return self


class Plan(FrozenModel):
    id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    service_id: str = Field(min_length=1)
    target_instance_id: str = Field(min_length=1)
    target_version: int = Field(ge=1)
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1)
    proposer: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=10000)
    evidence_ids: tuple[str, ...] = ()
    impact: str = Field(min_length=1, max_length=10000)
    success_condition: str = Field(min_length=1, max_length=10000)
    abort_condition: str = Field(min_length=1, max_length=10000)
    created_at: float = Field(ge=0)
    expires_at: float = Field(gt=0)
    digest: str = ""

    @model_validator(mode="after")
    def check_expiry(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("Plan must expire after its creation")
        return self


def plan_digest(plan: Plan) -> str:
    payload = json.dumps(
        plan.model_dump(mode="json", exclude={"digest"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ExecutionPermit(FrozenModel):
    id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_digest: str = Field(min_length=1)
    service_id: str = Field(min_length=1)
    target_instance_id: str = Field(min_length=1)
    target_version: int = Field(ge=1)
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1)
    expires_at: float = Field(gt=0)
    generation: int = Field(ge=0)
    signature: str = Field(min_length=1)


class OperationResult(FrozenModel):
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    detail: str = Field(max_length=2000)


class VerificationResult(FrozenModel):
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    detail: str = Field(max_length=2000)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    checked_at: float = Field(ge=0)


class Execution(FrozenModel):
    id: str
    plan_id: str
    service_id: str
    status: Literal["PENDING", "IN_FLIGHT", "SUCCEEDED", "FAILED", "UNKNOWN"]
    result: OperationResult | None = None
    created_at: float
    updated_at: float
    verification: VerificationResult | None = None
