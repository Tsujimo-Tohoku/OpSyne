"""Bounded, deterministic grouping of unknown JSON formats, without interpreting them."""

from __future__ import annotations

import hashlib
import json
import math
from typing import cast

from pydantic import JsonValue

from opsyne.contracts.core import Service
from opsyne.contracts.observations import CommonEvent, RawEvent

MAX_PAYLOAD_CHARACTERS = 1_048_576
MAX_DEPTH = 24
MAX_NODES = 2_048
MAX_ARRAY_ITEMS = 64

_SELECTORS = frozenset(
    {
        "format",
        "formatid",
        "formatversion",
        "schema",
        "schemaid",
        "schemaversion",
        "eventtype",
        "recordtype",
        "logtype",
    }
)


class _Unsupported(ValueError):
    pass


def _serialize(value: JsonValue) -> str:
    # Escaping Unicode also handles escaped lone surrogates without a UTF-8 encoder failure.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(_serialize(value).encode("ascii")).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError("nonstandard JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _check_depth(payload: str) -> None:
    """Bound nesting before invoking the recursive standard-library JSON parser."""
    depth = 0
    quoted = False
    escaped = False
    for character in payload:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise _Unsupported("complexity_limit")
        elif character in "]}":
            depth -= 1


def _key_name(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def _selector(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    name = _key_name(path[-1])
    if name in _SELECTORS:
        return True
    return (
        len(path) >= 2
        and _key_name(path[-2]) in {"schema", "format", "event", "record"}
        and name in {"type", "version"}
    )


def _shape(value: JsonValue, path: tuple[str, ...], budget: list[int]) -> JsonValue:
    budget[0] -= 1
    if budget[0] < 0:
        raise _Unsupported("complexity_limit")
    if isinstance(value, dict):
        fields: list[JsonValue] = []
        for key in sorted(value):
            fields.append([key, _shape(value[key], (*path, key), budget)])
        return ["object", fields]
    if isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            raise _Unsupported("complexity_limit")
        # All members within the limit are visited, so order never chooses the sample.
        members = {_serialize(_shape(item, (*path, "[]"), budget)) for item in value}
        array_shapes: list[JsonValue] = list(sorted(members))
        return ["array", array_shapes]
    if value is None:
        kind = "null"
    elif isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, str):
        kind = "string"
    else:
        kind = "number"
    return [kind, _digest(value)] if _selector(path) else kind


def _inspect(raw: RawEvent) -> tuple[str, JsonValue]:
    if raw.encoding_error:
        return "invalid_utf8", None
    if len(raw.payload) > MAX_PAYLOAD_CHARACTERS:
        return "payload_limit", None
    try:
        _check_depth(raw.payload)
        document = cast(
            JsonValue,
            json.loads(
                raw.payload,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
                object_pairs_hook=_unique_object,
            ),
        )
        if not isinstance(document, dict):
            return "json_object_required", None
        return "json_object", _shape(document, (), [MAX_NODES])
    except _Unsupported as error:
        return str(error), None
    except (ValueError, RecursionError):
        return "invalid_json", None


def supported_json(raw: RawEvent) -> bool:
    """Whether a bounded, unambiguous JSON object can receive a declarative proposal."""
    reason, _ = _inspect(raw)
    return reason == "json_object"


def format_fingerprint(raw: RawEvent, event: CommonEvent, service: Service) -> str:
    """Return a scoped SHA-256 grouping key, never a verdict or an adapter authorization.

    Ordinary scalar values are ignored. Explicit format/schema/event-type selectors
    distinguish formats using hashes of their values. Unknown semantic codes otherwise
    share the same format group; samples and human approval must resolve their meaning.
    Unsupported input groups by a coarse reason, without hashing its arbitrary contents.
    """
    if (
        raw.service_id != service.id
        or event.service_id != raw.service_id
        or event.source_id != raw.source_id
        or event.raw_ref != raw.id
    ):
        raise ValueError("format evidence must match the registered source and service")
    reason, shape = _inspect(raw)
    signature: dict[str, JsonValue] = {
        "fingerprint_version": 1,
        "source_id": raw.source_id,
        "service_id": service.id,
        "instance_id": service.instance_id,
        "service_version": service.version,
        "reason": reason,
        "shape": shape,
    }
    if reason == "json_object":
        signature["adapter_id"] = event.adapter_id
        signature["adapter_version"] = event.adapter_version
        unknown_fields: list[JsonValue] = list(sorted(set(event.unknown_fields)))
        signature["unknown_fields"] = unknown_fields
    return _digest(signature)
