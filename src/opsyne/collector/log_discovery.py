"""Bounded local filesystem discovery. Never executes configuration or log text."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from opsyne.contracts.log_discovery import (
    DiscoveryNotice,
    LogCandidate,
    LogDiscoveryRequest,
    LogDiscoveryResult,
    LogReference,
    LogSignal,
)

EXCLUDED = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".tools",
        ".uv-cache",
        "__pycache__",
        ".cache",
        ".ssh",
        ".aws",
        ".gnupg",
    }
)
CONFIG_SUFFIXES = {".conf", ".ini", ".yaml", ".yml", ".toml", ".properties", ".service", ".json"}
LOG_NAME = re.compile(
    r"(?:\.log(?:\.\d+)?|\.out|\.err|\.jsonl|\.ndjson)$|^(?:access|error|debug|server)_log$"
)
SECRET_NAME = re.compile(r"(?:^\.env(?:\.|$)|\.pem$|\.key$|credentials|secrets?|tokens?\.json$)")
DIRECTIVE = re.compile(
    r"^\s*[\"']?(access_log|error_log|customlog|errorlog|log_file|log_path|log_dir|"
    r"logfile|logpath|logdirectory|logging\.file\.name|logging\.file\.path|"
    r"filename|standardoutput|standarderror)[\"']?\s*(?:[:=]\s*|\s+)(.+)$",
    re.IGNORECASE,
)
HTTP_REQUEST = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) [^"\r\n]+"\s+[1-5]\d\d\b')
ERROR = re.compile(r"\b(?:error|fatal|critical|exception|traceback|panic)\b", re.IGNORECASE)
LIFECYCLE = re.compile(
    r"\b(?:started|starting|stopped|stopping|shutdown|shutting down|listening on)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class DiscoveryLimits:
    """Internal budgets, deliberately not configurable through the HTTP API."""

    entries: int = 10_000
    depth: int = 24
    candidates: int = 100
    references: int = 64
    total_bytes: int = 4 * 1024 * 1024
    sample_bytes: int = 16 * 1024
    config_bytes: int = 64 * 1024
    seconds: float = 10.0


class _Skipped(Exception):
    pass


def _local_path(value: str) -> Path:
    # Do not resolve symlinks, expand variables, glob, or accept Windows device/UNC paths.
    if len(value) > 4096 or value.startswith(("\\\\", "//")) or "\x00" in value or "://" in value:
        raise _Skipped("unsupported_path")
    path = Path(os.path.abspath(value))
    if os.name == "nt" and ":" in str(path)[len(path.drive) :]:
        raise _Skipped("unsupported_path")
    return path


def _regular_stat(path: Path) -> os.stat_result:
    # Check ancestors as well: a literal destination may contain a linked directory.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise _Skipped("linked_path")
    return info


def _read(path: Path, expected: os.stat_result, count: int, *, tail: bool) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(expected, actual):
            raise _Skipped("changed_file")
        # Recheck parent links after opening. Never intentionally follow reparse points.
        if not os.path.samestat(_regular_stat(path), actual):
            raise _Skipped("changed_file")
        offset = max(0, actual.st_size - count) if tail else 0
        os.lseek(descriptor, offset, os.SEEK_SET)
        value = os.read(descriptor, count)
        return value, bool(offset)
    finally:
        os.close(descriptor)


def _signals(sample: bytes) -> tuple[Literal["jsonl", "text", "empty"], list[LogSignal]]:
    if not sample:
        return "empty", []
    text = sample.decode("utf-8", errors="replace")
    signals: set[LogSignal] = set()
    structured = 0
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            event = None
        if isinstance(event, dict):
            structured += 1
            level = str(event.get("level", event.get("severity", ""))).lower()
            message = str(event.get("message", event.get("msg", "")))
            if level in {"error", "fatal", "critical", "panic"} or ERROR.search(message):
                signals.add("errors")
            if LIFECYCLE.search(message):
                signals.add("lifecycle")
            status = event.get("status", event.get("status_code"))
            method = event.get("method", event.get("http_method"))
            if (
                isinstance(method, str)
                and method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                and re.fullmatch(r"[1-5]\d\d", str(status))
            ):
                signals.add("http_requests")
        else:
            if HTTP_REQUEST.search(line):
                signals.add("http_requests")
            if ERROR.search(line):
                signals.add("errors")
            if LIFECYCLE.search(line):
                signals.add("lifecycle")
    return ("jsonl" if lines and structured == len(lines) else "text"), sorted(signals)


class _Discovery:
    def __init__(self, request: LogDiscoveryRequest, limits: DiscoveryLimits) -> None:
        self.roots = [_local_path(value) for value in request.roots]
        self.limits = limits
        self.deadline = time.monotonic() + limits.seconds
        self.entries = 0
        self.read_bytes = 0
        self.skipped: Counter[str] = Counter()
        self.notices: list[DiscoveryNotice] = []
        self.reached: set[str] = set()
        self.partial = False
        self.visited: set[Path] = set()
        self.unclassified: set[Path] = set()
        self.candidates: dict[Path, LogCandidate] = {}
        self.references: dict[Path, list[LogReference]] = {}
        self.pending: deque[Path] = deque()
        self.reference_count = 0

    def notice(self, path: Path, reason: str, *, incomplete: bool = True) -> None:
        self.skipped[reason] += 1
        self.partial |= incomplete
        if (reason != "excluded" or incomplete) and len(self.notices) < 100:
            self.notices.append(DiscoveryNotice(path=str(path), reason=reason))

    def limit(self, name: str) -> None:
        self.reached.add(name)
        self.partial = True

    def budget(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.limit("time")
        if self.entries >= self.limits.entries:
            self.limit("entries")
        return not self.reached.intersection({"time", "entries", "candidates", "bytes"})

    def read(self, path: Path, info: os.stat_result, *, config: bool) -> bytes:
        count = min(
            self.limits.config_bytes if config else self.limits.sample_bytes,
            self.limits.total_bytes - self.read_bytes,
        )
        if count <= 0:
            self.limit("bytes")
            raise _Skipped("read_budget")
        data, offset = _read(path, info, count, tail=not config)
        self.read_bytes += len(data)
        if b"\x00" in data:
            raise _Skipped("binary_file")
        if offset:
            # A partial first line cannot establish structured evidence.
            _, separator, remainder = data.partition(b"\n")
            data = remainder if separator else b""
            if not data:
                self.notice(path, "sample_no_complete_line")
        if config and info.st_size > count:
            self.notice(path, "config_truncated")
            data = data.rpartition(b"\n")[0]
        return data

    def reference(
        self, config: Path, line: int, key: str, raw: str, *, literal: bool = False
    ) -> None:
        value = raw.strip()
        if literal:
            value = raw
        elif value.startswith(("'", '"')):
            end = value.find(value[0], 1)
            if end < 0:
                self.notice(config, "unresolved_reference")
                return
            value = value[1:end]
        else:
            value = re.split(r"\s|;|,|#", value, maxsplit=1)[0]
        if value.startswith(("append:", "file:")):
            value = value.split(":", 1)[1]
        if value.lower() in {"off", "none", "null"}:
            return
        if value.lower() in {"stdout", "stderr", "journal", "journal+console", "inherit"} or (
            value.startswith(("/dev/", "/proc/", "syslog:", "php://"))
        ):
            self.notice(config, "non_file_destination")
            return
        if not value or any(char in value for char in "$%{}*?`|\n\r") or value.startswith("~"):
            self.notice(config, "unresolved_reference")
            return
        # Generic filename settings only qualify when the destination looks like a log.
        if key.lower() == "filename" and not LOG_NAME.search(Path(value).name.lower()):
            return
        if self.reference_count >= self.limits.references:
            self.limit("references")
            return
        self.reference_count += 1
        try:
            if Path(value).is_absolute():
                targets = [_local_path(value)]
            else:
                # Relative destinations may be relative to a config OR the application cwd.
                targets = list(
                    dict.fromkeys(
                        _local_path(str(base / value)) for base in [config.parent, *self.roots]
                    )
                )
        except _Skipped:
            self.notice(config, "unsupported_reference")
            return
        if len(targets) > 1:
            existing = []
            for target in targets:
                try:
                    target.lstat()
                    existing.append(target)
                except FileNotFoundError:
                    continue
                except OSError:
                    existing.append(target)
            targets = existing or targets[:1]
        for target in targets:
            reference = LogReference(config_path=str(config), line=line)
            references = self.references.setdefault(target, [])
            if reference not in references and len(references) < self.limits.references:
                references.append(reference)
                self.pending.append(target)

    def config(self, path: Path, info: os.stat_result) -> None:
        data = self.read(path, info, config=True)
        text = data.decode("utf-8-sig", errors="replace")
        # JSON may be minified or nest handlers; parse data without importing config code.
        if path.suffix.lower() == ".json":
            try:
                value = json.loads(text)
            except (ValueError, RecursionError):
                return
            self.json_references(path, value, 0)
            return
        for number, line in enumerate(text.splitlines(), 1):
            match = DIRECTIVE.match(line)
            if match:
                self.reference(path, number, match[1], match[2])

    def json_references(self, path: Path, value: object, depth: int) -> None:
        if depth > 16:
            self.notice(path, "config_depth")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and isinstance(item, str):
                    if DIRECTIVE.match(f"{key}={item}"):
                        self.reference(path, 0, key, item, literal=True)
                elif isinstance(item, (dict, list)):
                    self.json_references(path, item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                self.json_references(path, item, depth + 1)

    def visit(self, path: Path, depth: int = 0, *, root: bool = False) -> None:
        if path in self.visited or not self.budget():
            return
        self.visited.add(path)
        self.entries += 1
        if any(part.lower() in EXCLUDED for part in path.parts) or SECRET_NAME.search(
            path.name.lower()
        ):
            self.notice(path, "excluded", incomplete=root or path in self.references)
            return
        try:
            info = _regular_stat(path)
            if stat.S_ISDIR(info.st_mode):
                if depth >= self.limits.depth:
                    self.limit("depth")
                    return
                with os.scandir(path) as entries:
                    for entry in entries:
                        if not self.budget():
                            break
                        self.visit(Path(entry.path), depth + 1)
            elif root:
                self.notice(path, "not_directory")
            elif not stat.S_ISREG(info.st_mode):
                self.notice(path, "special_file")
            elif LOG_NAME.search(path.name.lower()) or path in self.references:
                if len(self.candidates) >= self.limits.candidates:
                    self.limit("candidates")
                    return
                try:
                    sample = self.read(path, info, config=False)
                except PermissionError:
                    self.notice(path, "permission_denied")
                    sample = None
                fmt, signals = _signals(sample) if sample is not None else ("unreadable", [])
                if fmt == "empty" and info.st_size:
                    fmt = "text"
                self.candidates[path] = LogCandidate(
                    path=str(path),
                    size_bytes=info.st_size,
                    modified_at=datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                    format=fmt,
                    signals=signals,
                    sample_bytes=len(sample or b""),
                    references=[],
                )
            elif path.suffix.lower() in CONFIG_SUFFIXES:
                self.config(path, info)
                self.unclassified.add(path)
            else:
                self.unclassified.add(path)
        except _Skipped as exc:
            self.notice(path, str(exc))
        except PermissionError:
            self.notice(path, "permission_denied")
        except FileNotFoundError:
            self.notice(path, "missing")
        except OSError:
            self.notice(path, "read_error")

    def run(self) -> LogDiscoveryResult:
        for root in self.roots:
            self.visit(root, root=True)
        while self.pending and self.budget():
            target = self.pending.popleft()
            # An extensionless reference may have been encountered before its config.
            if target in self.unclassified:
                self.visited.discard(target)
                self.unclassified.discard(target)
            self.visit(target)
        candidates = [
            candidate.model_copy(
                update={
                    "references": [
                        reference
                        for ancestor in (path, *path.parents)
                        for reference in self.references.get(ancestor, [])
                    ]
                }
            )
            for path, candidate in self.candidates.items()
        ]
        candidates.sort(
            key=lambda item: (bool(item.references), bool(item.signals), item.modified_at),
            reverse=True,
        )
        return LogDiscoveryResult(
            roots=[str(root) for root in self.roots],
            status="partial" if self.partial else "completed",
            candidates=candidates,
            inspected_entries=self.entries,
            read_bytes=self.read_bytes,
            skipped=dict(self.skipped),
            limits_reached=sorted(self.reached),
            notices=self.notices,
        )


def discover_logs(
    request: LogDiscoveryRequest, *, limits: DiscoveryLimits | None = None
) -> LogDiscoveryResult:
    """Find candidates using only the caller's current filesystem permissions."""
    try:
        return _Discovery(request, limits or DiscoveryLimits()).run()
    except _Skipped as exc:
        raise ValueError("対応していないローカルパスです") from exc
