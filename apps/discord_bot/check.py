"""Run only this adapter's checks; never synchronize or test the parent project."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    for arguments in (
        ("ruff", "check", "."),
        ("ruff", "format", "--check", "."),
        ("mypy",),
        ("pytest",),
    ):
        result = subprocess.run([sys.executable, "-m", *arguments], cwd=root, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
