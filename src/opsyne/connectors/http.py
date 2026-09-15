"""HTTP operations use only an owner's registered capability configuration."""

from __future__ import annotations

import os
import time

import httpx

from opsyne.contracts.execution import (
    Capability,
    CheckConfig,
    OperationResult,
    VerificationResult,
    validate_endpoint,
)


class HttpConnector:
    def __init__(
        self,
        timeout: float = 5,
        max_response_bytes: int = 65536,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not 0 < timeout <= 10 or not 0 < max_response_bytes <= 1048576:
            raise ValueError("HTTP timeout must be <=10s and response limit <=1MiB")
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    @staticmethod
    def _headers(auth_env: str | None) -> dict[str, str]:
        if auth_env is None:
            return {}
        secret = os.environ.get(auth_env)
        if not secret or "\r" in secret or "\n" in secret:
            raise ValueError("Configured connector credential is unavailable or invalid")
        return {"Authorization": f"Bearer {secret}"}

    def _read(self, response: httpx.Response, deadline: float) -> bytes:
        chunks = bytearray()
        # Check every transport chunk so a slow trickle cannot reset the read
        # timeout forever while waiting for a large application-level chunk.
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise TimeoutError("Response exceeded its time budget")
            if len(chunks) + len(chunk) > self._max_response_bytes:
                raise ValueError("Response exceeds configured size limit")
            chunks.extend(chunk)
        if time.monotonic() >= deadline:
            raise TimeoutError("Response exceeded its time budget")
        return bytes(chunks)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    def execute(self, capability: Capability, operation_id: str) -> OperationResult:
        if capability.kind != "http.request" or capability.endpoint is None:
            raise ValueError("HTTP connector requires an HTTP capability")
        validate_endpoint(capability.endpoint)
        try:
            headers = self._headers(capability.auth_env)
        except ValueError:
            return OperationResult(
                status="FAILED", detail="Credential unavailable; no request sent"
            )
        headers["Idempotency-Key"] = operation_id
        deadline = time.monotonic() + self._timeout
        try:
            with (
                self._client() as client,
                client.stream(
                    capability.method,
                    capability.endpoint,
                    json=capability.body,
                    headers=headers,
                ) as response,
            ):
                self._read(response, deadline)
                status = response.status_code
        except Exception:
            return OperationResult(
                status="UNKNOWN", detail="HTTP outcome is unknown; no automatic retry is allowed"
            )
        if 200 <= status < 300:
            return OperationResult(
                status="SUCCEEDED",
                detail=f"HTTP {status} accepted; independent verification required",
            )
        if 400 <= status < 500 and status != 408:
            return OperationResult(status="FAILED", detail=f"HTTP {status} rejected the operation")
        return OperationResult(
            status="UNKNOWN", detail=f"HTTP {status}; operation outcome is unknown"
        )

    def lookup(self, operation_id: str) -> OperationResult:
        # Idempotency-Key support is not assumed merely because the header was sent.
        return OperationResult(
            status="UNKNOWN",
            detail="This HTTP capability has no authoritative operation-history lookup",
        )

    def check(self, config: CheckConfig) -> VerificationResult:
        if config.kind != "http" or config.endpoint is None:
            raise ValueError("HTTP check requires an HTTP observation endpoint")
        validate_endpoint(config.endpoint)
        deadline = time.monotonic() + self._timeout
        try:
            headers = self._headers(config.auth_env)
            with (
                self._client() as client,
                client.stream("GET", config.endpoint, headers=headers) as response,
            ):
                body = self._read(response, deadline)
                status = response.status_code
            if (
                status in {401, 403, 407, 408, 429} or 300 <= status < 400
            ) and status != config.expected_status:
                return VerificationResult(
                    status="UNKNOWN",
                    detail="HTTP observation was blocked, redirected, or rate limited",
                    evidence={"http_status": status},
                    checked_at=time.time(),
                )
            body_matches = config.body_contains is None or config.body_contains in body.decode(
                "utf-8", errors="replace"
            )
            passed = status == config.expected_status and body_matches
            return VerificationResult(
                status="PASS" if passed else "FAIL",
                detail="Independent HTTP observation matched"
                if passed
                else "HTTP observation did not match",
                evidence={"http_status": status, "body_condition_matched": body_matches},
                checked_at=time.time(),
            )
        except Exception:
            return VerificationResult(
                status="UNKNOWN",
                detail="HTTP observation is unavailable or incomplete",
                checked_at=time.time(),
            )
