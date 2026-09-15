"""Pure interpretation of JSON using externally approved, bound definitions."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from opsyne.contracts.observations import AdapterDefinition, CommonEvent, ParseStatus, RawEvent

_MISSING = object()


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant: {value}")


def _json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON number exceeds finite range")
    return result


def _lookup(document: object, path: str) -> object:
    current = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except OverflowError:
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _map_key(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return str(value)
    return None


def normalize(
    raw: RawEvent,
    adapters: Sequence[AdapterDefinition],
    *,
    target_instance_id: str | None = None,
    target_version: int | None = None,
) -> CommonEvent:
    """Normalize with only the currently approved definitions supplied by Control.

    Target identity must be supplied by trusted registration, never by a log body.
    No matching binding or multiple matching definitions leave the event unknown.
    """
    base: dict[str, Any] = {
        "id": raw.id,
        "raw_ref": raw.id,
        "source_id": raw.source_id,
        "service_id": raw.service_id,
        "received_at": raw.received_at,
    }
    if raw.encoding_error:
        return CommonEvent(**base, parse_status="INVALID", reasons=["invalid UTF-8"])
    try:
        document = json.loads(
            raw.payload,
            parse_constant=_reject_nonfinite,
            parse_float=_json_float,
        )
    except (ValueError, RecursionError):
        return CommonEvent(**base, parse_status="INVALID", reasons=["invalid JSON"])
    if not isinstance(document, dict):
        return CommonEvent(**base, parse_status="UNKNOWN", reasons=["JSON object required"])
    candidates = [
        adapter
        for adapter in adapters
        if adapter.source_id == raw.source_id
        and adapter.target_instance_id == target_instance_id
        and adapter.target_version == target_version
        and all(
            (actual := _lookup(document, path)) is not _MISSING
            and type(actual) is type(expected)
            and actual == expected
            for path, expected in adapter.conditions.items()
        )
    ]
    if len(candidates) != 1:
        reason = "no matching approved adapter" if not candidates else "ambiguous adapters"
        return CommonEvent(**base, parse_status="UNKNOWN", reasons=[reason])
    adapter = candidates[0]
    result: dict[str, Any] = {
        **base,
        "adapter_id": adapter.id,
        "adapter_version": adapter.version,
    }
    unknown: list[str] = []
    understood = 0
    for field, path in adapter.fields.items():
        value = _lookup(document, path)
        if field == "timestamp":
            resolved: object = _timestamp(value)
        elif field in ("outcome", "severity"):
            key = _map_key(value)
            mapping = adapter.outcome_map if field == "outcome" else adapter.severity_map
            resolved = mapping.get(key) if key is not None else None
        else:
            resolved = value if isinstance(value, str) and value else None
        if resolved is None or resolved == "UNKNOWN":
            unknown.append(field)
        else:
            result[field] = resolved
            understood += 1
    # Missing semantic mappings remain visible even when the adapter omits them.
    unknown.extend(field for field in ("outcome", "severity") if field not in adapter.fields)
    status: ParseStatus = "KNOWN" if not unknown else "PARTIAL" if understood else "UNKNOWN"
    return CommonEvent(
        **result,
        parse_status=status,
        unknown_fields=unknown,
        reasons=["missing or unmapped values"] if unknown else [],
    )
