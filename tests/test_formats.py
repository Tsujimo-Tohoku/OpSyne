"""Format grouping must bound work and avoid one LLM request per log message."""

from __future__ import annotations

import json
import re

import pytest
from pydantic import JsonValue

from opsyne.contracts.core import Service
from opsyne.contracts.observations import CommonEvent, RawEvent
from opsyne.normalization.formats import (
    MAX_ARRAY_ITEMS,
    MAX_DEPTH,
    MAX_NODES,
    MAX_PAYLOAD_CHARACTERS,
    format_fingerprint,
    supported_json,
)


def raw_event(payload: str, **changes: object) -> RawEvent:
    values: dict[str, object] = {
        "id": "raw-1",
        "source_id": "application",
        "service_id": "checkout",
        "external_id": "external-1",
        "payload": payload,
        "received_at": 1.0,
        "digest": "payload-digest",
        "raw_bytes_b64": "",
    }
    values.update(changes)
    return RawEvent.model_validate(values)


def common(raw: RawEvent) -> CommonEvent:
    return CommonEvent(
        id=raw.id,
        raw_ref=raw.id,
        source_id=raw.source_id,
        service_id=raw.service_id,
        received_at=raw.received_at,
        parse_status="UNKNOWN",
    )


def service() -> Service:
    return Service(id="checkout", name="Checkout", instance_id="checkout-local", owner="owner")


def fingerprint(payload: JsonValue) -> str:
    raw = raw_event(json.dumps(payload))
    return format_fingerprint(raw, common(raw), service())


def test_ordinary_payload_changes_share_a_format() -> None:
    first: dict[str, JsonValue] = {
        "message": "request complete",
        "timestamp": "2026-01-01T00:00:00Z",
        "request_id": "request-a",
        "actor": "person-a",
        "source_ip": "192.0.2.1",
        "outcome": "NEW_CODE_A",
        "duration": 1,
    }
    second: dict[str, JsonValue] = {
        "duration": 4.2,
        "outcome": "NEW_CODE_B",
        "source_ip": "192.0.2.99",
        "actor": "person-b",
        "request_id": "request-b",
        "timestamp": "2026-08-01T00:00:00Z",
        "message": "request failed",
    }
    assert fingerprint(first) == fingerprint(second)


@pytest.mark.parametrize(
    "second",
    [
        {"message": "text", "new_field": True},
        {"message": {"text": "nested"}},
        {"message": None},
        {"message": 1},
        {"renamed": "text"},
    ],
)
def test_path_and_type_changes_get_distinct_groups(second: JsonValue) -> None:
    assert fingerprint({"message": "text"}) != fingerprint(second)


def test_boolean_and_number_types_differ() -> None:
    assert fingerprint({"outcome": True}) != fingerprint({"outcome": 1})


@pytest.mark.parametrize(
    "field",
    ["format", "format_id", "formatVersion", "schema", "schema_version", "event_type"],
)
def test_explicit_selectors_distinguish_same_shape(field: str) -> None:
    assert fingerprint({field: "v1", "message": "a"}) != fingerprint({field: "v2", "message": "b"})


def test_nested_selector_values_distinguish_same_shape() -> None:
    assert fingerprint({"event": {"type": "purchase"}}) != fingerprint(
        {"event": {"type": "refund"}}
    )
    assert fingerprint({"schema": {"version": 1}}) != fingerprint({"schema": {"version": 2}})


def test_source_service_and_target_version_bound_the_group() -> None:
    original = raw_event('{"message":"a"}')
    initial = format_fingerprint(original, common(original), service())
    moved = original.model_copy(update={"source_id": "other-source"})
    assert initial != format_fingerprint(moved, common(moved), service())
    moved = original.model_copy(update={"service_id": "other-service"})
    target = service().model_copy(update={"id": "other-service"})
    assert initial != format_fingerprint(moved, common(moved), target)
    for change in ({"instance_id": "new-instance"}, {"version": 2}):
        assert initial != format_fingerprint(
            original, common(original), service().model_copy(update=change)
        )


