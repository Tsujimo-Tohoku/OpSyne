"""Discovery reads synthetic local files, preserves uncertainty, and never registers sources."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opsyne import cli
from opsyne.api.app import create_app
from opsyne.collector import log_discovery as discovery
from opsyne.contracts.log_discovery import LogDiscoveryRequest, LogDiscoveryResult


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def scan(root: Path, **limits: Any) -> LogDiscoveryResult:
    return discovery.discover_logs(
        LogDiscoveryRequest(roots=[str(root)]),
        limits=replace(discovery.DiscoveryLimits(), **limits),
    )


def test_nested_and_configured_external_logs(tmp_path: Path) -> None:
    app = tmp_path / "app"
    access = write(app / "var" / "log" / "access.log", 'host - - [date] "GET / HTTP/1.1" 200 5\n')
    external = write(
        tmp_path / "server logs" / "service.log", '{"level":"error","message":"SECRET"}\n'
    )
    config = write(app / "config" / "nginx.conf", f'error_log "{external.as_posix()}" warn;\n')
    lifecycle = write(app / "run" / "stdout.out", "Server started\n")
    result = scan(app)
    assert result.status == "completed"
    assert {item.path for item in result.candidates} == {str(access), str(external), str(lifecycle)}
    first = result.candidates[0]
    assert first.path == str(external)
    assert first.references[0].config_path == str(config)
    assert first.references[0].line == 1
    assert first.signals == ["errors"]
    assert first.format == "jsonl"
    assert "SECRET" not in result.model_dump_json()
    assert next(item for item in result.candidates if item.path == str(access)).signals == [
        "http_requests"
    ]
    assert next(item for item in result.candidates if item.path == str(lifecycle)).signals == [
        "lifecycle"
    ]


def test_relative_paths_json_directory_and_extensionless_destination(tmp_path: Path) -> None:
    log = write(tmp_path / "logs" / "app.log", '{"method":"GET","status":503}\n')
    plain = write(tmp_path / "current", "ERROR failed\n")
    write(
        tmp_path / "logging.json", json.dumps({"handlers": {"file": {"filename": "logs/app.log"}}})
    )
    write(tmp_path / "application.conf", "LOG_DIR=logs\nLOG_FILE=current\n")
    result = scan(tmp_path)
    assert result.status == "completed"
    assert {item.path for item in result.candidates} == {str(log), str(plain)}
    assert all(item.references for item in result.candidates)
    assert next(item for item in result.candidates if item.path == str(log)).signals == [
        "http_requests"
    ]


def test_empty_unrecognized_and_non_file_destinations(tmp_path: Path) -> None:
    write(tmp_path / "empty.log", "")
    write(tmp_path / "unknown.log", "some custom payload\n")
    write(
        tmp_path / "app.service",
        "StandardOutput=journal\nStandardError=append:${LOG_DIR}/error.log\n",
    )
    result = scan(tmp_path)
    assert result.status == "partial"
    assert all(not item.signals for item in result.candidates)
    assert {item.format for item in result.candidates} == {"empty", "text"}
    assert result.skipped == {"non_file_destination": 1, "unresolved_reference": 1}
    assert "${LOG_DIR}" not in result.model_dump_json()


def test_empty_scope_and_missing_are_distinct(tmp_path: Path) -> None:
    assert scan(tmp_path).status == "completed"
    result = scan(tmp_path / "missing")
    assert result.status == "partial"
    assert result.skipped == {"missing": 1}
    assert not result.candidates
    assert scan(write(tmp_path / "file.log", "")).skipped == {"not_directory": 1}


@pytest.mark.parametrize(
    "value", ["", "  ", "https://example.org", "//server/share", "\\\\server\\share", "a\x00b"]
)
def test_reject_remote_and_invalid_roots(value: str) -> None:
    with pytest.raises(ValueError):
        LogDiscoveryRequest(roots=[value])


def test_exclusions_and_binary_files_are_not_read_as_logs(tmp_path: Path) -> None:
    write(tmp_path / "node_modules" / "library.log", "ERROR should not inspect\n")
    write(tmp_path / ".env", "LOG_FILE=private\n")
    write(tmp_path / "secrets.log", "ERROR secret\n")
    (tmp_path / "binary.log").write_bytes(b"\x00binary")
    result = scan(tmp_path)
    assert not result.candidates
    assert result.skipped == {"excluded": 3, "binary_file": 1}


def test_read_permission_failure_returns_unreadable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "error.log", "ERROR not actually readable\n")

    def denied(*args: Any, **kwargs: Any) -> tuple[bytes, bool]:
        raise PermissionError("test")

    monkeypatch.setattr(discovery, "_read", denied)
    result = scan(tmp_path)
    assert result.status == "partial"
    assert result.candidates[0].format == "unreadable"
    assert result.candidates[0].signals == []
    assert result.read_bytes == 0


def test_directory_permission_failure_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("test")

    monkeypatch.setattr(os, "scandir", denied)
    assert scan(tmp_path).skipped == {"permission_denied": 1}


def test_linked_ancestor_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "link"
    write(directory / "access.log", "ERROR\n")
    original = Path.lstat

    def lstat(path: Path, **kwargs: Any) -> os.stat_result:
        info = original(path, **kwargs)
        if path == directory:
            values = list(info)
            values[0] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    result = scan(directory)
    assert not result.candidates
    assert result.skipped == {"linked_path": 1}


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO and unprivileged symlinks")
def test_real_symlink_cycle_and_fifo(tmp_path: Path) -> None:
    (tmp_path / "cycle").symlink_to(tmp_path, target_is_directory=True)
    if sys.platform != "win32":
        os.mkfifo(tmp_path / "pipe.log")
    result = scan(tmp_path)
    assert not result.candidates
    assert result.skipped == {"linked_path": 1, "special_file": 1}


@pytest.mark.parametrize(
    "limits, reached",
    [
        ({"entries": 1}, "entries"),
        ({"depth": 0}, "depth"),
        ({"seconds": 0}, "time"),
        ({"candidates": 0}, "candidates"),
        ({"total_bytes": 0}, "bytes"),
    ],
)
def test_limits_report_partial(tmp_path: Path, limits: dict[str, int], reached: str) -> None:
    write(tmp_path / "file.log", "ERROR\n")
    result = scan(tmp_path, **limits)
    assert result.status == "partial"
    assert reached in result.limits_reached


def test_large_sample_is_bounded_and_tail_is_classified(tmp_path: Path) -> None:
    write(tmp_path / "app.log", "x" * 100_000 + '\n{"level":"error","message":"failed"}\n')
    result = scan(tmp_path, sample_bytes=1024)
    assert result.read_bytes == 1024
    assert result.candidates[0].signals == ["errors"]
    assert result.candidates[0].format == "jsonl"
    write(tmp_path / "app.log", "x" * 100_000)
    result = scan(tmp_path, sample_bytes=1024)
    assert result.candidates[0].format == "text"
    assert result.skipped == {"sample_no_complete_line": 1}


def test_overlapping_roots_are_deduplicated(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    write(nested / "app.log", "started\n")
    result = discovery.discover_logs(LogDiscoveryRequest(roots=[str(tmp_path), str(nested)]))
    assert len(result.candidates) == 1
    assert result.inspected_entries == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_real_windows_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    write(target / "error.log", "ERROR\n")
    link = tmp_path / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
    )
    try:
        result = scan(link)
        assert result.skipped == {"linked_path": 1}
        assert not result.candidates
    finally:
        link.rmdir()


def test_config_relative_to_application_root_and_explicit_json_log(tmp_path: Path) -> None:
    log = write(tmp_path / "log" / "events.json", '{"level":"error"}\n')
    write(tmp_path / "config" / "app.conf", "LOG_FILE=log/events.json\n")
    result = scan(tmp_path)
    assert result.status == "completed"
    assert [item.path for item in result.candidates] == [str(log)]
    assert result.candidates[0].signals == ["errors"]


def test_reference_and_config_limits_are_reported(tmp_path: Path) -> None:
    write(tmp_path / "app.conf", "LOG_FILE=missing1.log\nLOG_FILE=missing2.log\n")
    result = scan(tmp_path, references=1)
    assert result.limits_reached == ["references"]
    assert result.skipped == {"missing": 1}
    result = scan(tmp_path, config_bytes=22)
    assert result.status == "partial"
    assert result.skipped["config_truncated"] == 1


def test_json_escaped_absolute_destination_with_spaces(tmp_path: Path) -> None:
    root = tmp_path / "app"
    log = write(tmp_path / "logs with spaces" / "app.log", "ERROR\n")
    write(
        root / "logging.json", json.dumps({"handlers": {"file": {"filename": str(log)}}}, indent=2)
    )
    result = scan(root)
    assert result.status == "completed"
    assert [item.path for item in result.candidates] == [str(log)]
    assert len(result.candidates[0].references) == 1


def test_changed_file_is_not_read(tmp_path: Path) -> None:
    path = write(tmp_path / "app.log", "ERROR original\n")
    different = write(tmp_path / "different.log", "ERROR different\n")
    with pytest.raises(discovery._Skipped, match="changed_file"):
        discovery._read(path, different.stat(), 1024, tail=True)


def test_cli_outputs_json_without_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "app.log", "starting\n")
    monkeypatch.setattr(sys, "argv", ["opsyne", "discover-logs", "--root", str(tmp_path)])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["candidates"][0]["signals"] == ["lifecycle"]
    assert sorted(item.name for item in tmp_path.iterdir()) == ["app.log"]
    monkeypatch.setattr(
        sys, "argv", ["opsyne", "discover-logs", "--root", str(tmp_path / "absent")]
    )
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_api_is_admin_only_read_only_and_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPSYNE_HEROKU_DRAINS_FILE", raising=False)
    app = create_app(tmp_path / "state", background=False)
    tokens = json.loads((tmp_path / "state" / "tokens.json").read_text(encoding="utf-8"))
    headers = {item["actor"]: {"Authorization": f"Bearer {item['token']}"} for item in tokens}
    root = tmp_path / "app"
    write(root / "app.log", "started\n")
    body = {"roots": [str(root)]}
    with TestClient(app) as client:
        assert client.post("/api/log-discovery", json=body).status_code == 401
        for actor in ("viewer", "operator", "reviewer"):
            assert (
                client.post("/api/log-discovery", headers=headers[actor], json=body).status_code
                == 403
            )
        response = client.post("/api/log-discovery", headers=headers["owner"], json=body)
        assert response.status_code == 200
        assert response.json()["candidates"][0]["signals"] == ["lifecycle"]
        assert response.headers["cache-control"] == "no-store"
        assert app.state.runtime.collector.sources() == []
        assert (
            client.post(
                "/api/log-discovery", headers=headers["owner"], json={"roots": []}
            ).status_code
            == 422
        )
        started, release = Event(), Event()
        original = discovery.discover_logs

        def blocked(request: LogDiscoveryRequest) -> LogDiscoveryResult:
            started.set()
            assert release.wait(5)
            return original(request)

        monkeypatch.setattr("opsyne.api.app.discover_logs", blocked)
        responses: list[int] = []

        def invoke() -> None:
            responses.append(
                client.post("/api/log-discovery", headers=headers["owner"], json=body).status_code
            )

        worker = Thread(target=invoke)
        worker.start()
        try:
            assert started.wait(5)
            assert (
                client.post("/api/log-discovery", headers=headers["owner"], json=body).status_code
                == 409
            )
        finally:
            release.set()
            worker.join(5)
        assert responses == [200]
