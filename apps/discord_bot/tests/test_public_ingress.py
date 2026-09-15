from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from opsyne_discord.public_ingress import create_public_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def clients(handler: httpx.MockTransport) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=handler) as upstream:
        app = create_public_app(upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://public",
            ) as client,
        ):
            yield client


async def test_only_signed_interaction_forwarded_byte_exact() -> None:
    raw = b'{ "type": 1, "token": "synthetic" }'

    def receive(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:8766/interactions"
        assert request.content == raw
        assert request.headers["x-signature-ed25519"] == "synthetic-signature"
        assert request.headers["x-signature-timestamp"] == "123"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"type": 1})

    async with clients(httpx.MockTransport(receive)) as client:
        response = await client.post(
            "/interactions",
            content=raw,
            headers={
                "x-signature-ed25519": "synthetic-signature",
                "x-signature-timestamp": "123",
                "authorization": "do-not-forward",
            },
        )
        assert response.status_code == 200 and response.json() == {"type": 1}


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/healthz",
        "/notifications",
        "/api/discord/notifications",
        "/docs",
        "/openapi.json",
        "/interactions/",
    ],
)
async def test_private_paths_not_exposed(path: str) -> None:
    def receive(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Private route must not reach upstream")

    async with clients(httpx.MockTransport(receive)) as client:
        assert (await client.post(path)).status_code == 404


async def test_unsigned_or_oversized_body_rejected_before_forwarding() -> None:
    def receive(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid request must not reach upstream")

    async with clients(httpx.MockTransport(receive)) as client:
        assert (await client.post("/interactions", json={})).status_code == 401
        assert (
            await client.post(
                "/interactions",
                content=b"x" * 65537,
                headers={
                    "x-signature-ed25519": "synthetic",
                    "x-signature-timestamp": "123",
                },
            )
        ).status_code == 413


async def test_upstream_failure_does_not_expose_exception() -> None:
    def receive(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private details", request=request)

    async with clients(httpx.MockTransport(receive)) as client:
        response = await client.post(
            "/interactions",
            content=b"{}",
            headers={
                "x-signature-ed25519": "synthetic",
                "x-signature-timestamp": "123",
            },
        )
        assert response.status_code == 503
        assert "private details" not in response.text
