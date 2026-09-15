"""Read-only local discovery results; evidence hints are not health verdicts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from opsyne.contracts.core import Model

LocalPath = Annotated[str, Field(min_length=1, max_length=4096)]
LogSignal = Literal["http_requests", "errors", "lifecycle"]


class LogDiscoveryRequest(Model):
    roots: Annotated[list[LocalPath], Field(min_length=1, max_length=8)]

    @field_validator("roots")
    @classmethod
    def local_roots(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.strip() or "\x00" in value or value.startswith(("\\\\", "//")):
                raise ValueError("ローカルのフォルダーを指定してください")
            if "://" in value:
                raise ValueError("URL ではなくサーバー上のフォルダーを指定してください")
        return values


class LogReference(Model):
    config_path: str
    line: int


class LogCandidate(Model):
    path: str
    size_bytes: int
    modified_at: str
    format: Literal["jsonl", "text", "empty", "unreadable"]
    signals: list[LogSignal]
    sample_bytes: int
    references: list[LogReference]


class DiscoveryNotice(Model):
    path: str
    reason: str


class LogDiscoveryResult(Model):
    roots: list[str]
    status: Literal["completed", "partial"]
    candidates: list[LogCandidate]
    inspected_entries: int
    read_bytes: int
    skipped: dict[str, int]
    limits_reached: list[str]
    notices: list[DiscoveryNotice]
