from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from opsyne_discord.delivery import DiscordDelivery, Notification, Outbox

TOKEN = "test-bot-token-never-display"
CHANNEL = "123456789"


@pytest.fixture
def outbox(tmp_path: Path) -> Outbox:
    return Outbox(tmp_path / "outbox.sqlite3")


def record(outbox: Outbox, event_id: str = "event-1") -> Notification:
    item = outbox.get(event_id)
    assert item is not None
    return item


def queue(outbox: Outbox) -> None:
    outbox.enqueue("event-1", CHANNEL, {"content": "Synthetic incident"}, now=0)


def test_enqueue_is_durable_and_rejects_changed_payload_or_channel(outbox: Outbox) -> None:
    assert outbox.enqueue("event-1", CHANNEL, {"a": 1, "b": 2}, now=0)
    reopened = Outbox(outbox.path)
    assert not reopened.enqueue("event-1", CHANNEL, {"b": 2, "a": 1}, now=10)
    for channel, payload in [(CHANNEL, {"a": 2}), ("987654321", {"a": 1, "b": 2})]:
        with pytest.raises(ValueError, match="different content"):
            reopened.enqueue("event-1", channel, payload, now=10)
    assert record(reopened).payload == {"a": 1, "b": 2}
    assert record(reopened).next_attempt == 0


def test_concurrent_claim_has_one_winner(outbox: Outbox) -> None:
    queue(outbox)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(outbox.claim, [0.0] * 4))
    assert sum(item is not None for item in results) == 1
    assert record(outbox).state == "SENDING"


def test_restart_requires_reconciliation_and_never_resends(outbox: Outbox) -> None:
    queue(outbox)
    assert outbox.claim(0) is not None
    reopened = Outbox(outbox.path)
    assert record(reopened).state == "SENDING"
    assert reopened.recover_abandoned() == 1
    assert reopened.recover_abandoned() == 0
    assert record(reopened).state == "UNKNOWN"
    assert reopened.claim(100) is None
    with pytest.raises(ValueError, match="numeric"):
        reopened.reconcile("event-1", "not-an-id")
    reopened.reconcile("event-1", "456")
    assert record(reopened).state == "SENT"
    assert record(reopened).message_id == "456"
    assert reopened.claim(100) is None
    with pytest.raises(ValueError, match="not UNKNOWN"):
        reopened.reconcile("event-1", "456")


