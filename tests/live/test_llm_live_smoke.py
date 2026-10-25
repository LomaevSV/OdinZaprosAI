"""T-E2E-LIVE-LLM-01: сквозной ход свободного диалога на реальной LLM (ACCEPT-01 п. 3, DEC-07).

ПЛАТНЫЙ тест. Запускается только явно и только этот файл:
    ODIN_RUN_LIVE_LLM=1 uv run pytest tests/live/test_llm_live_smoke.py -q -s
Ключ, адрес и модель берутся из локального ``.env`` (ODIN_LLM_MODE=live, ODIN_LLM_BASE_URL, ODIN_LLM_MODEL,
ODIN_LLM_API_KEY). Данные поездки — Mock (сеть Tutu/Open-Meteo/OSRM не используется).

Бюджет: не больше 2 HTTP-запросов к LLM — Интерпретатор + Composer. Предел обеспечивает транспорт:
третий запрос не уходит в сеть. Повторы клиента выключены (ODIN_LLM_RETRIES=0). Тексты, ответы модели и ключ
не печатаются: только роль, статус, задержка и токены.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from odin.config import Settings, load_env_file
from tests.llm_fakes import ru_date

pytestmark = [
    pytest.mark.live,
    pytest.mark.req("T-E2E-LIVE-LLM-01", "ACCEPT-01", "DEC-07"),
    pytest.mark.skipif(
        os.environ.get("ODIN_RUN_LIVE_LLM") != "1", reason="платный Live LLM только при ODIN_RUN_LIVE_LLM=1"
    ),
]

MAX_LLM_REQUESTS = 2


class HardCapTransport(httpx.BaseTransport):
    """Пропускает в сеть не больше ``limit`` запросов; остальные отклоняются без сетевого обращения."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.sent = 0
        self.refused = 0
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.sent >= self.limit:
            self.refused += 1
            raise httpx.ConnectError("лимит платных запросов исчерпан (тест)", request=request)
        self.sent += 1
        return self._inner.handle_request(request)


def test_live_llm_free_text_turn_end_to_end(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.llm.gateway import default_client_factory

    env: dict[str, str] = dict(os.environ)
    load_env_file(".env", env)
    env.update(ODIN_DATA_MODE="mock", ODIN_LLM_RETRIES="0", ODIN_DB_PATH=str(tmp_path / "odin.sqlite3"))
    settings = Settings.from_env(env)
    if not settings.llm.live_enabled:
        pytest.skip(
            "в .env не настроены ODIN_LLM_MODE=live, ODIN_LLM_BASE_URL, ODIN_LLM_MODEL, ODIN_LLM_API_KEY"
        )

    cap = HardCapTransport(MAX_LLM_REQUESTS)
    application = build_application(
        settings, llm_client_factory=lambda llm: default_client_factory(llm, transport=cap)
    )
    import datetime as dt

    start = dt.date.today() + dt.timedelta(days=30)
    end = start + dt.timedelta(days=2)
    phrase = (
        f"Хочу из Москвы в Казань с {ru_date(start)} по {ru_date(end)} вдвоём, туда и обратно, "
        "не самолётом, бюджет не больше 60 000 рублей, желательно с завтраком"
    )
    with TestClient(create_app(settings, application=application)) as client:
        session = client.post("/api/v1/sessions").json()
        assert session["llm_mode"] == "live"
        response = client.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            json={
                "request_id": "req-live-llm-0001",
                "trip_revision": session["trip_revision"],
                "text": phrase,
            },
        )
        assert response.status_code == 200, response.status_code
        turn: dict[str, Any] = response.json()
        audit = application.storage.list_audit(turn["trip_id"])

    calls = [entry["payload"] for entry in audit if entry["event"] == "llm_call"]
    for call in calls:  # без текстов: роль, статус, задержка, токены
        print(
            "llm_call",
            {
                k: call.get(k)
                for k in (
                    "role",
                    "status",
                    "error_code",
                    "latency_ms",
                    "attempts",
                    "prompt_tokens",
                    "completion_tokens",
                )
            },
        )
    print(
        "http_requests_sent",
        cap.sent,
        "refused",
        cap.refused,
        "status",
        turn["status"],
        "input_mode",
        turn["input_mode"],
        "facts_verified",
        turn.get("facts_verified"),
    )
    assert cap.sent <= MAX_LLM_REQUESTS

    assert turn["input_mode"] == "free_text_llm", turn["assistant_text"][:300]
    request = turn["state"]["request"]
    assert request["origin"]["name"] == "Москва" and request["destination"]["name"] == "Казань"
    assert request["party"]["adults"] == 2
    assert (
        request["date_range"]["start"] == start.isoformat()
        and request["date_range"]["end"] == end.isoformat()
    )
    constraints = {item["field"]: item for item in request["constraints"]}
    assert constraints["transport_mode"]["value"] == ["rail"]
    assert constraints["transport_mode"]["requiredness"] == "required"
    assert constraints["budget_total"]["value"] == "60000.00"
    assert all(item["source_kind"] == "text_span" for item in constraints.values())
    assert turn["status"] == "planned", turn["questions"]
    text = turn["assistant_text"]
    assert "ДЕМО" in text  # маркировка режима не теряется после Composer
    for claim in ("забронировано", "оплачено", "гарантир"):
        assert claim not in text.lower()
