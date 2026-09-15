"""Use a private project, environment and cache without changing the parent project."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    local_uv = ROOT.parents[1] / ".tools" / "uv" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    executable = str(local_uv) if local_uv.is_file() else shutil.which("uv")
    if executable is None:
        print("uv is required. Install uv or use the repository's bundled uv.", file=sys.stderr)
        return 2
    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(ROOT / ".venv")
    environment["UV_CACHE_DIR"] = str(ROOT / ".uv-cache")
    environment["UV_PYTHON_INSTALL_DIR"] = str(ROOT / ".local" / "python")
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTHONPATH", None)
    return subprocess.call([executable, *sys.argv[1:]], cwd=ROOT, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
