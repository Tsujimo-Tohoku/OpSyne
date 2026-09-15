"""Typed observation, interpretation and finding contracts."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

ParseStatus = Literal["KNOWN", "PARTIAL", "UNKNOWN", "INVALID"]
CoverageStatus = Literal["healthy", "stale", "missing", "disabled"]
Outcome = Literal["UNKNOWN", "SUCCESS", "FAILURE"]
Severity = Literal["UNKNOWN", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
CommonField = Literal[
    "timestamp",
    "category",
    "action",
    "outcome",
    "actor",
    "target",
    "severity",
    "message",
    "source_ip",
    "destination_ip",
]


class ObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Source(ObservationModel):
    id: str = Field(min_length=1, max_length=200)
    service_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["push", "file"] = "push"
    path: str | None = Field(default=None, max_length=4096)
    stale_after_seconds: int = Field(default=300, ge=1, le=604800)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_path(self) -> Source:
        if self.kind == "file" and not self.path:
            raise ValueError("file sources require a path")
        if self.kind == "push" and self.path is not None:
            raise ValueError("push sources cannot have a file path")
        return self


class RawInput(ObservationModel):
    external_id: str = Field(min_length=1, max_length=512)
    payload: str = Field(max_length=1_048_576)
    observed_at: float | None = None


class RawEvent(ObservationModel):
    id: str
    source_id: str
    service_id: str
    external_id: str
    payload: str
    received_at: float
    observed_at: float | None = None
    digest: str
    raw_bytes_b64: str
    encoding_error: bool = False


class Coverage(ObservationModel):
    source_id: str
    service_id: str
    status: CoverageStatus
    last_received: float | None
    cursor: int | None = None
    pending_count: int = 0
    unknown_count: int = 0
    parse_failures: int = 0
    gap_count: int = 0
    last_gap_at: float | None = None
    last_error: str | None = None


class AdapterDefinition(ObservationModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    target_instance_id: str = Field(min_length=1, max_length=200)
    target_version: int = Field(ge=1)
    version: int = Field(ge=1)
    fields: dict[CommonField, str] = Field(min_length=1, max_length=11)
    conditions: dict[str, JsonValue] = Field(default_factory=dict, max_length=30)
    outcome_map: dict[str, Outcome] = Field(default_factory=dict, max_length=100)
    severity_map: dict[str, Severity] = Field(default_factory=dict, max_length=100)

    @field_validator("fields")
    @classmethod
    def validate_field_paths(cls, value: dict[CommonField, str]) -> dict[CommonField, str]:
        for path in value.values():
            cls.check_path(path)
        return value

    @field_validator("conditions")
    @classmethod
    def validate_condition_paths(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for path in value:
            cls.check_path(path)
        return value

    @staticmethod
    def check_path(path: str) -> None:
        if len(path) > 300 or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", path):
            raise ValueError("field paths must be simple dot-separated object keys")


class CommonEvent(ObservationModel):
    id: str
    raw_ref: str
    source_id: str
    service_id: str
    received_at: float
    parse_status: ParseStatus
    adapter_id: str | None = None
    adapter_version: int | None = None
    timestamp: float | None = None
    category: str | None = None
    action: str | None = None
    outcome: Outcome = "UNKNOWN"
    severity: Severity = "UNKNOWN"
    actor: str | None = None
    target: str | None = None
    message: str | None = None
    source_ip: str | None = None
    destination_ip: str | None = None
    unknown_fields: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class Finding(ObservationModel):
    dedup_key: str
    severity: Severity
    title: str
    kind: str
    evidence_ids: list[str]
    service_id: str
    source_id: str
