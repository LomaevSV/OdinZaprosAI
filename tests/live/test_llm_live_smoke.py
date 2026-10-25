"""T-E2E-LIVE-LLM-01: сквозной ход свободного диалога на реальной LLM (ACCEPT-01 п. 3, DEC-07).

ПЛАТНЫЙ тест. Запускается только явно и только этот файл:
    ODIN_RUN_LIVE_LLM=1 uv run pytest tests/live/test_llm_live_smoke.py -q -s
Ключ, адрес и модель берутся из локального ``.env`` (ODIN_LLM_MODE=live, ODIN_LLM_BASE_URL, ODIN_LLM_MODEL,
ODIN_LLM_API_KEY). Данные поездки — Mock (сеть Tutu/Open-Meteo/OSRM не используется).

Бюджет: по умолчанию не больше 2 HTTP-запросов к LLM — ход 1: Интерпретатор + Composer.
С ``ODIN_LLM_SMOKE_CALLS=3`` добавляется ход 2 «А давай всё-таки самолётом» (только Интерпретатор, Composer
выключен): проверка пересборки с сохранением обязательного бюджета. Предел обеспечивает транспорт: лишний
запрос не уходит в сеть. Повторы клиента выключены (ODIN_LLM_RETRIES=0). Ключ не печатается; печатаются
роль, статус, задержка, токены и начало ответа агента (тестовая фраза не содержит персональных данных).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
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

MAX_LLM_REQUESTS = 3 if os.environ.get("ODIN_LLM_SMOKE_CALLS") == "3" else 2
SWITCH = "А давай всё-таки самолётом"


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


def _say(client: Any, session: dict[str, Any], text: str, revision: int, n: int) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={"request_id": f"req-live-llm-{n:04d}", "trip_revision": revision, "text": text},
    )
    assert response.status_code == 200, response.status_code
    return response.json()


def _summary(turn: dict[str, Any]) -> dict[str, Any]:
    request = turn["state"]["request"] or {}
    return {
        "status": turn["status"],
        "input_mode": turn["input_mode"],
        "origin": (request.get("origin") or {}).get("name"),
        "destination": (request.get("destination") or {}).get("name"),
        "dates": request.get("date_range"),
        "adults": (request.get("party") or {}).get("adults"),
        "constraints": {
            c["field"]: [c["value"], c["requiredness"], c["source_span"]]
            for c in request.get("constraints", [])
        },
        "unparsed": [
            g["raw_text"] for g in request.get("constraint_groups", []) if g["operator"] == "unparsed"
        ],
        "questions": [q["question"] for q in turn.get("questions", [])],
        "answer_head": turn["assistant_text"][:500],
    }


def test_live_llm_free_text_turn_end_to_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
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
    turns: list[dict[str, Any]] = []
    odin_logger = logging.getLogger("odin")  # propagate=False: обработчик caplog подключается напрямую
    odin_logger.addHandler(caplog.handler)
    try:
        with TestClient(create_app(settings, application=application)) as client:
            session = client.post("/api/v1/sessions").json()
            assert session["llm_mode"] == "live"
            turns.append(_say(client, session, phrase, session["trip_revision"], 1))
            if MAX_LLM_REQUESTS == 3 and cap.sent == 2 and turns[0]["status"] == "planned":
                application.llm.settings = replace(application.llm.settings, composer=False)  # ход 2: 1 вызов
                turns.append(_say(client, session, SWITCH, turns[0]["trip_revision"], 2))
            audit = application.storage.list_audit(turns[0]["trip_id"])
    finally:
        odin_logger.removeHandler(caplog.handler)

    keys = ("role", "status", "error_code", "latency_ms", "attempts", "prompt_tokens", "completion_tokens")
    calls = [{k: e["payload"].get(k) for k in keys} for e in audit if e["event"] == "llm_call"]
    rewrite = "отклонена" if "переформулировка отклонена" in caplog.text else "принята или не вызывалась"
    protocol = {
        "model": settings.llm.model,
        "http_requests_sent": cap.sent,
        "refused": cap.refused,
        "llm_calls": calls,
        "composer_rewrite": rewrite,
        "turns": [_summary(turn) for turn in turns],
    }
    print("LLM_PROTOCOL " + json.dumps(protocol, ensure_ascii=False, default=str))
    assert cap.sent <= MAX_LLM_REQUESTS
    assert settings.llm.api_key not in json.dumps(protocol, ensure_ascii=False) + caplog.text

    first = turns[0]
    assert first["input_mode"] == "free_text_llm", first["assistant_text"][:300]
    request = first["state"]["request"]
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
    assert constraints["budget_total"]["requiredness"] == "required"
    assert all(item["source_kind"] == "text_span" for item in constraints.values())
    assert first["status"] == "planned", first["questions"]
    assert first["state"]["plan"]["components"] and first["state"]["cart"] is None  # без покупки
    text = first["assistant_text"]
    assert "ДЕМО" in text  # маркировка режима не теряется после Composer
    for claim in ("забронировано", "оплачено", "гарантир"):
        assert claim not in text.lower()

    if MAX_LLM_REQUESTS == 3:
        assert len(turns) == 2, "ход 2 не выполнен: лимит вызовов израсходован на ходе 1"
        second = turns[1]
        assert second["status"] == "planned" and second["input_mode"] == "free_text_llm"
        after = {item["field"]: item for item in second["state"]["request"]["constraints"]}
        assert after.get("transport_mode", {}).get("value") != ["rail"]  # изменение применено
        assert after["budget_total"]["value"] == "60000.00"  # REPLAN-01: обязательный бюджет сохранён
        assert after["budget_total"]["requiredness"] == "required"
        assert second["state"]["request"]["date_range"] == request["date_range"]
        assert second["state"]["request"]["party"]["adults"] == 2
        assert "Пересборка" in second["assistant_text"]
