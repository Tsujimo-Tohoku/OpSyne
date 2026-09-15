"""Local preview, ingress server and explicit outbound delivery commands."""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
from dataclasses import asdict

import httpx
import uvicorn

from opsyne_discord.app import create_app
from opsyne_discord.config import from_environment
from opsyne_discord.delivery import DiscordDelivery, Outbox
from opsyne_discord.presentation import CaseView, PlanView, render_case, render_plan
from opsyne_discord.sender import SenderBusy, run_worker, sender_lock


def preview() -> None:
    case = CaseView(
        "DEMO-204",
        "デモ: 決済APIのエラー率上昇",
        "AWAITING_APPROVAL",
        "合成データです。直近5分のエラー率が12%。原因は調査中です。",
        "2026-09-15T14:32:00+09:00",
        ("synthetic-evidence-1",),
    )
    plan = PlanView(
        "DEMO-PLAN-1",
        1,
        "a" * 64,
        "demo-service-instance",
        "1",
        "検証環境の状態を戻す",
        "合成データの変更のみ。実サービス操作なし。",
        ("検証環境であることを確認",),
        ("独立した合成チェックが成功",),
        ("対象・計画が変更されていたら中止",),
        "2026-09-15T14:45:00+09:00",
        "demo-request-1",
    )
    print(json.dumps({"case": render_case(case), "plan": render_plan(plan)}, ensure_ascii=False))


def commands() -> None:
    print(
        json.dumps(
            {
                "name": "opsyne",
                "description": "OpSyneの案件と固定計画を確認",
                "type": 1,
                "default_member_permissions": "0",
                "options": [
                    {
                        "type": 1,
                        "name": name,
                        "description": description,
                        "options": [
                            {"type": 3, "name": "id", "description": "参照ID", "required": True}
                        ],
                    }
                    for name, description in (
                        ("case", "案件の現在状態を確認"),
                        ("plan", "固定計画を確認"),
                    )
                ],
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "preview", help="Render synthetic cards locally; no network or credentials"
    )
    subcommands.add_parser("commands", help="Print guild command registration JSON; no network")
    serve = subcommands.add_parser("serve", help="Start the signed HTTP interaction endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    subcommands.add_parser("deliver-once", help="Send at most one queued notification to Discord")
    worker = subcommands.add_parser("worker", help="Deliver queued notifications until interrupted")
    worker.add_argument("--poll-seconds", type=float, default=1.0)
    status = subcommands.add_parser("queue-status", help="Inspect delivery metadata for one event")
    status.add_argument("event_id")
    recover = subcommands.add_parser("recover-abandoned", help="Mark interrupted sends UNKNOWN")
    recover.add_argument("--sender-stopped", action="store_true", required=True)
    reconcile = subcommands.add_parser("reconcile", help="Record an independently found message ID")
    reconcile.add_argument("event_id")
    reconcile.add_argument("message_id")
    options = parser.parse_args()
    if options.command == "preview":
        preview()
        return 0
    if options.command == "commands":
        commands()
        return 0
    try:
        settings = from_environment()
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        if options.command == "serve":
            uvicorn.run(
                create_app(settings),
                host=options.host,
                port=options.port,
                access_log=False,
                proxy_headers=False,
            )
            return 0
        outbox = Outbox(settings.state_dir / "outbox.sqlite3")
        if options.command in {"deliver-once", "worker"}:
            with httpx.Client(trust_env=False) as client:
                delivery = DiscordDelivery(outbox, client, settings.bot_token, settings.channel_ids)
                lock_path = settings.state_dir / "sender.lock"
                if options.command == "worker":
                    run_worker(
                        outbox,
                        delivery,
                        lock_path,
                        poll_seconds=options.poll_seconds,
                        stop=lambda: False,
                    )
                else:
                    with sender_lock(lock_path):
                        print(json.dumps({"processed": delivery.run_once(time.time())}))
        elif options.command == "queue-status":
            record = outbox.get(options.event_id)
            if record is None:
                print("Event not found", file=sys.stderr)
                return 1
            # Do not echo notification bodies or credentials into terminal history.
            metadata = asdict(record)
            metadata.pop("payload")
            print(json.dumps(metadata, ensure_ascii=False))
        elif options.command == "recover-abandoned":
            with sender_lock(settings.state_dir / "sender.lock"):
                print(json.dumps({"marked_unknown": outbox.recover_abandoned()}))
        elif options.command == "reconcile":
            outbox.reconcile(options.event_id, options.message_id)
            print(json.dumps({"state": "SENT", "event_id": options.event_id}))
    except KeyboardInterrupt:
        return 0
    except SenderBusy:
        print("A Discord sender is already running for this state directory.", file=sys.stderr)
        return 2
    except (ValueError, OSError, sqlite3.Error):
        print(
            "Configuration or local state error. Check the Bot README and local permissions.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
