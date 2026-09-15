"""Version-bound, human-approved declarative normalization definitions."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from opsyne.contracts.core import Actor, digest
from opsyne.contracts.observations import AdapterDefinition, Source
from opsyne.control.repository import Conflict, Control, Denied


class AdapterRegistry:
    def __init__(self, control: Control, source: Callable[[str], Source]) -> None:
        self.control = control
        self.source = source

    def _binding(self, definition: AdapterDefinition) -> None:
        source = self.source(definition.source_id)
        service = self.control.service(source.service_id)
        if (
            not source.enabled
            or not service.enabled
            or service.instance_id != definition.target_instance_id
            or service.version != definition.target_version
        ):
            raise Denied("変換定義の対象・版が現在の観測源と一致しません")

    def propose(self, definition: AdapterDefinition, actor: str) -> dict[str, Any]:
        self._binding(definition)
        body = {
            **definition.model_dump(mode="json"),
            "digest": digest(definition.model_dump(mode="json")),
            "status": "DRAFT",
            "proposer": actor,
            "created_at": time.time(),
        }
        with self.control.db.connection() as db:
            if db.execute(
                "SELECT 1 FROM objects WHERE kind='adapter' AND id=?", (definition.id,)
            ).fetchone():
                raise Conflict("定義は不変です。更新は新しいidとversionで提案してください")
            self.control._put(db, "adapter", definition.id, body)
            self.control._audit(db, actor, "adapter.propose", definition.id, str(body["digest"]))
        return body

    @staticmethod
    def definition(body: dict[str, Any]) -> AdapterDefinition:
        return AdapterDefinition.model_validate(
            {k: v for k, v in body.items() if k in AdapterDefinition.model_fields}
        )

    def approve(self, adapter_id: str, expected_digest: str, actor: Actor) -> dict[str, Any]:
        if actor.role not in {"admin", "approver"}:
            raise Denied("変換定義の承認権限が必要です")
        with self.control.db.connection() as db:
            body = self.control._get(db, "adapter", adapter_id)
            definition = self.definition(body)
        self._binding(definition)
        with self.control.db.connection() as db:
            body = self.control._get(db, "adapter", adapter_id)
            if body["status"] != "DRAFT" or body["digest"] != expected_digest:
                raise Conflict("固定された定義とdigestを確認してください")
            if body["proposer"] == actor.actor:
                raise Denied("変換定義の提案者本人は承認できません")
            if digest(definition.model_dump(mode="json")) != expected_digest:
                raise Denied("変換定義の内容が変更されています")
            for row in db.execute("SELECT body FROM objects WHERE kind='adapter'"):
                import json

                other = json.loads(row[0])
                if other["status"] != "APPROVED" or other["source_id"] != definition.source_id:
                    continue
                active = self.definition(other)
                if (active.target_instance_id, active.target_version) != (
                    definition.target_instance_id,
                    definition.target_version,
                ):
                    continue
                separated = any(
                    k in active.conditions and active.conditions[k] != v
                    for k, v in definition.conditions.items()
                )
                if not separated:
                    raise Conflict(
                        "有効な定義と適用範囲が重なります。失効または条件の限定が必要です"
                    )
            body.update(status="APPROVED", approver=actor.actor, approved_at=time.time())
            self.control._put(db, "adapter", adapter_id, body)
            self.control._audit(db, actor.actor, "adapter.approve", adapter_id, expected_digest)
            return body

    def revoke(self, adapter_id: str, actor: Actor) -> dict[str, Any]:
        if actor.role not in {"admin", "approver"}:
            raise Denied("承認権限が必要です")
        with self.control.db.connection() as db:
            body = self.control._get(db, "adapter", adapter_id)
            body.update(status="REVOKED", revoked_at=time.time())
            self.control._put(db, "adapter", adapter_id, body)
            self.control._audit(db, actor.actor, "adapter.revoke", adapter_id, "")
            return body

    def active(self) -> list[AdapterDefinition]:
        return [
            self.definition(item)
            for item in self.control.objects("adapter")
            if item["status"] == "APPROVED"
        ]
