"""Bounded Cedar Logplex decoding; retain bytes separately from the JSON projection."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from opsyne.contracts.observations import RawInput

MAX_BODY_BYTES = 2_000_000
MAX_MESSAGE_BYTES = 1_048_576
MAX_MESSAGES = 10_000
_HEADER = re.compile(
    r"^<(?P<priority>[0-9]{1,3})>1 "
    r"(?P<timestamp>[^\s]+) (?P<hostname>[^\s]+) (?P<app_name>[^\s]+) "
    r"(?P<proc_id>[^\s]+) (?P<msg_id>[^\s]+) (?P<message>[\s\S]*)$"
)


def parse_logplex(body: bytes) -> list[bytes]:
    """Validate the entire octet-counted frame before any event can be stored."""
    if not body or len(body) > MAX_BODY_BYTES:
        raise ValueError("Logplex body is empty or exceeds the size limit")
    messages: list[bytes] = []
    offset = 0
    while offset < len(body):
        separator = body.find(b" ", offset, offset + 8)
        prefix = body[offset:separator] if separator >= 0 else b""
        if not prefix or not prefix.isdigit():
            raise ValueError("invalid Logplex length prefix")
        length = int(prefix)
        start = separator + 1
        end = start + length
        if not 1 <= length <= MAX_MESSAGE_BYTES or end > len(body):
            raise ValueError("invalid or truncated Logplex message")
        messages.append(body[start:end])
        if len(messages) > MAX_MESSAGES:
            raise ValueError("Logplex message count exceeds the limit")
        offset = end
    return messages


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonstandard JSON constant")


def _check_json_depth(text: str) -> None:
    depth = 0
    quoted = False
    escaped = False
    for character in text:
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
            if depth > 32:
                raise ValueError("application JSON nesting exceeds the limit")
        elif character in "]}":
            depth -= 1


def _projection(message: bytes) -> str:
    text = message.decode("utf-8", "replace")
    document: dict[str, Any] = {"transport": "heroku_logplex_v1", "raw": text}
    header = _HEADER.fullmatch(text)
    if header is None or int(header["priority"]) > 191:
        document["decode_status"] = "unsupported_header"
    else:
        fields = header.groupdict()
        content = fields.pop("message")
        document.update(heroku=fields, message=content, decode_status="text")
        # A transport priority is not an application outcome or severity verdict.
        # Ambiguous/non-object/overlarge application JSON remains unclassified text.
        if len(content) <= 65_536:
            try:
                _check_json_depth(content)
                app = json.loads(
                    content,
                    object_pairs_hook=_unique_object,
                    parse_float=_finite_float,
                    parse_constant=_reject_constant,
                )
                if isinstance(app, dict):
                    # Reserve a separate namespace; app text cannot overwrite provenance.
                    json.dumps(app, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    document["app"] = app
                    document["decode_status"] = "json_object"
            except (ValueError, RecursionError):
                pass
    try:
        return json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except RecursionError as exc:
        raise ValueError("Logplex JSON nesting exceeds the limit") from exc


def logplex_inputs(messages: list[bytes], frame_id: str) -> list[RawInput]:
    """Make a versioned projection. Original bytes remain the identity and audit evidence."""
    return [
        RawInput(external_id=f"heroku:{frame_id}:{index}", payload=_projection(message))
        for index, message in enumerate(messages)
    ]
