"""Run the same quality checks locally and in CI, stopping on the first failure."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    import_linter = Path(sys.executable).parent / (
        "lint-imports.exe" if os.name == "nt" else "lint-imports"
    )
    commands = (
        (sys.executable, "-m", "ruff", "check", "."),
        (sys.executable, "-m", "ruff", "format", "--check", "."),
        (sys.executable, "-m", "mypy"),
        (str(import_linter),),
        (sys.executable, "-m", "pytest"),
    )
    for command in commands:
        print(f"Running: {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
