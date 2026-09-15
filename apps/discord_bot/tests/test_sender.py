from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from opsyne_discord.delivery import DiscordDelivery, Outbox
from opsyne_discord.sender import SenderBusy, run_worker, sender_lock


def test_sender_lock_rejects_duplicates_and_can_be_reacquired(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "sender.lock"
    with sender_lock(path), pytest.raises(SenderBusy), sender_lock(path):
        pytest.fail("a second sender acquired the lock")
    assert path.exists()
    with sender_lock(path):
        pass


def test_sender_lock_releases_when_body_raises(tmp_path: Path) -> None:
    path = tmp_path / "sender.lock"
    with pytest.raises(RuntimeError, match="synthetic failure"), sender_lock(path):
        raise RuntimeError("synthetic failure")
    with sender_lock(path):
        pass


def test_other_io_errors_are_not_reported_as_sender_busy(tmp_path: Path) -> None:
    parent = tmp_path / "ordinary-file"
    parent.write_text("occupied", encoding="utf-8")
    with pytest.raises(OSError), sender_lock(parent / "sender.lock"):
        pytest.fail("a file cannot be the lock's parent directory")


def test_os_releases_lock_after_process_is_killed(tmp_path: Path) -> None:
    path = tmp_path / "sender.lock"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from opsyne_discord.sender import sender_lock\n"
        "with sender_lock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "locked"
            with pytest.raises(SenderBusy), sender_lock(path):
                pytest.fail("a live process already holds the lock")
        finally:
            process.kill()
            process.communicate(timeout=10)
    with sender_lock(path):
        pass


def test_worker_recovers_once_and_dispatches_with_sleep_every_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("abandoned", "123", {"content": "Uncertain delivery"}, 0)
    assert outbox.claim(0) is not None
    outbox.enqueue("pending", "123", {"content": "New incident"}, 0)
    requests: list[httpx.Request] = []
    pauses: list[float] = []
    ticks = [10.0]
    recoveries: list[int] = []
    original_recover = outbox.recover_abandoned

    def recover() -> int:
        count = original_recover()
        recoveries.append(count)
        return count

    monkeypatch.setattr(outbox, "recover_abandoned", recover)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"retry_after": 2})
        return httpx.Response(200, json={"id": "456"})

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        ticks[0] += seconds

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_worker(
            outbox,
            DiscordDelivery(outbox, client, "synthetic-token", {"123"}, clock=lambda: 0.0),
            tmp_path / "sender.lock",
            stop=lambda: len(pauses) == 4,
            sleep=sleep,
            clock=lambda: ticks[0],
        )
    assert pauses == [1.0, 1.0, 1.0, 1.0]
    assert recoveries == [1]
    assert len(requests) == 2
    abandoned = outbox.get("abandoned")
    pending = outbox.get("pending")
    assert abandoned is not None and abandoned.state == "UNKNOWN"
    assert pending is not None and pending.state == "SENT"


def test_busy_worker_does_not_recover_active_delivery(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("active", "123", {}, 0)
    assert outbox.claim(0) is not None
    path = tmp_path / "sender.lock"
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        delivery = DiscordDelivery(outbox, client, "synthetic-token", {"123"})
        with sender_lock(path), pytest.raises(SenderBusy):
            run_worker(outbox, delivery, path, stop=lambda: True)
    active = outbox.get("active")
    assert active is not None and active.state == "SENDING"


@pytest.mark.parametrize("poll_seconds", [0.0, -1.0, float("nan"), float("inf")])
def test_worker_rejects_invalid_poll_before_lock_or_recovery(
    tmp_path: Path, poll_seconds: float
) -> None:
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("active", "123", {}, 0)
    assert outbox.claim(0) is not None
    lock_path = tmp_path / "sender.lock"
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        delivery = DiscordDelivery(outbox, client, "synthetic-token", {"123"})
        with pytest.raises(ValueError, match="finite and positive"):
            run_worker(outbox, delivery, lock_path, poll_seconds=poll_seconds, stop=lambda: True)
    assert not lock_path.exists()
    active = outbox.get("active")
    assert active is not None and active.state == "SENDING"
