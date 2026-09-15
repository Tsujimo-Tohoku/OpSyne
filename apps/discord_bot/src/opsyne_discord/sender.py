"""Single-process notification delivery with an OS-owned lock."""

from __future__ import annotations

import errno
import math
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from opsyne_discord.delivery import DiscordDelivery, Outbox

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class SenderBusy(Exception):
    """Another process already holds the sender lock."""


@contextmanager
def sender_lock(path: Path) -> Iterator[None]:
    """Lock a stable file; do not remove it, which could create a second lock inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if sys.platform == "win32":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            busy_codes = {errno.EAGAIN, errno.EWOULDBLOCK}
            if sys.platform == "win32":
                busy_codes.update({errno.EACCES, errno.EDEADLK})
            if exc.errno in busy_codes:
                raise SenderBusy("another notification sender is running") from None
            raise
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_worker(
    outbox: Outbox,
    delivery: DiscordDelivery,
    lock_path: Path,
    *,
    poll_seconds: float = 1.0,
    stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> None:
    """Recover once under the sender lock, then poll until a stop is requested."""
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll_seconds must be finite and positive")
    with sender_lock(lock_path):
        outbox.recover_abandoned()
        while not stop():
            delivery.run_once(clock())
            sleep(poll_seconds)
