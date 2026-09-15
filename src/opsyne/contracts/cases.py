"""Cases, finite investigation tasks, and bounded evidence grants."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from opsyne.contracts.core import Identifier, Model


class Case(Model):
    id: Identifier
    service_id: Identifier
    source_id: str
    kind: str
    title: str
    severity: str
    status: Literal[
        "OPEN", "INVESTIGATING", "AWAITING_APPROVAL", "EXECUTING", "VERIFYING", "RESOLVED"
    ] = "OPEN"
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: float
    updated_at: float


class EvidenceClaim(Model):
    text: str
    evidence_ids: list[str]


class Analysis(Model):
    summary: str
    facts: list[EvidenceClaim]
    hypotheses: list[EvidenceClaim]
    unknowns: list[str]
    recommendations: list[str]


class Task(Model):
    id: Identifier
    case_id: Identifier
    service_id: Identifier
    role: Literal["operator", "sre", "security", "adapter", "periodic"] = "operator"
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"] = "PENDING"
    created_at: float
    expires_at: float
    detail: str = ""
    attempts: int = 0
    discovery_id: str | None = None
    automatic: bool = False
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    target_instance_id: str | None = None
    target_version: int | None = Field(default=None, ge=1)


class EvidenceGrant(Model):
    task_id: Identifier
    case_id: Identifier
    service_id: Identifier
    role: Literal["operator", "sre", "security", "adapter", "periodic"]
    evidence_ids: tuple[str, ...]
    expires_at: float
    max_items: Annotated[int, Field(ge=1, le=50)] = 20
    max_characters: Annotated[int, Field(ge=256, le=40000)] = 12000
