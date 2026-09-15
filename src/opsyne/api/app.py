"""API endpoints preserve authenticated identity and never infer approval from text."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from opsyne.api.auth import AccessTokens
from opsyne.api.heroku import install_heroku_drain, load_drains
from opsyne.collector.log_discovery import discover_logs
from opsyne.contracts.adapter_reviews import AdapterDraftRequest, ExplanationUpdate
from opsyne.contracts.core import Actor, Model, Service
from opsyne.contracts.execution import Capability, CheckConfig
from opsyne.contracts.log_discovery import LogDiscoveryRequest, LogDiscoveryResult
from opsyne.contracts.observations import RawInput, Source
from opsyne.contracts.service_views import ServicePage, ServiceSummary
from opsyne.control.repository import Conflict
from opsyne.runtime import Runtime
from opsyne.server_config import ServerSettings
from opsyne.service_queries import Collection, ServiceQueries


class Approval(Model):
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Reason(Model):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class PlanRequest(Reason):
    capability_id: str


class Ingest(Model):
    events: Annotated[list[RawInput], Field(min_length=1, max_length=500)]


def create_app(
    data_dir: str | Path = ".local/opsyne",
    *,
    background: bool = True,
    runtime: Runtime | None = None,
    server_settings: ServerSettings | None = None,
) -> FastAPI:
    settings = server_settings or ServerSettings.from_env()
    drains = load_drains()
    state = runtime or Runtime(
        Path(data_dir),
        api_key=os.environ.get("OPENAI_API_KEY"),
        daily_call_limit=int(os.environ.get("OPSYNE_DAILY_LLM_CALLS", "20")),
        auto_investigate=os.environ.get("OPSYNE_AUTO_INVESTIGATE", "false").lower() == "true",
        auto_adapter_proposals=os.environ.get("OPSYNE_AUTO_ADAPTER_PROPOSALS", "true").lower()
        == "true",
        adapter_coalesce_seconds=int(os.environ.get("OPSYNE_ADAPTER_COALESCE_SECONDS", "5")),
        adapter_retry_seconds=int(os.environ.get("OPSYNE_ADAPTER_RETRY_SECONDS", "900")),
        adapter_max_attempts=int(os.environ.get("OPSYNE_ADAPTER_MAX_ATTEMPTS", "3")),
    )
    tokens = AccessTokens(state.data_dir / "tokens.json")
    service_queries = ServiceQueries(
        state.control, state.collector.sources, state.collector.coverage, state.execution_list
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if background:
            state.start()
        try:
            yield
        finally:
            if background:
                state.stop()

    app = FastAPI(
        title="OpSyne",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = state
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    install_heroku_drain(app, state, drains)

    @app.middleware("http")
    async def local_request_boundary(request: Request, call_next: Any) -> Response:
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return JSONResponse({"detail": "不正なOriginです"}, status_code=403)
            if (
                parsed.netloc != request.headers.get("host")
                or parsed.scheme != request.url.scheme
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                return JSONResponse(
                    {"detail": "異なるオリジンからのアクセスは許可されていません"}, status_code=403
                )
        if request.method in {"POST", "PUT", "PATCH"}:
            if request.headers.get("transfer-encoding"):
                return JSONResponse({"detail": "Content-Lengthを指定してください"}, status_code=411)
            length = request.headers.get("content-length", "0")
            if re.fullmatch(r"[0-9]{1,7}", length) is None or int(length) > 2_000_000:
                return JSONResponse({"detail": "要求サイズ上限を超えています"}, status_code=413)
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def identity(authorization: Annotated[str | None, Header()] = None) -> Actor:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "アクセストークンが必要です")
        actor = tokens.authenticate(authorization[7:])
        if actor is None:
            raise HTTPException(401, "アクセストークンが無効です")
        return actor

    authentication = Depends(identity)

    def editor(actor: Actor = authentication) -> Actor:
        if actor.role not in {"admin", "operator"}:
            raise HTTPException(403, "操作担当者の権限が必要です")
        return actor

    editing = Depends(editor)

    def admin(actor: Actor = authentication) -> Actor:
        if actor.role != "admin":
            raise HTTPException(403, "管理者権限が必要です")
        return actor

    administration = Depends(admin)
    discovery_lock = Lock()

    @app.post("/api/log-discovery")
    def log_discovery(
        body: LogDiscoveryRequest, actor: Actor = administration
    ) -> LogDiscoveryResult:
        if not discovery_lock.acquire(blocking=False):
            raise HTTPException(409, "ログを探索中です。完了後に再実行してください")
        try:
            return discover_logs(body)
        finally:
            discovery_lock.release()

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": "指定されたリソースが存在しません"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc)}, status_code=409 if isinstance(exc, Conflict) else 400
        )

    @app.exception_handler(PermissionError)
    async def denied(request: Request, exc: PermissionError) -> JSONResponse:
        state.control.audit("api", "request.denied", request.url.path, type(exc).__name__)
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "running",
            "collector_last_poll": state.last_poll,
            "collector_error": state.last_poll_error,
        }

    @app.get("/api/session")
    def session(actor: Actor = authentication) -> Actor:
        return actor

    @app.get("/api/overview")
    def overview(actor: Actor = authentication) -> dict[str, Any]:
        result = state.overview()
        if actor.role == "admin":
            result["recovery"] = {
                "quarantined": state.control.restore_quarantined(),
                "holds": [
                    {**hold, "has_execution": state.runner.for_plan(hold["plan_id"]) is not None}
                    for hold in state.control.recovery_holds()
                ],
            }
        return result

    @app.get("/api/openapi.json", dependencies=[Depends(identity)])
    def schema() -> dict[str, Any]:
        return app.openapi()

    @app.get("/api/services/{service_id}/overview")
    def service_overview(service_id: str, actor: Actor = authentication) -> ServiceSummary:
        return service_queries.summary(service_id, actor)

    @app.get("/api/services/{service_id}/items/{collection}")
    def service_items(
        service_id: str,
        collection: Collection,
        actor: Actor = authentication,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=4096),
    ) -> ServicePage:
        return service_queries.page(service_id, collection, actor, limit, cursor)

    @app.get("/api/history")
    def unassigned_history(
        scope: Literal["global", "unknown"],
        actor: Actor = authentication,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=4096),
    ) -> ServicePage:
        return service_queries.paginate(
            service_queries.history(None, scope), f"history/{scope}", actor, limit, cursor
        )

    @app.get("/api/capabilities", dependencies=[Depends(identity)])
    def capabilities() -> list[dict[str, Any]]:
        return state.control.objects("capability")

    @app.post("/api/capabilities")
    def capability(body: Capability, actor: Actor = administration) -> Capability:
        return state.control.register_capability(body, actor.actor)

    @app.post("/api/services")
    def service(body: Service, actor: Actor = administration) -> Service:
        with state._lock:
            return state.control.register_service(body, actor.actor)

    @app.get("/api/checks", dependencies=[Depends(identity)])
    def checks() -> list[dict[str, Any]]:
        items = []
        for service in state.control.objects("service"):
            with suppress(KeyError):
                items.append(
                    {"service_id": service["id"], **state.control.get("check", service["id"])}
                )
        return items

    @app.post("/api/services/{service_id}/check")
    def set_check(service_id: str, body: CheckConfig, actor: Actor = administration) -> CheckConfig:
        with state._lock:
            state.control.set_check(service_id, body, actor.actor)
        return body

    @app.post("/api/services/{service_id}/verify")
    def service_check(service_id: str, actor: Actor = editing) -> dict[str, Any]:
        return state.check(service_id).model_dump(mode="json")

    @app.post("/api/sources")
    def source(body: Source, actor: Actor = administration) -> Source:
        with state._lock:
            state.control.service(body.service_id)
            result = state.collector.register_source(body)
            state.control.bind_source_scope(body.id, body.service_id)
            state.control.audit(actor.actor, "source.register", body.id, body.kind)
            return result

    @app.post("/api/sources/{source_id}/ingest")
    def ingest(source_id: str, body: Ingest, actor: Actor = editing) -> dict[str, Any]:
        state.control.service(state.source(source_id).service_id)
        events = state.collector.ingest(source_id, body.events)
        state.control.audit(actor.actor, "source.ingest", source_id, str(len(events)))
        state.poll()
        return {"received": len(events), "event_ids": [event.id for event in events]}

    @app.get("/api/evidence", dependencies=[Depends(identity)])
    def evidence(
        source_id: str | None = None,
        service_id: str | None = None,
        query: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        return [
            raw.model_dump(mode="json")
            for raw in state.collector.search(source_id, service_id, query, limit)
        ]

    @app.get("/api/cases/{case_id}", dependencies=[Depends(identity)])
    def case_detail(case_id: str) -> dict[str, Any]:
        return state.case_detail(case_id)

    @app.post("/api/cases/{case_id}/investigate")
    def investigate(case_id: str, actor: Actor = editing) -> dict[str, Any]:
        task = state.control.enqueue(case_id)
        state.control.audit(actor.actor, "investigation.request", task.id, case_id)
        return task.model_dump(mode="json")

    @app.post("/api/cases/{case_id}/adapter-proposal")
    def suggest_adapter(case_id: str, actor: Actor = editing) -> dict[str, Any]:
        task = state.discoveries.request(case_id)
        state.control.audit(actor.actor, "adapter.investigation.request", task.id, case_id)
        return task.model_dump(mode="json")

    @app.post("/api/cases/{case_id}/plans")
    def propose(case_id: str, body: PlanRequest, actor: Actor = editing) -> dict[str, Any]:
        return state.control.create_plan(case_id, body.capability_id, body.reason, actor.actor)

    @app.post("/api/plans/{plan_id}/approve")
    def approve(plan_id: str, body: Approval, actor: Actor = authentication) -> dict[str, Any]:
        return state.control.approve(plan_id, body.digest, actor)

    @app.post("/api/plans/{plan_id}/reject")
    def reject(plan_id: str, body: Reason, actor: Actor = authentication) -> dict[str, Any]:
        return state.control.reject(plan_id, actor, body.reason)

    @app.post("/api/plans/{plan_id}/execute")
    def execute(plan_id: str, actor: Actor = editing) -> dict[str, Any]:
        return state.execute(plan_id, actor).model_dump(mode="json")

    @app.post("/api/executions/{execution_id}/reconcile")
    def reconcile(execution_id: str, actor: Actor = editing) -> dict[str, Any]:
        return state.reconcile(execution_id, actor).model_dump(mode="json")

    @app.post("/api/executions/{execution_id}/verify")
    def verify(execution_id: str, actor: Actor = editing) -> dict[str, Any]:
        return state.verify(execution_id, actor).model_dump(mode="json")

    @app.post("/api/adapters")
    def adapter(body: AdapterDraftRequest, actor: Actor = editing) -> dict[str, Any]:
        return state.adapters.propose(
            state.adapters.definition(body.model_dump(mode="json")), actor.actor, body.explanation
        )

    @app.get("/api/adapters/{adapter_id}")
    def adapter_detail(adapter_id: str, actor: Actor = authentication) -> dict[str, Any]:
        return state.control.get("adapter", adapter_id)

    @app.put("/api/adapters/{adapter_id}/explanation")
    def adapter_explanation(
        adapter_id: str, body: ExplanationUpdate, actor: Actor = editing
    ) -> dict[str, Any]:
        with state._lock:
            return state.adapters.update_explanation(
                adapter_id, body.digest, body.explanation, actor
            )

    @app.post("/api/adapters/{adapter_id}/approve")
    def approve_adapter(
        adapter_id: str, body: Approval, actor: Actor = authentication
    ) -> dict[str, Any]:
        with state._lock:
            validation = state.validate_adapter(adapter_id)
            if validation["supported_count"] == 0:
                raise Conflict(
                    "保持した原本に対応する標本がありません。定義と入力を確認してください"
                )
            state.control.audit(
                actor.actor, "adapter.validate", adapter_id, str(validation["sample_count"])
            )
            return state.adapters.approve(adapter_id, body.digest, actor)

    @app.post("/api/adapters/{adapter_id}/validate")
    def validate_adapter(adapter_id: str, actor: Actor = authentication) -> dict[str, Any]:
        return state.validate_adapter(adapter_id)

    @app.post("/api/adapters/{adapter_id}/revoke")
    def revoke_adapter(adapter_id: str, actor: Actor = authentication) -> dict[str, Any]:
        with state._lock:
            result = state.adapters.revoke(adapter_id, actor)
        state.reprocess(str(result["source_id"]))
        return result

    @app.post("/api/adapters/{adapter_id}/reprocess")
    def reprocess(adapter_id: str, actor: Actor = editing) -> dict[str, int]:
        body = state.control.get("adapter", adapter_id)
        return state.reprocess(str(body["source_id"]))

    @app.post("/api/poll")
    def poll(actor: Actor = editing) -> dict[str, Any]:
        return state.poll()

    @app.post("/api/demo")
    def demo(actor: Actor = administration) -> dict[str, Any]:
        return state.seed_demo(actor)

    @app.post("/api/authorization/revoke-all")
    def revoke_all(actor: Actor = administration) -> dict[str, int]:
        return {"generation": state.control.advance_generation(actor.actor)}

    @app.get("/api/recovery/holds", dependencies=[Depends(admin)])
    def holds() -> list[dict[str, Any]]:
        return state.control.recovery_holds()

    @app.post("/api/recovery/unsent/{plan_id}")
    def release_unsent(
        plan_id: str, body: Reason, actor: Actor = administration
    ) -> dict[str, bool]:
        with state._execution_lock:
            if state.runner.for_plan(plan_id) is not None:
                raise Conflict("実行台帳があるため予約を解除できません。結果の照合が必要です")
            state.control.release_unsent(plan_id, actor, body.reason)
        return {"released": True}

    @app.post("/api/recovery/acknowledge")
    def acknowledge_restore(body: Reason, actor: Actor = administration) -> dict[str, bool]:
        state.control.acknowledge_restore(actor, body.reason)
        return {"acknowledged": True}

    static = Path(__file__).resolve().parents[1] / "web"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    return app
