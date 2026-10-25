"""Офлайн-диагностика конфигурации (P0-3): без сети, ключ не выводится."""

from __future__ import annotations

import json

import pytest

from odin import diagnose

pytestmark = pytest.mark.req("SECURITY-01", "DELIVERY-01")

LIVE = {
    "ODIN_LLM_MODE": "live",
    "ODIN_LLM_BASE_URL": "https://llm.example.test/v1",
    "ODIN_LLM_MODEL": "m-1",
    "ODIN_LLM_API_KEY": "sk-secret-value-123",
}


def test_diagnose_reports_modes_without_secret(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for key, value in LIVE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ODIN_DATA_MODE", raising=False)
    code, data = diagnose.report(str(tmp_path / "absent.env"))
    dump = json.dumps(data, ensure_ascii=False)
    assert code == 0 and data["data_mode"] == "live" and data["llm"]["key"] == "present"
    assert data["llm"]["host"] == "llm.example.test" and data["llm"]["paid_calls_per_turn_typical"] == 2
    assert "sk-secret-value-123" not in dump


def test_diagnose_explains_broken_live_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ODIN_LLM_MODE", "live")
    for key in (
        "ODIN_LLM_BASE_URL",
        "ODIN_LLM_MODEL",
        "ODIN_LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    code, data = diagnose.report(str(tmp_path / "absent.env"))
    assert code == 1 and "ODIN_LLM_API_KEY" in data["error"]


@pytest.mark.req("T-DLV-DOCKER-01")
def test_selfcheck_passes_against_api(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Команда самопроверки для Docker-приёмки совпадает с фактическим API (сама Docker-проверка — BLOCKED)."""
    from fastapi.testclient import TestClient

    from odin import selfcheck
    from odin.api import create_app
    from odin.config import Settings

    settings = Settings.from_env({"ODIN_DATA_MODE": "mock", "ODIN_DB_PATH": str(tmp_path / "s.sqlite3")})
    with TestClient(create_app(settings)) as client:

        def call(base: str, method: str, path: str, body=None):
            prefix = base.replace("http://testserver", "")
            response = client.request(method, prefix + path, json=body)
            assert response.status_code < 300, response.text
            return response.json()

        monkeypatch.setattr(selfcheck, "_call", call)
        result = selfcheck.run("http://testserver")
    assert result["plan_mode"] == "mock" and result["idempotent_replay"] is True
    assert result["health"]["status"] == "ok"
