"""Local server and offline state backup commands."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

import uvicorn

from opsyne.api.app import create_app
from opsyne.collector.log_discovery import discover_logs
from opsyne.contracts.log_discovery import LogDiscoveryRequest
from opsyne.control.repository import Control
from opsyne.server_config import ServerSettings
from opsyne.storage.instance import InstanceLock


def backup(source: Path, destination: Path, *, restoring: bool = False) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("バックアップ先は新しいディレクトリを指定してください")
    if (source / "restore.pending").exists():
        raise ValueError("復元未完了の状態はバックアップできません")
    names = ("control.sqlite3", "collector.sqlite3", "runner.sqlite3", "demo.sqlite3")
    for name in (*names, "signing.key"):
        if not (source / name).is_file():
            raise ValueError(f"保存状態が不完全です: {name}")
    if len((source / "signing.key").read_bytes()) != 32:
        raise ValueError("署名鍵が不正です")
    lock = InstanceLock(source / "instance.lock")
    lock.acquire()
    try:
        destination.mkdir(parents=True, mode=0o700)
        if restoring:
            with (destination / "restore.pending").open("x", encoding="utf-8") as marker:
                marker.write("未完了の復元。このディレクトリから起動しないでください。")
                marker.flush()
                os.fsync(marker.fileno())
        for name in names:
            path = source / name
            if not path.is_file():
                raise ValueError(f"保存状態が不完全です: {name}")
            original = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            copied = sqlite3.connect(destination / name)
            try:
                original.backup(copied)
            finally:
                original.close()
                copied.close()
        for name in ("signing.key", "tokens.json", "demo-seeded"):
            path = source / name
            if path.exists():
                shutil.copy2(path, destination / name)
        (destination / "backup.json").write_text(
            json.dumps({"format": 1, "complete": True}), encoding="utf-8"
        )
    finally:
        lock.release()


def restore(source: Path, destination: Path) -> None:
    manifest = json.loads((source / "backup.json").read_text(encoding="utf-8"))
    if manifest != {"format": 1, "complete": True}:
        raise ValueError("バックアップが不完全です")
    backup(source, destination, restoring=True)
    control = Control(destination / "control.sqlite3", (destination / "signing.key").read_bytes())
    control.advance_generation("restore")
    control.quarantine_restore()
    (destination / "restore.pending").unlink()


def load_env(path: Path) -> None:
    """Read explicit UTF-8 KEY=VALUE settings without expansion or code execution."""
    settings: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        key, separator, value = value.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError(f"設定ファイルの{number}行目はKEY=VALUE形式ではありません")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"設定ファイルの{number}行目の引用符が閉じていません")
            value = value[1:-1]
        settings[key] = value
    for key, value in settings.items():
        os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpSyne ローカル運用コンソール")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="APIとWeb画面を起動")
    serve.add_argument("--data-dir", type=Path, default=Path(".local/opsyne"))
    serve.add_argument("--host", help="待受IPアドレス (既定127.0.0.1)")
    serve.add_argument("--port", type=int, help="待受ポート (既定8765)")
    serve.add_argument("--env-file", type=Path, help="明示したUTF-8 KEY=VALUE設定を読み込む")
    discovery = commands.add_parser(
        "discover-logs", help="既存の読取権限でログ候補を探索 (JSON出力)"
    )
    discovery.add_argument(
        "--root", action="append", required=True, help="アプリのフォルダー。複数指定可能"
    )
    for name in ("backup", "restore"):
        command = commands.add_parser(name, help="停止中の状態を退避/復元")
        command.add_argument("source", type=Path)
        command.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "discover-logs":
        try:
            result = discover_logs(LogDiscoveryRequest(roots=args.root))
        except ValueError as exc:
            parser.error(str(exc))
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "completed" else 2
    elif args.command == "serve":
        if args.env_file is not None:
            load_env(args.env_file)
        settings = ServerSettings.from_env(host=args.host, port=args.port)
        app = create_app(args.data_dir, server_settings=settings)
        print(f"OpSyne: http://{settings.host}:{settings.port}")
        print(f"ログイン用トークン: {(args.data_dir / 'tokens.json').resolve()}")
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level="warning",
            proxy_headers=True,
            forwarded_allow_ips=settings.trusted_proxies,
        )
    elif args.command == "backup":
        backup(args.source.resolve(), args.destination.resolve())
        print("バックアップ完了 (認証情報を含みます)")
    elif args.command == "restore":
        restore(args.source.resolve(), args.destination.resolve())
        print(
            "復元完了。過去の承認・許可は失効しています。管理者が外部操作履歴を照合するまで実行は停止します"
        )
    return 0
