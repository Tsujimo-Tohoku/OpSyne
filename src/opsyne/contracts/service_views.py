"""Read-only service navigation contracts; these never grant authorization."""

from typing import Any, Literal

from pydantic import Field

from opsyne.contracts.core import Model


class ServicePage(Model):
    items: list[dict[str, Any]]
    total: int = Field(ge=0)
    has_more: bool
    next_cursor: str | None
    collection_state: Literal["empty", "complete", "partial"]
    observed_at: float


class ServiceSummary(Model):
    service: dict[str, Any]
    unresolved_incidents: int
    pending_plans: int
    pending_adapters: int
    reviewable_by_actor: dict[str, int]
    coverage: list[dict[str, Any]]
    observed_at: float
    history_unknown_count: int
