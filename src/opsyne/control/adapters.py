"""Version-bound, human-approved declarative normalization definitions."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from opsyne.contracts.adapter_reviews import AgentExplanation, HumanExplanation
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

    def propose(
        self,
        definition: AdapterDefinition,
        actor: str,
        explanation: HumanExplanation | None = None,
        *,
        agent_explanation: AgentExplanation | None = None,
    ) -> dict[str, Any]:
        self._binding(definition)
        body = {
            **definition.model_dump(mode="json"),
            "review_schema_version": 1,
            "explanation_revision": 1,
            "explanation": {
                "human": explanation.model_dump(mode="json") if explanation else None,
                "recorded_by": actor if explanation else None,
                "agent": agent_explanation.model_dump(mode="json") if agent_explanation else None,
            },
            "explanation_editors": [actor] if explanation else [],
            "status": "DRAFT",
            "proposer": actor,
            "created_at": time.time(),
        }
        body["digest"] = self.review_digest(body)
        with self.control.db.connection() as db:
            if db.execute(
                "SELECT 1 FROM objects WHERE kind='adapter' AND id=?", (definition.id,)
            ).fetchone():
                raise Conflict("定義は不変です。更新は新しいidとversionで提案してください")
            self.control._put(db, "adapter", definition.id, body)
            self.control._audit(db, actor, "adapter.propose", definition.id, str(body["digest"]))
        return body

    @classmethod
    def review_digest(cls, body: dict[str, Any]) -> str:
        definition = cls.definition(body).model_dump(mode="json")
        if "review_schema_version" not in body:
            if any(
                key in body
                for key in ("explanation", "explanation_revision", "explanation_editors")
            ):
                raise Denied("説明の契約バージョンがありません")
            return digest(definition)
        if body["review_schema_version"] != 1:
            raise Denied("未対応の説明契約です")
        return digest(
            {
                "definition": definition,
                "review_schema_version": 1,
                "explanation_revision": body["explanation_revision"],
                "explanation": body["explanation"],
                "explanation_editors": body["explanation_editors"],
            }
        )

    def update_explanation(
        self,
        adapter_id: str,
        expected_digest: str,
        explanation: HumanExplanation,
        actor: Actor,
    ) -> dict[str, Any]:
        if actor.role not in {"admin", "operator"}:
            raise Denied("説明の編集権限が必要です")
        with self.control.db.connection() as db:
            body = self.control._get(db, "adapter", adapter_id)
            if body["status"] != "DRAFT" or body["digest"] != expected_digest:
                raise Conflict("最新の未承認定義とdigestを確認してください")
            if self.review_digest(body) != expected_digest:
                raise Denied("変換定義または説明が変更されています")
            revision = body.get("explanation_revision", 0)
            self.control._put(db, "adapter_review", f"{adapter_id}:{revision}", body)
            editors = list(body.get("explanation_editors", []))
            if actor.actor not in editors:
                editors.append(actor.actor)
            body.update(
                review_schema_version=1,
                explanation_revision=revision + 1,
                explanation={
                    "human": explanation.model_dump(mode="json"),
                    "recorded_by": actor.actor,
                    "agent": body.get("explanation", {}).get("agent"),
                },
                explanation_editors=editors,
            )
            body["digest"] = self.review_digest(body)
            self.control._put(db, "adapter", adapter_id, body)
            self.control._audit(
                db, actor.actor, "adapter.explanation.update", adapter_id, body["digest"]
            )
            return body

    @staticmethod
    def definition(body: dict[str, Any]) -> AdapterDefinition:
        return AdapterDefinition.model_validate(
            {k: v for k, v in body.items() if k in AdapterDefinition.model_fields}
        )

    def _overlap(self, db: sqlite3.Connection, definition: AdapterDefinition) -> None:
        for row in db.execute("SELECT body FROM objects WHERE kind='adapter'"):
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
                key in active.conditions
                and (
                    type(active.conditions[key]) is not type(value)
                    or active.conditions[key] != value
                )
                for key, value in definition.conditions.items()
            )
            if not separated:
                raise Conflict("有効な定義と適用範囲が重なります。失効または条件の限定が必要です")

    def validate_candidate(self, definition: AdapterDefinition) -> None:
        """Check current binding and active overlaps without saving or activating a draft."""
        self._binding(definition)
        with self.control.db.connection() as db:
            self._overlap(db, definition)

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
            if body["proposer"] == actor.actor or actor.actor in body.get(
                "explanation_editors", []
            ):
                raise Denied("変換定義の提案者本人は承認できません")
            if self.review_digest(body) != expected_digest:
                raise Denied("変換定義の内容が変更されています")
            definition = self.definition(body)
            self._overlap(db, definition)
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
