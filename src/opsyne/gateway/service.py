"""A scoped evidence read boundary with bounded, redacted model-facing copies."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable

from pydantic import JsonValue, ValidationError

from opsyne.contracts.cases import EvidenceGrant
from opsyne.contracts.observations import RawEvent

_MASK = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"token|password|passwd|secret|authorization|cookie|api[_-]?key|private[_-]?key|credential",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+"
)
_ASSIGNMENT = re.compile(
    r"""(?ix)(["']?(?:[\w-]*(?:token|password|passwd|secret|api[_-]?key|credential)[\w-]*)["']?\s*[:=]\s*)"""
    r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}&]+)""",
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*")
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")


class EvidenceAccessError(ValueError):
    """A grant could not be fully validated or fulfilled within its scope."""


def _mask_text(value: str) -> str:
    value = _HEADER.sub(lambda match: f"{match.group(1)}: {_MASK}", value)
    value = _ASSIGNMENT.sub(lambda match: match.group(1) + _MASK, value)
    value = _BEARER.sub(lambda match: match.group(1) + " " + _MASK, value)
    return _API_KEY.sub(_MASK, value)


def _mask_json(value: JsonValue, depth: int = 0) -> JsonValue:
    if depth > 50:
        return "[NESTED DATA OMITTED]"
    if isinstance(value, dict):
        return {
            key: _MASK if _SECRET_KEY.search(key) else _mask_json(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_json(item, depth + 1) for item in value]
    return _mask_text(value) if isinstance(value, str) else value


def _redact(payload: str) -> str:
    """Mask common credential patterns; this is not complete data classification."""
    try:
        parsed: JsonValue = json.loads(payload)
        return json.dumps(
            _mask_json(parsed), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, RecursionError):
        return _mask_text(payload)


def _serialized(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _prefix(value: str, budget: int) -> str:
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if len(_serialized(value[:middle])) - 2 <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low]


class EvidenceGateway:
    def __init__(
        self,
        fetch: Callable[[str], RawEvent | None],
        validate_task: Callable[[EvidenceGrant], None],
    ) -> None:
        self._fetch = fetch
        self._validate_task = validate_task

    def retrieve(
        self,
        grant: EvidenceGrant,
        now: float | None = None,
    ) -> list[dict[str, JsonValue]]:
        try:
            grant = EvidenceGrant.model_validate(grant.model_dump())
        except ValidationError:
            raise EvidenceAccessError("invalid evidence grant") from None
        timestamp = time.time() if now is None else now
        if not math.isfinite(timestamp) or timestamp >= grant.expires_at:
            raise EvidenceAccessError("evidence grant expired")
        if len(grant.evidence_ids) > grant.max_items:
            raise EvidenceAccessError("evidence item budget exceeded")
        if len(set(grant.evidence_ids)) != len(grant.evidence_ids):
            raise EvidenceAccessError("duplicate evidence IDs are not permitted")
        self._validate_task(grant)
        records: list[dict[str, JsonValue]] = []
        payloads: list[str] = []
        for identifier in grant.evidence_ids:
            raw = self._fetch(identifier)
            if raw is None or raw.id != identifier or raw.service_id != grant.service_id:
                raise EvidenceAccessError("evidence is unavailable or outside the granted service")
            records.append(
                {
                    "id": raw.id,
                    "service_id": raw.service_id,
                    "source_id": raw.source_id,
                    "payload": "",
                    "truncated": True,
                }
            )
            payloads.append(_redact(raw.payload))
        # Include JSON escaping and provenance metadata in the character ceiling.
        overhead = len(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
        remaining = grant.max_characters - overhead
        if remaining < 0:
            raise EvidenceAccessError("evidence metadata exceeds the character budget")
        for index, (record, payload) in enumerate(zip(records, payloads, strict=True)):
            portion = remaining // (len(records) - index)
            bounded = _prefix(payload, portion)
            record["payload"] = bounded
            record["truncated"] = len(bounded) < len(payload)
            # False costs one more character than True in serialized JSON.
            cost = len(_serialized(bounded)) - 2 + int(not record["truncated"])
            if cost > remaining:
                record["truncated"] = True
                record["payload"] = _prefix(payload, max(0, remaining - 1))
                cost = len(_serialized(record["payload"])) - 2
            remaining -= cost
        self._validate_task(grant)
        if now is None and time.time() >= grant.expires_at:
            raise EvidenceAccessError("evidence grant expired during retrieval")
        if (
            len(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
            > grant.max_characters
        ):
            raise EvidenceAccessError("evidence character budget exceeded")
        return records
