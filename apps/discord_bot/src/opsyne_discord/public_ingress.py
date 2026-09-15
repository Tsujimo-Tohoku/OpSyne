"""Loopback tunnel target exposing only Discord's signed interaction endpoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from opsyne_discord.app import bounded_body


def create_public_app(client: httpx.AsyncClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if client is not None:
            app.state.client = client
            yield
        else:
            async with httpx.AsyncClient(trust_env=False) as owned:
                app.state.client = owned
                yield

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    @app.post("/interactions")
    async def interactions(request: Request) -> Response:
        signature = request.headers.get("x-signature-ed25519")
        timestamp = request.headers.get("x-signature-timestamp")
        if not signature or not timestamp:
            return Response(status_code=401)
        raw = await bounded_body(request)
        try:
            upstream = await app.state.client.post(
                "http://127.0.0.1:8766/interactions",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Signature-Ed25519": signature,
                    "X-Signature-Timestamp": timestamp,
                },
                timeout=2.5,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return Response(status_code=503)
        # Never forward Location, cookies, or request headers to another destination.
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return app


if __name__ == "__main__":
    uvicorn.run(
        create_public_app(), host="127.0.0.1", port=8767, access_log=False, proxy_headers=False
    )
