"""Offline checks of evidence authorization and the actual Responses SDK wire format."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest
from openai import OpenAI
from pydantic import JsonValue

from opsyne.agents.investigator import InvestigationError, Investigator
from opsyne.contracts.cases import Analysis, EvidenceGrant, Task
from opsyne.contracts.observations import RawEvent
from opsyne.gateway.service import EvidenceAccessError, EvidenceGateway


@pytest.fixture(autouse=True)
def stable_sdk_platform_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not query Windows WMI for optional SDK telemetry in offline request tests."""
    monkeypatch.setattr("openai._base_client.get_platform", lambda: "Unknown")
    monkeypatch.setattr("openai._base_client.get_architecture", lambda: "unknown")


def event(payload: str, identifier: str = "event-1", service_id: str = "service-1") -> RawEvent:
    encoded = payload.encode()
    return RawEvent(
        id=identifier,
        source_id="source-1",
        service_id=service_id,
        external_id=identifier,
        payload=payload,
        received_at=10,
        digest=hashlib.sha256(encoded).hexdigest(),
        raw_bytes_b64=base64.b64encode(encoded).decode(),
    )


def grant(**updates: object) -> EvidenceGrant:
    return EvidenceGrant.model_validate(
        {
            "task_id": "task-1",
            "case_id": "case-1",
            "service_id": "service-1",
            "role": "operator",
            "evidence_ids": ["event-1"],
            "expires_at": time.time() + 120,
            **updates,
        }
    )


def task(**updates: object) -> Task:
    return Task.model_validate(
        {
            "id": "task-1",
            "case_id": "case-1",
            "service_id": "service-1",
            "status": "RUNNING",
            "created_at": time.time(),
            "expires_at": time.time() + 120,
            **updates,
        }
    )


def evidence() -> list[dict[str, JsonValue]]:
    return [
        {
            "id": "event-1",
            "service_id": "service-1",
            "source_id": "source-1",
            "payload": "observed failure",
            "truncated": False,
        }
    ]


def analysis(**updates: object) -> dict[str, Any]:
    return {
        "summary": "要調査",
        "facts": [{"text": "failure observed", "evidence_ids": ["event-1"]}],
        "hypotheses": [],
        "unknowns": ["cause unknown"],
        "recommendations": ["investigate"],
        **updates,
    }


def response(body: dict[str, Any] | None = None, **updates: object) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-5.6-luna",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "text": json.dumps(body if body is not None else analysis()),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 10},
        },
        **updates,
    }


