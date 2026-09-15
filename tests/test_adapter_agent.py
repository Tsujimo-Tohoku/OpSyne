"""Offline adapter drafting, scope checks and safe declarative conversion."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import OpenAI
from pydantic import JsonValue, ValidationError

from opsyne.agents.adapter import AdapterAgent
from opsyne.agents.investigator import InvestigationError
from opsyne.contracts.adapter_proposals import AdapterProposal
from opsyne.contracts.cases import Task
from opsyne.contracts.core import Service
from opsyne.contracts.observations import Source


@pytest.fixture(autouse=True)
def stable_sdk_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openai._base_client.get_platform", lambda: "Unknown")
    monkeypatch.setattr("openai._base_client.get_architecture", lambda: "unknown")


def source() -> Source:
    return Source(id="source-1", service_id="service-1", name="Audit", kind="push")


def service() -> Service:
    return Service(
        id="service-1", name="Service", instance_id="instance-1", version=3, owner="owner"
    )


def task(**updates: object) -> Task:
    return Task.model_validate(
        {
            "id": "task-1",
            "case_id": "case-1",
            "service_id": "service-1",
            "role": "adapter",
            "status": "RUNNING",
            "created_at": time.time(),
            "expires_at": time.time() + 120,
            **updates,
        }
    )


def evidence() -> list[dict[str, JsonValue]]:
    return [
        {
            "id": "raw-1",
            "service_id": "service-1",
            "source_id": "source-1",
            "payload": '{"result":"code-7","message":"uncertain"}',
            "truncated": False,
        }
    ]


def proposal(**updates: object) -> dict[str, Any]:
    return {
        "name": "Unknown audit format",
        "fields": [{"field": "outcome", "path": "result"}, {"field": "message", "path": "message"}],
        "conditions": [],
        "outcome_map": [{"input": "code-7", "output": "UNKNOWN"}],
        "severity_map": [],
        "rationale": [
            {"text": "result code semantics are not documented", "evidence_ids": ["raw-1"]}
        ],
        "unknowns": ["code-7 business meaning is unknown"],
        **updates,
    }


def response(body: dict[str, Any], **updates: object) -> dict[str, Any]:
    return {
        "id": "resp_adapter",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-5.6-luna",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "message",
                "id": "msg_adapter",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "annotations": [], "text": json.dumps(body)}],
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
        api_key="test-only",
        base_url="https://api.openai.com/v1/",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_draft_uses_array_schema_and_trusted_registration_without_activation() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response(proposal()))

    with client(handle) as sdk:
        result = AdapterAgent(None, client=sdk).propose(task(), evidence(), source(), service())
    assert result.unknowns
    definition = result.to_definition(id="draft-1", source=source(), service=service())
    assert definition.target_instance_id == "instance-1"
    assert definition.target_version == 3
    assert definition.outcome_map["code-7"] == "UNKNOWN"
    assert "approved" not in definition.model_dump()
    assert not hasattr(result, "activate")
    request = json.loads(requests[0].content)
    assert request["model"] == "gpt-5.6-luna"
    assert request["store"] is False
    assert request["reasoning"] == {"effort": "low"}
    assert request["max_output_tokens"] == 2500
    assert "tools" not in request
    schema = request["text"]["format"]["schema"]
    assert schema["properties"]["fields"]["type"] == "array"
    assert schema["properties"]["outcome_map"]["type"] == "array"
    assert schema["additionalProperties"] is False
    for definition_schema in schema["$defs"].values():
        if definition_schema.get("type") == "object":
            assert definition_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "invalid",
    [
        {"fields": [{"field": "message", "path": "eval('code')"}]},
        {"code": "import os"},
        {"activate": True},
        {"rationale": [{"text": "claimed approval", "evidence_ids": ["outside"]}]},
        {"rationale": [{"text": "unfounded", "evidence_ids": []}]},
        {"fields": [{"field": "message", "path": "x"}, {"field": "message", "path": "y"}]},
        {"outcome_map": [{"input": "code-7", "output": "APPROVED"}]},
    ],
)
def test_rejects_code_malformed_path_duplicates_and_unsupported_evidence(
    invalid: dict[str, object],
) -> None:
    with (
        client(lambda _: httpx.Response(200, json=response(proposal(**invalid)))) as sdk,
        pytest.raises(InvestigationError),
    ):
        AdapterAgent(None, client=sdk).propose(task(), evidence(), source(), service())


def test_agent_can_withhold_mapping_when_meaning_or_structure_is_unknown() -> None:
    withheld = proposal(fields=[], outcome_map=[], unknowns=["No safe structural mapping found"])
    with client(lambda _: httpx.Response(200, json=response(withheld))) as sdk:
        result = AdapterAgent(None, client=sdk).propose(task(), evidence(), source(), service())
    assert result.fields == []
    with pytest.raises(ValueError, match="withheld"):
        result.to_definition("draft", source(), service())


def test_scope_role_deadline_and_evidence_budget_are_checked_before_api() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response(proposal()))

    with client(handle) as sdk:
        agent = AdapterAgent(None, client=sdk)
        for invalid_task in [task(role="operator"), task(status="PENDING"), task(expires_at=0)]:
            with pytest.raises(InvestigationError):
                agent.propose(invalid_task, evidence(), source(), service())
        wrong = evidence()
        wrong[0]["source_id"] = "another-source"
        with pytest.raises(InvestigationError, match="scope"):
            agent.propose(task(), wrong, source(), service())
        too_large = evidence()
        too_large[0]["payload"] = "x" * 40001
        with pytest.raises(InvestigationError, match="budget"):
            agent.propose(task(), too_large, source(), service())
        with pytest.raises(InvestigationError, match="scope"):
            agent.propose(task(service_id="other"), evidence(), source(), service())
    assert calls == 0
    with pytest.raises(InvestigationError, match="API key"):
        AdapterAgent(None).propose(task(), evidence(), source(), service())


@pytest.mark.parametrize("failure", ["refusal", "incomplete", "parse", "timeout"])
def test_rejects_refusal_incomplete_parse_failure_and_timeout_without_retry(failure: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("private diagnostic", request=request)
        body = response(proposal())
        if failure == "refusal":
            body["output"][0]["content"] = [{"type": "refusal", "refusal": "declined"}]
        elif failure == "incomplete":
            body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        elif failure == "parse":
            body["output"][0]["content"][0]["text"] = "malformed"
        return httpx.Response(200, json=body)

    with client(handle) as sdk, pytest.raises(InvestigationError) as error:
        AdapterAgent(None, client=sdk).propose(task(), evidence(), source(), service())
    assert calls == 1
    assert "private diagnostic" not in str(error.value)


def test_pure_conversion_rejects_mismatched_registration_and_invalid_conditions() -> None:
    parsed = AdapterProposal.model_validate(proposal())
    with pytest.raises(ValueError, match="identity"):
        parsed.to_definition(
            "draft", source().model_copy(update={"service_id": "other"}), service()
        )
    with pytest.raises(ValidationError):
        AdapterProposal.model_validate(proposal(conditions=[{"path": "x", "value": {"code": "y"}}]))
