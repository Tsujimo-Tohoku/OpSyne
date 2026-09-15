"""Read-only composition for service navigation, without global display limits."""

import time
from collections.abc import Callable
from typing import Any, Literal, cast

from pydantic import JsonValue

from opsyne.contracts.core import Actor, digest
from opsyne.contracts.observations import Coverage, Source
from opsyne.contracts.service_views import ServicePage, ServiceSummary
from opsyne.control.repository import Conflict, Control

Collection = Literal["cases", "approvals", "executions", "history"]


class ServiceQueries:
    def __init__(
        self,
        control: Control,
        sources: Callable[[], list[Source]],
        coverage: Callable[[], list[Coverage]],
        executions: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self.control = control
        self.sources = sources
        self.coverage = coverage
        self.executions = executions

    @staticmethod
    def review(item: dict[str, Any], actor: Actor) -> dict[str, Any]:
        contributors = [item.get("proposer"), *item.get("explanation_editors", [])]
        reason = (
            "role"
            if actor.role not in {"admin", "approver"}
            else ("self_authored" if actor.actor in contributors else None)
        )
        return {"can_review": reason is None, "reason": reason, "authorization_required": True}

    def approvals(self, service_id: str, actor: Actor) -> list[dict[str, Any]]:
        sources = {source.id for source in self.sources() if source.service_id == service_id}
        result = []
        for kind in ("plan", "adapter"):
            for item in self.control.objects(kind):
                belongs = (
                    item.get("service_id") == service_id
                    if kind == "plan"
                    else (item.get("source_id") in sources)
                )
                if belongs and item["status"] == "DRAFT":
                    result.append(
                        {
                            "kind": kind,
                            "id": item["id"],
                            "definition": item,
                            "review": self.review(item, actor),
                        }
                    )
        return result

    def summary(self, service_id: str, actor: Actor) -> ServiceSummary:
        service = self.control.service(service_id)
        approvals = self.approvals(service_id, actor)
        return ServiceSummary(
            service=service.model_dump(mode="json"),
            unresolved_incidents=sum(
                item["service_id"] == service_id and item["status"] != "RESOLVED"
                for item in self.control.objects("case")
            ),
            pending_plans=sum(item["kind"] == "plan" for item in approvals),
            pending_adapters=sum(item["kind"] == "adapter" for item in approvals),
            reviewable_by_actor={
                kind: sum(
                    item["kind"] == kind and item["review"]["can_review"] for item in approvals
                )
                for kind in ("plan", "adapter")
            },
            coverage=[
                item.model_dump(mode="json")
                for item in self.coverage()
                if item.service_id == service_id
            ],
            observed_at=time.time(),
            history_unknown_count=sum(
                row["scope"] == "unknown" for row in self.control.scoped_history()
            ),
        )

    def history(self, service_id: str | None, scope: str = "service") -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.control.scoped_history()
            if row["scope"] == scope and (scope != "service" or row["service_id"] == service_id)
        ]
        executions = {item["id"]: item for item in self.executions()}
        for row in rows:
            row["current_object"] = None
            row["related_plan"] = None
            if row["scope"] != "service" or not row["object_kind"]:
                continue
            try:
                kind = row["object_kind"]
                obj = (
                    executions[row["object_id"]]
                    if kind == "execution"
                    else self.control.get(
                        "source_scope" if kind == "source" else kind, row["object_id"]
                    )
                )
                row["current_object"] = obj
                if kind == "execution":
                    row["related_plan"] = self.control.get("plan", obj["plan_id"])
            except KeyError:
                pass  # Missing objects do not erase the immutable audit event or invent a scope.
        return rows

    def page(
        self,
        service_id: str,
        collection: Collection,
        actor: Actor,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ServicePage:
        self.control.service(service_id)  # Missing is a 404, never an empty successful page.
        if collection == "approvals":
            items = self.approvals(service_id, actor)
        elif collection == "history":
            items = self.history(service_id)
        else:
            values = (
                self.executions() if collection == "executions" else self.control.objects("case")
            )
            items = [item for item in values if item["service_id"] == service_id]
        return self.paginate(items, f"{service_id}/{collection}", actor, limit, cursor)

    def paginate(
        self,
        items: list[dict[str, Any]],
        scope: str,
        actor: Actor,
        limit: int,
        cursor: str | None,
    ) -> ServicePage:
        if not 1 <= limit <= 200:
            raise ValueError("limitは1〜200です")
        items = sorted(items, key=lambda item: (item.get("kind", ""), item["id"]), reverse=True)
        fingerprint = digest(cast(JsonValue, items))
        identity = {"scope": scope, "actor": actor.model_dump(mode="json"), "limit": limit}
        start = 0
        if cursor:
            saved = self.control.read_page_cursor(cursor)
            if saved.get("identity") != identity:
                raise ValueError("カーソルの対象・主体・ページサイズが一致しません")
            if saved.get("fingerprint") != fingerprint:
                raise Conflict("一覧が更新されました。カーソルなしで先頭から取得し直してください")
            start = saved["offset"]
            if type(start) is not int or not 0 <= start <= len(items):
                raise ValueError("カーソルの位置が無効です")
        selected = items[start : start + limit]
        more = start + limit < len(items)
        return ServicePage(
            items=selected,
            total=len(items),
            has_more=more,
            next_cursor=self.control.page_cursor(
                {"identity": identity, "fingerprint": fingerprint, "offset": start + limit}
            )
            if more
            else None,
            collection_state="empty"
            if not items
            else "complete"
            if len(selected) == len(items)
            else "partial",
            observed_at=time.time(),
        )