def client(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAI:
    return OpenAI(
        api_key="test-only-not-a-real-secret",
        base_url="https://api.openai.com/v1/",
        max_retries=0,
        timeout=30,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_gateway_scopes_before_fetch_and_checks_current_task() -> None:
    fetched: list[str] = []
    validated: list[str] = []

    def fetch(identifier: str) -> RawEvent:
        fetched.append(identifier)
        return event("safe", identifier)

    gateway = EvidenceGateway(fetch, lambda current: validated.append(current.task_id))
    assert gateway.retrieve(grant())[0]["payload"] == "safe"
    assert fetched == ["event-1"]
    assert len(validated) == 2
    with pytest.raises(EvidenceAccessError, match="expired"):
        gateway.retrieve(grant(expires_at=0))
    with pytest.raises(EvidenceAccessError, match="item budget"):
        gateway.retrieve(grant(evidence_ids=["event-1", "event-2"], max_items=1))
    with pytest.raises(EvidenceAccessError, match="invalid"):
        gateway.retrieve(grant().model_copy(update={"role": "admin"}))
    assert fetched == ["event-1"]

    def revoked(_: EvidenceGrant) -> None:
        raise EvidenceAccessError("task revoked")

    with pytest.raises(EvidenceAccessError, match="revoked"):
        EvidenceGateway(fetch, revoked).retrieve(grant())
    assert fetched == ["event-1"]


def test_gateway_rejects_wrong_service_and_missing_evidence() -> None:
    with pytest.raises(EvidenceAccessError, match="outside"):
        EvidenceGateway(lambda _: event("safe", service_id="other"), lambda _: None).retrieve(
            grant()
        )
    with pytest.raises(EvidenceAccessError, match="unavailable"):
        EvidenceGateway(lambda _: None, lambda _: None).retrieve(grant())


def test_gateway_redacts_recursively_without_changing_originals() -> None:
    original = event(
        json.dumps(
            {
                "password": "p-secret",
                "nested": [{"api_key": "key-secret", "refreshToken": "t-secret"}],
                "message": "password=inline-secret and normal text",
                "normal": 42,
            }
        )
    )
    result = EvidenceGateway(lambda _: original, lambda _: None).retrieve(grant())
    serialized = json.dumps(result)
    for secret in ["p-secret", "key-secret", "t-secret", "inline-secret"]:
        assert secret not in serialized
    assert "p-secret" in original.payload
    assert "normal" in serialized
    text = event("Authorization: Bearer credential-value\napi_key=secret123 password='hello there'")
    result = EvidenceGateway(lambda _: text, lambda _: None).retrieve(grant())
    assert "credential-value" not in json.dumps(result)
    assert "secret123" not in json.dumps(result)
    assert "hello there" not in json.dumps(result)


def test_gateway_truncates_after_masking_with_aggregate_json_budget() -> None:
    value = event("password=privatevalue " + "long \n" * 1000)
    result = EvidenceGateway(lambda _: value, lambda _: None).retrieve(grant(max_characters=256))
    assert result[0]["truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 256
    assert "privatevalue" not in str(result)


def test_investigator_sends_structured_tool_free_request_to_exact_model() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response())

    with client(handle) as sdk:
        result = Investigator(None, client=sdk).investigate(task(), evidence())
    assert isinstance(result, Analysis)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.openai.com/v1/responses"
    body = json.loads(request.content)
    assert body["model"] == "gpt-5.6-luna"
    assert body["store"] is False
    assert body["max_output_tokens"] == 2500
    assert body["reasoning"] == {"effort": "low"}
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert "tools" not in body
    assert [item["role"] for item in body["input"]] == ["developer", "user"]
    assert "untrusted" in body["input"][0]["content"]


@pytest.mark.parametrize(
    "kind", ["refusal", "incomplete", "invalid", "foreign", "no_evidence", "budget"]
)
def test_investigator_rejects_unsafe_or_unusable_outputs(kind: str) -> None:
    output = response()
    if kind == "refusal":
        output["output"][0]["content"] = [{"type": "refusal", "refusal": "cannot comply"}]
    elif kind == "incomplete":
        output.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    elif kind == "invalid":
        output["output"][0]["content"][0]["text"] = "not JSON"
    elif kind == "foreign":
        output = response(analysis(facts=[{"text": "invented", "evidence_ids": ["other-event"]}]))
    elif kind == "no_evidence":
        output = response(analysis(facts=[{"text": "unsupported", "evidence_ids": []}]))
    elif kind == "budget":
        output["usage"]["output_tokens"] = 2501
    with (
        client(lambda _: httpx.Response(200, json=output)) as sdk,
        pytest.raises(InvestigationError),
    ):
        Investigator(None, client=sdk).investigate(task(), evidence())


def test_investigator_missing_key_expiry_input_budget_and_timeout() -> None:
    with pytest.raises(InvestigationError, match="API key"):
        Investigator(None).investigate(task(), evidence())
    calls = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private response detail", request=request)

    with client(timeout) as sdk:
        investigator = Investigator(None, client=sdk)
        with pytest.raises(InvestigationError, match="expired"):
            investigator.investigate(task(expires_at=0), evidence())
        large = evidence()
        large[0]["payload"] = "x" * 40001
        with pytest.raises(InvestigationError, match="character budget"):
            investigator.investigate(task(), large)
        assert calls == 0
        with pytest.raises(InvestigationError) as error:
            investigator.investigate(task(), evidence())
        assert "private response detail" not in str(error.value)
        assert calls == 1


def test_investigator_rejects_model_substitution_and_endpoint_override() -> None:
    with pytest.raises(ValueError, match=r"gpt-5\.6-luna"):
        Investigator(None, model="different-model")
    with (
        OpenAI(api_key="test", base_url="https://example.invalid/v1") as sdk,
        pytest.raises(ValueError, match="official"),
    ):
        Investigator(None, client=sdk)
