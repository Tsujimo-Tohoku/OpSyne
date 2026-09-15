"""External bind and reverse-proxy settings must be explicit and preserve local defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opsyne import cli
from opsyne.api.app import create_app
from opsyne.server_config import ServerSettings


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPSYNE_HOST",
        "OPSYNE_PORT",
        "PORT",
        "OPSYNE_ALLOWED_HOSTS",
        "OPSYNE_TRUSTED_PROXIES",
        "OPSYNE_HEROKU_DRAINS_FILE",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_and_cli_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = ServerSettings.from_env()
    assert settings.host == "127.0.0.1" and settings.port == 8765
    assert settings.allowed_hosts == ["localhost", "127.0.0.1", "testserver"]
    monkeypatch.setenv("PORT", "8080")
    assert ServerSettings.from_env().port == 8080
    monkeypatch.setenv("OPSYNE_PORT", "9000")
    monkeypatch.setenv("OPSYNE_HOST", "0.0.0.0")
    assert ServerSettings.from_env().port == 9000
    assert ServerSettings.from_env().host == "0.0.0.0"
    assert ServerSettings.from_env(host="127.0.0.1", port=8766).port == 8766


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPSYNE_HOST", "https://example.com"),
        ("PORT", "no"),
        ("PORT", "65536"),
        ("PORT", "0"),
        ("OPSYNE_ALLOWED_HOSTS", "*"),
        ("OPSYNE_ALLOWED_HOSTS", "https://example.com"),
        ("OPSYNE_ALLOWED_HOSTS", "example.com:443"),
        ("OPSYNE_ALLOWED_HOSTS", ""),
        ("OPSYNE_TRUSTED_PROXIES", "*"),
        ("OPSYNE_TRUSTED_PROXIES", "0.0.0.0/0"),
    ],
)
def test_invalid_exposure_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        ServerSettings.from_env()


def test_external_host_keeps_origin_and_authentication_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPSYNE_ALLOWED_HOSTS", "opsyne.example")
    with TestClient(
        create_app(tmp_path, background=False), base_url="https://opsyne.example"
    ) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/overview").status_code == 401
        assert client.get("/health", headers={"Host": "unknown.example"}).status_code == 400
        assert client.get("/health", headers={"Origin": "https://other.example"}).status_code == 403
        assert (
            client.get("/health", headers={"Origin": "https://opsyne.example"}).status_code == 200
        )


def test_cli_loads_env_before_settings_and_passes_trusted_proxy_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "server.env"
    env.write_text(
        "OPSYNE_HOST=0.0.0.0\nPORT=8080\nOPSYNE_TRUSTED_PROXIES=10.2.0.5\n", encoding="utf-8"
    )
    monkeypatch.setattr("sys.argv", ["opsyne", "serve", "--env-file", str(env), "--port", "8123"])
    sentinel = object()
    monkeypatch.setattr(cli, "create_app", lambda directory, **kwargs: sentinel)
    captured: dict[str, Any] = {}

    def run(app: object, **kwargs: Any) -> None:
        assert app is sentinel
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", run)
    assert cli.main() == 0
    assert captured["host"] == "0.0.0.0" and captured["port"] == 8123
    assert captured["forwarded_allow_ips"] == "10.2.0.5"
