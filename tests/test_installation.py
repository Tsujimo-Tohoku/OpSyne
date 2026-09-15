"""Check that the package works outside the checkout's import path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_installed_package_is_importable_from_another_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from importlib.metadata import version; "
            "from importlib.resources import files; "
            "import opsyne; "
            "assert files('opsyne').joinpath('py.typed').is_file(); "
            "print(version('opsyne'))",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "Installed package metadata is missing"