def test_delivery_fixed_destination_mentions_and_no_token_in_state(
    outbox: Outbox, caplog: pytest.LogCaptureFixture
) -> None:
    outbox.enqueue(
        "event-1", CHANNEL, {"content": "@everyone", "allowed_mentions": {"parse": ["everyone"]}}, 0
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == f"https://discord.com/api/v10/channels/{CHANNEL}/messages"
        assert request.headers["Authorization"] == f"Bot {TOKEN}"
        assert json.loads(request.content)["allowed_mentions"] == {"parse": []}
        return httpx.Response(200, json={"id": "456"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delivery = DiscordDelivery(outbox, client, TOKEN, {CHANNEL})
        assert TOKEN not in repr(delivery)
        assert delivery.run_once(0)
        assert not delivery.run_once(100)
    assert len(requests) == 1
    assert record(outbox).state == "SENT"
    assert record(outbox).message_id == "456"
    assert TOKEN not in repr(record(outbox))
    assert TOKEN not in caplog.text
    assert TOKEN.encode() not in outbox.path.read_bytes()


def test_unlisted_channel_never_reaches_transport(outbox: Outbox) -> None:
    queue(outbox)

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request to {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert DiscordDelivery(outbox, client, TOKEN, {"987654321"}).run_once(0)
    assert record(outbox).state == "FAILED"
    assert record(outbox).last_error == "channel_not_allowed"


def test_rate_limit_is_durable_and_retries_only_when_due(outbox: Outbox) -> None:
    queue(outbox)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"retry_after": 2.5})
        return httpx.Response(200, json={"id": "456"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert DiscordDelivery(outbox, client, TOKEN, {CHANNEL}, clock=lambda: 0.0).run_once(10)
        reopened = Outbox(outbox.path)
        assert record(reopened).state == "PENDING"
        assert record(reopened).next_attempt == 12.5
        delivery = DiscordDelivery(reopened, client, TOKEN, {CHANNEL}, clock=lambda: 0.0)
        assert not delivery.run_once(12)
        assert delivery.run_once(12.5)
    assert len(requests) == 2
    assert record(outbox).state == "SENT"


def test_rate_limit_pauses_other_and_new_events_across_restart(outbox: Outbox) -> None:
    queue(outbox)
    outbox.enqueue("event-2", "987654321", {"content": "Another incident"}, now=1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, json={"retry_after": 5, "global": False})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delivery = DiscordDelivery(outbox, client, TOKEN, {CHANNEL, "987654321"}, clock=lambda: 0.0)
        assert delivery.run_once(10)
        restarted = Outbox(outbox.path)
        restarted.enqueue("event-3", CHANNEL, {"content": "New incident"}, now=11)
        other_worker = DiscordDelivery(
            restarted, client, TOKEN, {CHANNEL, "987654321"}, clock=lambda: 0.0
        )
        assert not other_worker.run_once(14)
        assert len(requests) == 1
        assert other_worker.run_once(15)
        assert len(requests) == 2
        assert not delivery.run_once(19)
    assert record(outbox, "event-2").next_attempt == 20
    assert record(outbox, "event-3").state == "PENDING"


def test_rate_limit_wait_starts_when_slow_response_arrives(outbox: Outbox) -> None:
    queue(outbox)
    outbox.enqueue("event-2", CHANNEL, {"content": "Waiting incident"}, now=1)
    ticks = [100.0]

    def handler(request: httpx.Request) -> httpx.Response:
        ticks[0] += 10.0
        return httpx.Response(429, json={"retry_after": 5})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delivery = DiscordDelivery(outbox, client, TOKEN, {CHANNEL}, clock=lambda: ticks[0])
        assert delivery.run_once(1_000)
    restarted = Outbox(outbox.path)
    assert record(restarted).next_attempt == 1_015
    assert restarted.claim(1_010) is None
    assert restarted.claim(1_014.9) is None
    assert restarted.claim(1_015) is not None


@pytest.mark.parametrize(
    "retry_after", [None, True, "2", 0, -1, 86_401, float("inf"), float("nan")]
)
def test_invalid_rate_limit_response_is_unknown(outbox: Outbox, retry_after: object) -> None:
    queue(outbox)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=json.dumps({"retry_after": retry_after}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delivery = DiscordDelivery(outbox, client, TOKEN, {CHANNEL})
        assert delivery.run_once(0)
        assert not delivery.run_once(100_000)
    assert record(outbox).state == "UNKNOWN"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503, 302])
def test_http_failures_do_not_retry_or_follow_redirects(outbox: Outbox, status: int) -> None:
    queue(outbox)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://example.org/"}, text=TOKEN)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        delivery = DiscordDelivery(outbox, client, TOKEN, {CHANNEL})
        assert delivery.run_once(0)
        assert not delivery.run_once(100)
    assert len(requests) == 1
    assert record(outbox).state == ("FAILED" if 400 <= status < 500 else "UNKNOWN")
    assert TOKEN not in repr(record(outbox))


@pytest.mark.parametrize(
    "body", [b"invalid-json", b"null", b"[]", b"{}", b'{"id": 456}', b'{"id":"x"}']
)
def test_invalid_success_is_unknown(outbox: Outbox, body: bytes) -> None:
    queue(outbox)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delivery = DiscordDelivery(outbox, client, TOKEN, {CHANNEL})
        assert delivery.run_once(0)
        assert not delivery.run_once(100)
    assert record(outbox).state == "UNKNOWN"


def test_timeout_does_not_persist_exception_or_resend(outbox: Outbox) -> None:
    queue(outbox)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(TOKEN, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        delivery = DiscordDelivery(outbox, client, TOKEN, {CHANNEL})
        assert delivery.run_once(0)
        assert not delivery.run_once(100)
    assert record(outbox).state == "UNKNOWN"
    assert record(outbox).last_error == "transport_error"
    assert TOKEN not in repr(record(outbox))


@pytest.mark.parametrize(
    "channel", ["../messages", "", "0", "\uff11\uff12\uff13", "18446744073709551616"]
)
def test_invalid_channel_rejected(outbox: Outbox, channel: str) -> None:
    with pytest.raises(ValueError, match="numeric"):
        outbox.enqueue("event-1", channel, {}, 0)


def test_pending_cannot_be_reconciled(outbox: Outbox) -> None:
    queue(outbox)
    with pytest.raises(ValueError, match="not UNKNOWN"):
        outbox.reconcile("event-1", "456")