def test_unrelated_evidence_cannot_be_grouped_as_this_service() -> None:
    raw = raw_event("{}")
    with pytest.raises(ValueError, match="must match"):
        format_fingerprint(raw, common(raw), service().model_copy(update={"id": "other"}))
    with pytest.raises(ValueError, match="must match"):
        format_fingerprint(raw, common(raw).model_copy(update={"raw_ref": "other"}), service())


def test_partial_group_accounts_for_adapter_revision_and_missing_fields() -> None:
    raw = raw_event('{"outcome":"new"}')
    event = common(raw).model_copy(
        update={
            "parse_status": "PARTIAL",
            "adapter_id": "adapter",
            "adapter_version": 1,
            "unknown_fields": ["outcome", "severity"],
        }
    )
    first = format_fingerprint(raw, event, service())
    assert first == format_fingerprint(
        raw, event.model_copy(update={"unknown_fields": ["severity", "outcome"]}), service()
    )
    assert first != format_fingerprint(
        raw, event.model_copy(update={"adapter_version": 2}), service()
    )
    assert first != format_fingerprint(
        raw, event.model_copy(update={"unknown_fields": ["outcome"]}), service()
    )


@pytest.mark.parametrize(
    "payload", ["plain text", "{broken", '{"a":NaN}', '{"a":1e999}', '{"a":1,"a":2}']
)
def test_malformed_inputs_use_one_coarse_group(payload: str) -> None:
    raw = raw_event(payload)
    other = raw_event("different invalid text")
    assert not supported_json(raw)
    assert format_fingerprint(raw, common(raw), service()) == format_fingerprint(
        other, common(other), service()
    )


def test_nonobjects_and_invalid_utf8_have_separate_coarse_groups() -> None:
    plain = raw_event("not json")
    utf8 = raw_event("not json", encoding_error=True)
    array = raw_event("[]")
    scalar = raw_event('"text"')
    assert not supported_json(utf8)
    assert not supported_json(array)
    assert not supported_json(scalar)
    groups = {format_fingerprint(item, common(item), service()) for item in (plain, utf8, array)}
    assert len(groups) == 3
    assert format_fingerprint(array, common(array), service()) == format_fingerprint(
        scalar, common(scalar), service()
    )


def test_array_shape_is_order_stable_and_ignores_repeated_ordinary_values() -> None:
    assert fingerprint({"items": [{"message": "a"}, {"code": 1}]}) == fingerprint(
        {"items": [{"code": 2}, {"message": "b"}, {"message": "c"}]}
    )
    assert fingerprint({"items": [{"message": "a"}]}) != fingerprint(
        {"items": [{"message": "a"}, {"message": 5}]}
    )


def test_array_limits_refuse_large_payloads_instead_of_sampling_order() -> None:
    payload: JsonValue = {"items": list(range(MAX_ARRAY_ITEMS + 1))}
    reverse: JsonValue = {"items": list(reversed(range(MAX_ARRAY_ITEMS + 1)))}
    assert not supported_json(raw_event(json.dumps(payload)))
    assert fingerprint(payload) == fingerprint(reverse)
    assert supported_json(raw_event(json.dumps({"items": list(range(MAX_ARRAY_ITEMS))})))


def test_size_depth_and_node_limits_refuse_unsupported_objects() -> None:
    assert not supported_json(raw_event(" " * (MAX_PAYLOAD_CHARACTERS + 1)))
    nested = '{"a":' * (MAX_DEPTH + 1) + "null" + "}" * (MAX_DEPTH + 1)
    assert not supported_json(raw_event(nested))
    many_nodes = json.dumps({str(index): None for index in range(MAX_NODES)})
    assert not supported_json(raw_event(many_nodes))
    # Brackets inside strings do not count as parser nesting.
    assert supported_json(raw_event(json.dumps({"message": "[" * 100 + '\\"' * 100})))


def test_group_key_contains_no_payload_values() -> None:
    result = fingerprint({"schema": "secret-schema-value", "message": "secret-message"})
    assert re.fullmatch(r"[0-9a-f]{64}", result)
    assert "secret" not in result


def test_escaped_lone_surrogate_does_not_break_grouping() -> None:
    raw = raw_event('{"schema":"\\ud800"}')
    assert supported_json(raw)
    assert re.fullmatch(r"[0-9a-f]{64}", format_fingerprint(raw, common(raw), service()))
