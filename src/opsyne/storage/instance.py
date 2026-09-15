"""Exclusive process lock: startup recovery may only run with one local owner."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import BinaryIO


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: BinaryIO | None = None

    def acquire(self) -> None:
        if self.file is not None:
            raise RuntimeError("このインスタンスは既に状態ロックを保持しています")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            # Windows byte locks reject even reads of an already locked byte.
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            raise RuntimeError("このデータディレクトリを使用しているOpSyneが既にあります") from None
        self.file = file

    def release(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
