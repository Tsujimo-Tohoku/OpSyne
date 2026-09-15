"""Run the pinned uv with project-local caches and managed Python installations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def uv_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("UV_CACHE_DIR", str(ROOT / ".uv-cache"))
    env.setdefault("UV_PYTHON_INSTALL_DIR", str(ROOT / ".tools" / "python"))
    env.setdefault("PYTHONUTF8", "1")
    return env


def uv_path() -> str:
    executable = "uv.exe" if os.name == "nt" else "uv"
    local = ROOT / ".tools" / "uv" / "bin" / executable
    if local.is_file():
        return str(local)
    installed = shutil.which("uv")
    if installed:
        return installed
    raise RuntimeError("uv is missing. Run: python scripts/bootstrap.py")


def verify_uv(executable: str) -> None:
    expected = (ROOT / ".uv-version").read_text(encoding="utf-8").strip()
    actual = subprocess.check_output([executable, "--version"], text=True).split()
    if len(actual) < 2 or actual[1] != expected:
        raise RuntimeError(f"uv {expected} is required. Run: python scripts/bootstrap.py")


def main() -> int:
    if len(sys.argv) == 1:
        print("Usage: python scripts/dev.py <uv arguments>", file=sys.stderr)
        return 2
    try:
        executable = uv_path()
        verify_uv(executable)
        return subprocess.call([executable, *sys.argv[1:]], cwd=ROOT, env=uv_environment())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
