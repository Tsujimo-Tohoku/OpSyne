"""Provider framing and projection tests use synthetic messages only."""

from __future__ import annotations

import json

import pytest

from opsyne.connectors.heroku_logplex import (
    MAX_BODY_BYTES,
    MAX_MESSAGE_BYTES,
    logplex_inputs,
    parse_logplex,
)


def frame(*messages: bytes) -> bytes:
    return b"".join(str(len(message)).encode() + b" " + message for message in messages)


def test_byte_framing_preserves_newlines_unicode_and_non_utf8() -> None:
    messages = ["日本語\nsecond line".encode(), b"\xff\x00\r\n", b"last"]
    assert parse_logplex(frame(*messages)) == messages


@pytest.mark.parametrize(
    "body",
    [b"", b"x hi", b"-1 x", b"+1 x", b"0 ", b"2 a", b"1 ax", b"1a", b"1 a\n", b"12345678 x"],
)
def test_invalid_frame_is_rejected(body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_logplex(body)


def test_all_resource_limits() -> None:
    for body in (
        b"a" * (MAX_BODY_BYTES + 1),
        frame(b"a" * (MAX_MESSAGE_BYTES + 1)),
        frame(*([b"a"] * 10_001)),
    ):
        with pytest.raises(ValueError):
            parse_logplex(body)


def test_projection_keeps_provenance_and_application_fields_separate() -> None:
    raw = (
        b"<40>1 2026-09-15T04:00:00Z host app web.1 - "
        b'{"level":"error","message":"failure","transport":"forged","heroku":{"app_name":"x"}}'
    )
    event = logplex_inputs([raw], "frame-1")[0]
    document = json.loads(event.payload)
    assert event.external_id == "heroku:frame-1:0"
    assert event.observed_at is None
    assert document["transport"] == "heroku_logplex_v1"
    assert document["heroku"]["app_name"] == "app"
    assert document["app"]["transport"] == "forged"
    assert document["raw"].encode() == raw
    assert document["decode_status"] == "json_object"
    assert "severity" not in document and "outcome" not in document


@pytest.mark.parametrize(
    "content",
    [
        b"plain ERROR text",
        b'{"level":"error","level":"info"}',
        b'{"value":NaN}',
        b'{"value":1e999}',
        b'{"value":"\\ud800"}',
        b"[1,2]",
        b"{broken",
        b'{"value":' + b"[" * 2000 + b"1" + b"]" * 2000 + b"}",
    ],
    ids=["text", "duplicate", "nan", "infinite", "surrogate", "array", "malformed", "deep"],
)
def test_ambiguous_or_unsupported_json_remains_text(content: bytes) -> None:
    raw = b"<40>1 2026-09-15T04:00:00Z host app web.1 - " + content
    projected = logplex_inputs([raw], "frame")[0].payload
    projected.encode("utf-8")
    document = json.loads(projected)
    assert "app" not in document
    assert document["decode_status"] == "text"
    assert document["raw"] == raw.decode()


def test_unsupported_header_and_invalid_encoding_retain_text() -> None:
    for raw in (b"no recognized header", b"<999>1 a b c d e text", b"\xff"):
        document = json.loads(logplex_inputs([raw], "frame")[0].payload)
        assert document["decode_status"] == "unsupported_header"
        assert document["raw"] == raw.decode("utf-8", "replace")
        assert "app" not in document
