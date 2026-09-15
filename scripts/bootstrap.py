"""Install project-local uv and synchronize the locked development environment."""

from __future__ import annotations

import argparse
import subprocess
import sys

from dev import ROOT, uv_environment, uv_path, verify_uv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=(ROOT / ".python-version").read_text(encoding="utf-8").strip(),
        help="Python 3.12 executable or version request (default: .python-version)",
    )
    args = parser.parse_args()
    expected = (ROOT / ".uv-version").read_text(encoding="utf-8").strip()
    try:
        try:
            executable = uv_path()
            verify_uv(executable)
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--upgrade",
                    "--target",
                    str(ROOT / ".tools" / "uv"),
                    f"uv=={expected}",
                ],
                cwd=ROOT,
                check=True,
            )
            executable = uv_path()
            verify_uv(executable)
        subprocess.run(
            [executable, "sync", "--locked", "--python", args.python],
            cwd=ROOT,
            env=uv_environment(),
            check=True,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    print("Ready. Run: python scripts/dev.py run --locked python scripts/check.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
