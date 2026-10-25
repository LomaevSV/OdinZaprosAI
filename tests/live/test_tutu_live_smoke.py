"""T-E2E-LIVE-01: сквозной сценарий на реальных Tutu MCP, Open-Meteo и OSRM (бесплатно, только чтение).

Запускается ТОЛЬКО явно: ``ODIN_RUN_LIVE=1 uv run pytest -m live tests/live/test_tutu_live_smoke.py -q``.
Объём: геокодинг 2 города, search_rail туда и обратно, search_hotels, матрица OSRM — ~6 запросов.
Протокол режимов по компонентам печатается в вывод теста (без текстов пользователя и ключей).
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.e2e_live,
    pytest.mark.req("T-E2E-LIVE-01", "ACCEPT-01"),
    pytest.mark.skipif(os.environ.get("ODIN_RUN_LIVE") != "1", reason="Live только при ODIN_RUN_LIVE=1"),
]


def test_live_moscow_kazan_plan_has_live_components(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.config import Settings

    settings = Settings.from_env(
        {
            "ODIN_DATA_MODE": "live",
            "ODIN_DB_PATH": str(tmp_path / "live.sqlite3"),
            "ODIN_REPLAY_DIR": str(tmp_path / "replay"),
        }
    )
    start = date.today() + timedelta(days=30)
    form = {
        "origin": "Москва",
        "destination": "Казань",
        "date_from": start.isoformat(),
        "date_to": (start + timedelta(days=2)).isoformat(),
        "adults": 2,
        "return_trip": True,
        "transport_modes": ["rail"],
    }
    with TestClient(create_app(settings, application=build_application(settings))) as client:
        session = client.post("/api/v1/sessions").json()
        turn = client.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            json={"request_id": "req-live-e2e-1", "trip_revision": session["trip_revision"], "form": form},
        ).json()
    plan = turn["state"]["plan"]
    print("trip_data_mode:", plan and plan["trip_data_mode"], plan and plan["data_mode_breakdown"])
    print("termination:", turn["state"]["search_termination"])
    assert plan is not None and plan["components"], turn["assistant_text"]
    assert {"outbound", "hotel"} <= {component["role"] for component in plan["components"]}
    assert all(component["data_mode"] == "live" for component in plan["components"])
