"""Source-bound external intake, byte identity, replay and deterministic downstream flow."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from opsyne.api.app import create_app
from opsyne.api.heroku import load_drains
from opsyne.collector.service import Collector
from opsyne.contracts.core import Service
from opsyne.contracts.observations import RawEvent, RawInput, Source
from opsyne.runtime import Runtime

PASSWORD = "synthetic-drain-password-0123456789"
TOKEN = "d.synthetic-drain-token"
ENDPOINT = "/api/drains/heroku/heroku-app"
ERROR_LOG = (
    b"<40>1 2026-09-15T04:00:00Z host app web.1 - "
    b'{"level":"ERROR","result":"FAILURE","message":"database connection failed"}'
)


def frame(*messages: bytes) -> bytes:
    return b"".join(str(len(message)).encode() + b" " + message for message in messages)


def headers(count: int = 1, frame_id: str = "frame-1") -> dict[str, str]:
    return {
        "Authorization": "Basic " + base64.b64encode(f"heroku:{PASSWORD}".encode()).decode(),
        "Content-Type": "application/logplex-1",
        "Logplex-Msg-Count": str(count),
        "Logplex-Frame-Id": frame_id,
        "Logplex-Drain-Token": TOKEN,
    }


def actor_headers(runtime: Runtime, name: str) -> dict[str, str]:
    entries = json.loads((runtime.data_dir / "tokens.json").read_text(encoding="utf-8"))
    return {"Authorization": "Bearer " + next(x["token"] for x in entries if x["actor"] == name)}


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    for name in ("OPENAI_API_KEY", "OPSYNE_ALLOWED_HOSTS", "OPSYNE_PORT", "PORT", "OPSYNE_HOST"):
        monkeypatch.delenv(name, raising=False)
    bindings = tmp_path / "drains.json"
    bindings.write_text(
        json.dumps(
            [
                {
                    "source_id": "heroku-app",
                    "service_id": "service-a",
                    "username": "heroku",
                    "password_env": "TEST_DRAIN_PASSWORD",
                    "drain_token_env": "TEST_DRAIN_TOKEN",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPSYNE_HEROKU_DRAINS_FILE", str(bindings))
    monkeypatch.setenv("TEST_DRAIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("TEST_DRAIN_TOKEN", TOKEN)
    state = Runtime(tmp_path / "data", api_key=None)
    for name in ("a", "b"):
        state.control.register_service(
            Service(
                id=f"service-{name}", name=name, instance_id=f"{name}-production", owner="owner"
            ),
            "owner",
        )
    state.collector.register_source(Source(id="heroku-app", service_id="service-a", name="Heroku"))
    state.control.bind_source_scope("heroku-app", "service-a")
    return state


@pytest.fixture
def client(configured: Runtime) -> Iterator[TestClient]:
    with TestClient(
        create_app(runtime=configured, background=False), base_url="https://testserver"
    ) as test_client:
        yield test_client


def test_exact_bytes_and_replay_survive_restart(client: TestClient, configured: Runtime) -> None:
    raw = ERROR_LOG + " 日本語".encode()
    body = frame(raw, b"\xff\xfe")
    first = client.post(ENDPOINT, headers=headers(2), content=body)
    assert first.status_code == 204, first.text
    assert first.content == b"" and first.headers["content-length"] == "0"
    assert "transfer-encoding" not in first.headers
    retained = configured.collector.search(source_id="heroku-app")
    assert len(retained) == 2
    event = next(item for item in retained if item.external_id.endswith(":0"))
    assert base64.b64decode(event.raw_bytes_b64) == raw
    assert event.digest == hashlib.sha256(raw).hexdigest()
    invalid = next(item for item in retained if item.external_id.endswith(":1"))
    assert invalid.encoding_error
    assert base64.b64decode(invalid.raw_bytes_b64) == b"\xff\xfe"
    # Repeated input must keep the first saved projection and evidence identity.
    restarted_app = create_app(configured.data_dir, background=False)
    with TestClient(restarted_app, base_url="https://testserver") as restarted:
        assert restarted.post(ENDPOINT, headers=headers(2), content=body).status_code == 204
        state = cast(Runtime, restarted_app.state.runtime)
        assert {item.id for item in state.collector.search()} == {item.id for item in retained}
    configured.poll()
    interpretation = configured.control.event(invalid.id)
    assert interpretation is not None and interpretation["parse_status"] == "INVALID"


def test_conflict_compares_original_bytes_not_replacement_text(
    client: TestClient, configured: Runtime
) -> None:
    assert client.post(ENDPOINT, headers=headers(), content=frame(b"\xff")).status_code == 204
    assert client.post(ENDPOINT, headers=headers(), content=frame(b"\xfe")).status_code == 409
    assert len(configured.collector.search()) == 1


@pytest.mark.parametrize(
    ("name", "value", "status"),
    [
        ("Authorization", None, 401),
        ("Authorization", "Basic invalid!", 401),
        ("Logplex-Drain-Token", "d.other", 401),
        ("Logplex-Frame-Id", None, 400),
        ("Logplex-Frame-Id", "a" * 129, 400),
        ("Logplex-Msg-Count", "2", 400),
        ("Logplex-Msg-Count", "0", 400),
        ("Logplex-Msg-Count", "-1", 400),
        ("Content-Type", "application/json", 415),
        ("Content-Length", "2000001", 413),
        ("Content-Length", "1", 400),
        ("Transfer-Encoding", "chunked", 411),
        ("Host", "attacker.example", 400),
        ("Origin", "https://attacker.example", 403),
    ],
)
def test_invalid_requests_do_not_store(
    client: TestClient, configured: Runtime, name: str, value: str | None, status: int
) -> None:
    supplied = headers()
    if value is None:
        supplied.pop(name)
    else:
        supplied[name] = value
    response = client.post(ENDPOINT, headers=supplied, content=frame(ERROR_LOG))
    assert response.status_code == status, response.text
    assert configured.collector.search() == []


def test_duplicate_headers_and_actual_body_size_are_rejected(
    client: TestClient, configured: Runtime
) -> None:
    supplied = [*headers().items(), ("Logplex-Frame-Id", "frame-2")]
    assert client.post(ENDPOINT, headers=supplied, content=frame(ERROR_LOG)).status_code == 400
    supplied = [*headers().items(), ("Content-Length", "1")]
    assert client.post(ENDPOINT, headers=supplied, content=b"a" * 2_000_001).status_code == 413
    assert configured.collector.search() == []


def test_credentials_cannot_read_or_change_other_sources(
    client: TestClient, configured: Runtime
) -> None:
    configured.collector.register_source(Source(id="other", service_id="service-b", name="other"))
    assert client.get("/api/overview", headers=headers()).status_code == 401
    assert client.post("/api/poll", headers=headers()).status_code == 401
    assert (
        client.post(
            "/api/drains/heroku/other", headers=headers(), content=frame(ERROR_LOG)
        ).status_code
        == 401
    )
    assert (
        client.post(
            ENDPOINT,
            headers={**headers(), **actor_headers(configured, "owner")},
            content=frame(ERROR_LOG),
        ).status_code
        == 401
    )
    assert configured.collector.search() == []


def test_binding_mismatch_disabled_and_missing_source_rejected(
    client: TestClient, configured: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = configured.source("heroku-app")
    for replacement, status in (
        (source.model_copy(update={"service_id": "service-b"}), 403),
        (source.model_copy(update={"enabled": False}), 400),
        (source.model_copy(update={"kind": "file", "path": "unused"}), 400),
    ):
        monkeypatch.setattr(configured, "source", lambda identifier, result=replacement: result)
        assert (
            client.post(ENDPOINT, headers=headers(), content=frame(ERROR_LOG)).status_code == status
        )

    def missing(identifier: str) -> Source:
        raise KeyError(identifier)

    monkeypatch.setattr(configured, "source", missing)
    assert client.post(ENDPOINT, headers=headers(), content=frame(ERROR_LOG)).status_code == 404
    assert configured.collector.search() == []


def test_malformed_later_message_is_rejected_before_first_commit(
    client: TestClient, configured: Runtime
) -> None:
    body = frame(*([ERROR_LOG] * 500)) + b"100 short"
    assert client.post(ENDPOINT, headers=headers(501), content=body).status_code == 400
    assert configured.collector.search() == []


def test_partial_batch_failure_can_replay_without_duplicates(
    client: TestClient, configured: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = frame(*([ERROR_LOG] * 501))
    original = configured.collector.ingest
    calls = 0

    def fail_second(
        source_id: str,
        events: Sequence[RawInput],
        now: float | None = None,
        *,
        originals: Sequence[bytes] | None = None,
    ) -> list[RawEvent]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("simulated storage interruption")
        return original(source_id, events, now, originals=originals)

    monkeypatch.setattr(configured.collector, "ingest", fail_second)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        client.post(ENDPOINT, headers=headers(501), content=body)
    assert json.loads(configured.control.audit_log()[0]["detail"])["result"] == "interrupted"
    assert len(configured.collector.search(limit=1000)) == 500
    monkeypatch.setattr(configured.collector, "ingest", original)
    assert client.post(ENDPOINT, headers=headers(501), content=body).status_code == 204
    assert len(configured.collector.search(limit=1000)) == 501
    assert len({event.id for event in configured.collector.pending(1000)}) == 501


def test_secrets_are_absent_from_audit_and_responses(
    client: TestClient, configured: Runtime
) -> None:
    response = client.post(ENDPOINT, headers=headers(), content=frame(ERROR_LOG))
    assert response.status_code == 204
    history = configured.control.scoped_history()
    record = next(item for item in history if item["action"] == "source.heroku.receive")
    assert record["service_id"] == "service-a"
    assert PASSWORD not in json.dumps(history) + response.text
    assert TOKEN not in json.dumps(history) + response.text
    assert (
        json.loads(record["detail"])["drain_token_sha256"]
        == hashlib.sha256(TOKEN.encode()).hexdigest()
    )


def test_dedicated_intake_requires_https(client: TestClient, configured: Runtime) -> None:
    response = client.post(
        "http://testserver" + ENDPOINT,
        headers={**headers(), "X-Forwarded-Proto": "https"},
        content=frame(ERROR_LOG),
    )
    assert response.status_code == 400
    assert configured.collector.search() == []


def test_receiver_is_disabled_without_explicit_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPSYNE_HEROKU_DRAINS_FILE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(
        create_app(tmp_path, background=False), base_url="https://testserver"
    ) as client:
        assert client.post(ENDPOINT, headers=headers(), content=frame(ERROR_LOG)).status_code == 404


def test_collector_rejects_mismatched_originals_without_partial_save(tmp_path: Path) -> None:
    collector = Collector(tmp_path / "collector.db")
    collector.register_source(Source(id="source", service_id="service", name="source"))
    with pytest.raises(ValueError, match="original byte count"):
        collector.ingest(
            "source", [RawInput(external_id="one", payload="projection")], originals=[]
        )
    assert collector.search() == []


def test_invalid_drain_configuration_does_not_leak_values(
    configured: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DRAIN_PASSWORD", "short-secret")
    with pytest.raises(ValueError) as error:
        load_drains()
    assert "short-secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_source_credentials_remain_isolated_when_multiple_drains_are_configured(
    configured: Runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "drains.json"
    bindings = json.loads(path.read_text(encoding="utf-8"))
    bindings.append(
        {**bindings[0], "source_id": "other", "service_id": "service-b", "username": "other"}
    )
    path.write_text(json.dumps(bindings), encoding="utf-8")
    # Even a different username cannot make a reused password source-specific.
    with pytest.raises(ValueError):
        load_drains()
    other_password = "other-synthetic-password-0123456789"
    bindings[1]["password_env"] = "TEST_OTHER_PASSWORD"
    monkeypatch.setenv("TEST_OTHER_PASSWORD", other_password)
    path.write_text(json.dumps(bindings), encoding="utf-8")
    configured.collector.register_source(Source(id="other", service_id="service-b", name="other"))
    with TestClient(
        create_app(runtime=configured, background=False), base_url="https://testserver"
    ) as client:
        assert (
            client.post(
                "/api/drains/heroku/other", headers=headers(), content=frame(ERROR_LOG)
            ).status_code
            == 401
        )
        other_headers = {
            **headers(),
            "Authorization": "Basic "
            + base64.b64encode(f"other:{other_password}".encode()).decode(),
        }
        assert (
            client.post(
                "/api/drains/heroku/other", headers=other_headers, content=frame(ERROR_LOG)
            ).status_code
            == 204
        )
    events = configured.collector.search()
    assert len(events) == 1 and events[0].service_id == "service-b"


def test_basic_only_bootstrap_requires_a_valid_token_header(
    configured: Runtime, tmp_path: Path
) -> None:
    path = tmp_path / "drains.json"
    bindings = json.loads(path.read_text(encoding="utf-8"))
    del bindings[0]["drain_token_env"]
    path.write_text(json.dumps(bindings), encoding="utf-8")
    with TestClient(
        create_app(runtime=configured, background=False), base_url="https://testserver"
    ) as client:
        supplied = headers()
        del supplied["Logplex-Drain-Token"]
        assert client.post(ENDPOINT, headers=supplied, content=frame(ERROR_LOG)).status_code == 400
        supplied["Logplex-Drain-Token"] = "d.initial-token"
        assert client.post(ENDPOINT, headers=supplied, content=frame(ERROR_LOG)).status_code == 204


def test_received_application_json_enters_existing_approval_and_detection_flow(
    client: TestClient, configured: Runtime
) -> None:
    assert client.post(ENDPOINT, headers=headers(), content=frame(ERROR_LOG)).status_code == 204
    raw = configured.collector.search()[0]
    assert configured.collector.pending()[0].id == raw.id
    configured.poll()
    initial = configured.control.event(raw.id)
    assert initial is not None and initial["parse_status"] == "UNKNOWN"
    definition: dict[str, Any] = {
        "id": "heroku-json-v1",
        "name": "Application failures",
        "source_id": "heroku-app",
        "target_instance_id": "a-production",
        "target_version": 1,
        "version": 1,
        "fields": {"severity": "app.level", "outcome": "app.result", "message": "app.message"},
        "conditions": {"transport": "heroku_logplex_v1", "decode_status": "json_object"},
        "severity_map": {"ERROR": "ERROR"},
        "outcome_map": {"FAILURE": "FAILURE"},
    }
    proposed = client.post(
        "/api/adapters", headers=actor_headers(configured, "operator"), json=definition
    )
    assert proposed.status_code == 200, proposed.text
    draft = proposed.json()
    approved = client.post(
        "/api/adapters/heroku-json-v1/approve",
        headers=actor_headers(configured, "reviewer"),
        json={"digest": draft["digest"]},
    )
    assert approved.status_code == 200, approved.text
    configured.reprocess("heroku-app")
    interpretation = configured.control.event(raw.id)
    assert interpretation is not None
    assert interpretation["severity"] == "ERROR" and interpretation["outcome"] == "FAILURE"
    assert not any(
        case["kind"] == "operation_failure" for case in configured.control.objects("case")
    )
    assert (
        client.post(
            ENDPOINT, headers=headers(frame_id="new-frame"), content=frame(ERROR_LOG)
        ).status_code
        == 204
    )
    configured.poll()
    cases = configured.control.objects("case")
    assert any(case["kind"] == "operation_failure" for case in cases)
    assert configured.control.objects("plan") == []
    assert configured.execution_list() == []
