"""Source-bound machine intake, independent of browser/operator credentials."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from opsyne.collector.service import DuplicateConflict
from opsyne.connectors.heroku_logplex import (
    MAX_BODY_BYTES,
    MAX_MESSAGES,
    logplex_inputs,
    parse_logplex,
)
from opsyne.contracts.core import Model
from opsyne.runtime import Runtime

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,200}$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class DrainBinding(Model):
    source_id: Identifier
    service_id: Identifier
    username: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    password_env: EnvName
    drain_token_env: EnvName | None = None


@dataclass(frozen=True)
class DrainCredential:
    binding: DrainBinding
    basic_digest: bytes = field(repr=False)
    token_digest: bytes | None = field(repr=False)


def load_drains() -> dict[str, DrainCredential]:
    """Read an explicit owner-managed mapping; never create a source from incoming data."""
    filename = os.environ.get("OPSYNE_HEROKU_DRAINS_FILE")
    if not filename:
        return {}
    try:
        with Path(filename).open("rb") as file:
            body = file.read(65_537)
        if len(body) > 65_536:
            raise ValueError("configuration exceeds limit")
        entries = json.loads(body)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("configuration must list bindings")
        credentials: dict[str, DrainCredential] = {}
        password_digests: set[bytes] = set()
        for entry in entries:
            binding = DrainBinding.model_validate(entry)
            password = os.environ[binding.password_env]
            if not 32 <= len(password) <= 256 or not all(33 <= ord(c) <= 126 for c in password):
                raise ValueError("invalid dedicated password")
            digest = hashlib.sha256(f"{binding.username}:{password}".encode()).digest()
            password_digest = hashlib.sha256(password.encode()).digest()
            if binding.source_id in credentials or password_digest in password_digests:
                raise ValueError("duplicate source or reused credentials")
            token = os.environ[binding.drain_token_env] if binding.drain_token_env else None
            if token is not None and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", token) is None:
                raise ValueError("invalid drain token")
            credentials[binding.source_id] = DrainCredential(
                binding, digest, hashlib.sha256(token.encode()).digest() if token else None
            )
            password_digests.add(password_digest)
        return credentials
    except (OSError, KeyError, ValueError, TypeError, RecursionError):
        # Validation errors can contain input values: do not expose them, even on startup.
        raise ValueError("invalid Heroku drain configuration or missing dedicated secret") from None


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise HTTPException(400, f"exactly one {name} header is required")
    return values[0]


def _authorize(request: Request, credential: DrainCredential | None) -> None:
    values = request.headers.getlist("authorization")
    value = values[0] if len(values) == 1 else ""
    scheme, _, encoded = value.partition(" ")
    supplied = b""
    if scheme.lower() == "basic" and len(encoded) <= 1024:
        with suppress(ValueError, binascii.Error):
            supplied = base64.b64decode(encoded, validate=True)
    digest = hashlib.sha256(supplied).digest()
    known = credential.basic_digest if credential else bytes(32)
    if not hmac.compare_digest(digest, known) or credential is None:
        raise HTTPException(401, "invalid drain credentials", headers={"WWW-Authenticate": "Basic"})


def install_heroku_drain(
    app: FastAPI, state: Runtime, credentials: dict[str, DrainCredential]
) -> None:
    @app.post("/api/drains/heroku/{source_id}", status_code=204)
    async def receive(source_id: str, request: Request) -> Response:
        if not credentials:
            raise HTTPException(404, "Heroku intake is not configured")
        if request.url.scheme != "https":
            raise HTTPException(400, "Heroku intake requires HTTPS")
        credential = credentials.get(source_id)
        _authorize(request, credential)
        assert credential is not None
        token = _single_header(request, "logplex-drain-token")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", token) is None:
            raise HTTPException(400, "invalid Logplex-Drain-Token")
        token_digest = hashlib.sha256(token.encode()).digest()
        if credential.token_digest is not None and not hmac.compare_digest(
            token_digest, credential.token_digest
        ):
            raise HTTPException(401, "invalid drain credentials")
        if _single_header(request, "content-type").lower() != "application/logplex-1":
            raise HTTPException(415, "application/logplex-1 is required")
        frame_id = _single_header(request, "logplex-frame-id")
        count_value = _single_header(request, "logplex-msg-count")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", frame_id) is None:
            raise HTTPException(400, "invalid Logplex-Frame-Id")
        if re.fullmatch(r"[0-9]{1,5}", count_value) is None:
            raise HTTPException(400, "invalid Logplex-Msg-Count")
        count = int(count_value)
        if not 1 <= count <= MAX_MESSAGES:
            raise HTTPException(400, "invalid Logplex-Msg-Count")
        length = _single_header(request, "content-length")
        if re.fullmatch(r"[0-9]{1,7}", length) is None or int(length) > MAX_BODY_BYTES:
            raise HTTPException(413, "Logplex body exceeds size limit")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise HTTPException(413, "Logplex body exceeds size limit")
            body.extend(chunk)
        if len(body) != int(length):
            raise HTTPException(400, "Content-Length does not match body")

        def store() -> None:
            result = "interrupted"
            try:
                messages = parse_logplex(bytes(body))
                if len(messages) != count:
                    raise ValueError("Logplex message count mismatch")
                # Validate every projection before the first chunk commits.
                events = logplex_inputs(messages, frame_id)
                with state._lock:
                    source = state.source(source_id)
                    if source.service_id != credential.binding.service_id:
                        raise HTTPException(403, "drain service binding does not match source")
                    state.control.service(source.service_id)
                    if source.kind != "push" or not source.enabled:
                        raise HTTPException(400, "drain requires an enabled push source")
                    for start in range(0, len(events), 500):
                        state.collector.ingest(
                            source_id,
                            events[start : start + 500],
                            originals=messages[start : start + 500],
                        )
                result = "accepted"
            except DuplicateConflict:
                result = "conflict"
                raise HTTPException(409, "frame conflicts with retained original bytes") from None
            except ValueError:
                result = "rejected"
                raise HTTPException(400, "invalid Logplex frame or event") from None
            except (HTTPException, KeyError):
                result = "rejected"
                raise
            finally:
                state.control.audit(
                    "heroku-drain",
                    "source.heroku.receive",
                    source_id,
                    json.dumps(
                        {
                            "frame_id": frame_id,
                            "drain_token_sha256": token_digest.hex(),
                            "message_count": count,
                            "byte_count": len(body),
                            "result": result,
                        }
                    ),
                    service_id=credential.binding.service_id,
                    object_kind="source",
                )

        await run_in_threadpool(store)
        # Durable intake only: the existing worker handles interpretation and findings.
        return Response(status_code=204, headers={"Content-Length": "0"})
