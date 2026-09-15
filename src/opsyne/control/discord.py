"""Discord decisions share Control's transaction, authority, and durable receipts."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from opsyne.contracts.core import Actor, canonical
from opsyne.contracts.discord import DiscordInstallation
from opsyne.control.repository import Control, Denied


def iso(at: float | None) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat() if at is not None else "不明"


class DiscordControl:
    def __init__(self, control: Control) -> None:
        self.control = control
        control.db.initialize("""
            CREATE TABLE IF NOT EXISTS discord_receipts (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                context TEXT NOT NULL, service_id TEXT NOT NULL, reply TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discord_challenges (
                id TEXT PRIMARY KEY, context TEXT NOT NULL, plan_id TEXT NOT NULL,
                digest TEXT NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL);
        """)

    @staticmethod
    def installation_key(config: DiscordInstallation) -> str:
        # SecretStr serialization is masked; bind rotation through an explicit hash.
        return hashlib.sha256(
            (config.model_dump_json() + config.bridge_token.get_secret_value()).encode()
        ).hexdigest()

    @staticmethod
    def _scope(config: DiscordInstallation, user: str, channel: str, service: str) -> None:
        binding = config.users.get(user)
        route = config.routes.get(service)
        if (
            not binding
            or service not in binding.service_ids
            or not route
            or route.channel_id != channel
        ):
            raise Denied("Discordの閲覧対象に含まれません")

    @staticmethod
    def _case(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "case_id": body["id"],
            "title": "OpSyne 案件",
            "state": body["status"],
            "summary": "詳細と根拠は本体で確認してください。",
            "observed_at": iso(body.get("updated_at")),
            "evidence_refs": [],
        }

    def _plan(self, body: dict[str, Any]) -> dict[str, Any]:
        plan = self.control.plan_model(body)
        return {
            "plan_id": plan.id,
            # v1 is the presentation contract; immutable identity is ID + digest.
            "version": 1,
            "digest": plan.digest,
            "target_id": plan.target_instance_id,
            "target_version": str(plan.target_version),
            "operation": f"{plan.capability_id} (版 {plan.capability_version})\n"
            f"提案者: {plan.proposer}\n理由: {plan.reason}\n根拠: {', '.join(plan.evidence_ids)}",
            "impact": plan.impact,
            "preconditions": [
                "実行直前の独立チェックがFAIL、対象・能力・承認世代が現在と一致、競合なし"
            ],
            "success_conditions": [plan.success_condition],
            "abort_conditions": [plan.abort_condition],
            "expires_at": iso(plan.expires_at),
            "approval_request_id": plan.id,
            "state": body["status"],
            "approver": body.get("approver") or "",
        }

    def interact(
        self,
        config: DiscordInstallation,
        actor: Actor,
        user: str,
        channel: str,
        interaction: str,
        fingerprint: str,
        action: str,
        reference: str,
    ) -> dict[str, Any]:
        c = self.control
        with c.db.connection() as db:
            # Evaluate deadlines after acquiring the write transaction, not before a DB wait.
            now = time.time()
            generation = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[
                0
            ]
            context = canonical(
                {
                    "config": self.installation_key(config),
                    "actor": actor.model_dump(mode="json"),
                    "user": user,
                    "channel": channel,
                    "generation": generation,
                }
            )
            old = db.execute("SELECT * FROM discord_receipts WHERE id=?", (interaction,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint or old["context"] != context:
                    raise Denied("受付内容または現在の権限が変わっています")
                self._scope(config, user, channel, old["service_id"])
                reply: dict[str, Any] = json.loads(old["reply"])
                return reply
            challenge = None
            if action == "confirm":
                challenge = db.execute(
                    "SELECT * FROM discord_challenges WHERE id=?", (reference,)
                ).fetchone()
                if (
                    not challenge
                    or challenge["context"] != context
                    or challenge["consumed"]
                    or now >= challenge["expires_at"]
                ):
                    raise Denied("確認ボタンが無効、期限切れ、または本人と一致しません")
                reference = challenge["plan_id"]
            body = c._get(db, "case" if action == "case" else "plan", reference)
            service = str(body["service_id"])
            self._scope(config, user, channel, service)
            reply = {"protocol": "opsyne-discord/v1"}
            if action == "case":
                reply.update(kind="case", case=self._case(body))
            else:
                if not config.routes[service].disclose_plan_details:
                    raise Denied("このサービスの計画詳細はDiscordへ開示できません")
                plan = c.plan_model(body)
                if action in {"review", "confirm", "reject"}:
                    c._current(db, plan, now)
                    if actor.role not in {"admin", "approver"}:
                        raise Denied("承認権限が必要です")
                if action == "plan":
                    reply.update(kind="plan", plan=self._plan(body))
                elif action == "review":
                    if body["status"] != "DRAFT" or plan.proposer == actor.actor:
                        raise Denied("現在の計画を承認できません")
                    nonce = secrets.token_urlsafe(32)
                    db.execute(
                        "INSERT INTO discord_challenges VALUES (?,?,?,?,?,0)",
                        (
                            nonce,
                            context,
                            plan.id,
                            plan.digest,
                            min(now + 120, plan.expires_at),
                        ),
                    )
                    reply.update(kind="review", plan=self._plan(body), confirmation_id=nonce)
                elif action == "confirm" and challenge is not None:
                    c._approve(db, plan.id, challenge["digest"], actor, now)
                    db.execute(
                        "UPDATE discord_challenges SET consumed=1 WHERE id=?", (challenge["id"],)
                    )
                    reply.update(kind="approval_recorded")
                elif action == "reject":
                    c._reject(db, plan.id, actor, "Discordで差し戻し")
                    reply.update(kind="rejected")
                else:
                    raise Denied("未対応の操作です")
            db.execute(
                "INSERT INTO discord_receipts VALUES (?,?,?,?,?)",
                (
                    interaction,
                    fingerprint,
                    context,
                    service,
                    canonical(reply),
                ),
            )
            c._audit(
                db, actor.actor, "discord." + action, reference, interaction, service_id=service
            )
            return reply

    def feed(self, config: DiscordInstallation, cursor: str) -> dict[str, Any]:
        with self.control.db.connection() as db:
            generation = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[
                0
            ]
            epoch = self.installation_key(config) + "-" + generation
            offset = 0
            if cursor:
                prefix, position = cursor.rsplit(":", 1)
                if prefix != epoch or not position.isdigit():
                    raise Denied(
                        "設定・承認世代が変わりました。通知カーソルを明示的にリセットしてください"
                    )
                offset = int(position)
            maximum = db.execute("SELECT COALESCE(MAX(seq),0) FROM discord_events").fetchone()[0]
            if offset > maximum:
                raise Denied("通知履歴が復元されています。カーソルの照合が必要です")
            rows = db.execute(
                "SELECT * FROM discord_events WHERE seq>? ORDER BY seq LIMIT 100", (offset,)
            ).fetchall()
            events = []
            for row in rows:
                offset = row["seq"]
                route = config.routes.get(row["service_id"])
                if route is None:
                    continue
                body = json.loads(row["body"])
                body["observed_at"] = iso(body["observed_at"])
                events.append(
                    {"event_id": row["event_id"], "channel_id": route.channel_id, "case": body}
                )
            return {
                "protocol": "opsyne-discord/v1",
                "events": events,
                "cursor": f"{epoch}:{offset}",
            }
