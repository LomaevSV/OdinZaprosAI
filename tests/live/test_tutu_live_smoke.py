"""T-E2E-LIVE-01: сквозной сценарий на реальных Tutu MCP, Open-Meteo и OSRM (бесплатно, только чтение).

Запускается ТОЛЬКО явно: ``ODIN_RUN_LIVE=1 uv run pytest -m live tests/live/test_tutu_live_smoke.py -q -s``.
Разрешение руководителя 25.10 (DEC-11): не более 6 бесплатных запросов данных, только чтение, без
бронирований, оплаты и передачи секретов. Ограничение обеспечивает транспорт-счётчик: седьмой запрос данных
не уходит в сеть. Сценарий: Москва → Казань в одну сторону, 2 взрослых, поезд — геокодинг ×2,
``search_rail`` ×1, ``search_hotels`` ×1, ``get_offer_details`` ×1 (``ODIN_TOP_K=1``), матрица OSRM ×1 =
6 запросов данных. Служебные сообщения MCP (``initialize``, ``notifications/initialized``, ``tools/list``) данных
не запрашивают и считаются отдельно: всего до 9 HTTP-запросов. Под ограничение «6 сетевых запросов суммарно»
(решение 25.10, второй прогон) этот тест НЕ подходит — для него нужен ``tests/live/test_tutu_details_live.py``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.e2e_live,
    pytest.mark.req("T-E2E-LIVE-01", "ACCEPT-01", "DEC-11"),
    pytest.mark.skipif(os.environ.get("ODIN_RUN_LIVE") != "1", reason="Live только при ODIN_RUN_LIVE=1"),
]

DATA_REQUEST_LIMIT = 6
ALLOWED_HOSTS = {"mcp.tutu.ru", "geocoding-api.open-meteo.com", "router.project-osrm.org"}
SERVICE_METHODS = {"initialize", "notifications/initialized", "tools/list"}


class LimitedTransport(httpx.BaseTransport):
    """Пропускает в сеть только разрешённые хосты и не больше ``limit`` запросов данных (общий счётчик)."""

    def __init__(self, counter: dict[str, list[str]], limit: int) -> None:
        self.inner = httpx.HTTPTransport()
        self.counter = counter
        self.limit = limit

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host not in ALLOWED_HOSTS:
            raise httpx.ConnectError(f"хост вне разрешения: {host}", request=request)
        label = host
        if host == "mcp.tutu.ru":
            message = json.loads(request.content or b"{}")
            method = message.get("method", "")
            if method in SERVICE_METHODS:
                self.counter["service"].append(method)
                return self.inner.handle_request(request)
            if method != "tools/call":
                raise httpx.ConnectError(f"метод MCP вне разрешения: {method}", request=request)
            label = f"tutu:{message['params']['name']}"
        if len(self.counter["data"]) >= self.limit:
            raise httpx.ConnectError("лимит бесплатных запросов исчерпан (DEC-11)", request=request)
        self.counter["data"].append(label)
        return self.inner.handle_request(request)


def test_live_moscow_kazan_plan_has_live_components(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.config import Settings

    counter: dict[str, list[str]] = {"data": [], "service": []}
    transport = LimitedTransport(counter, DATA_REQUEST_LIMIT)
    settings = Settings.from_env(
        {
            "ODIN_DATA_MODE": "live",
            "ODIN_LLM_MODE": "off",
            "ODIN_TOOL_RETRIES": "0",
            "ODIN_TOP_K": "1",  # детали одной гостиницы: всего 6 запросов данных
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
        "return_trip": False,
        "transport_modes": ["rail"],
    }
    application = build_application(settings, mcp_transport=transport, http_transport=transport)
    started = time.perf_counter()
    with TestClient(create_app(settings, application=application)) as client:
        session = client.post("/api/v1/sessions").json()
        turn = client.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            json={"request_id": "req-live-e2e-1", "trip_revision": session["trip_revision"], "form": form},
        ).json()
        cart = client.post(
            f"/api/v1/trips/{turn['trip_id']}/cart",
            json={"request_id": "req-live-e2e-2", "trip_revision": turn["trip_revision"]},
        )
    elapsed = round(time.perf_counter() - started, 1)
    plan = turn["state"]["plan"]
    protocol = {
        "elapsed_s": elapsed,
        "data_requests": counter["data"],
        "mcp_service_messages": counter["service"],
        "status": turn["status"],
        "trip_data_mode": plan and plan["trip_data_mode"],
        "components": plan and [(c["role"], c["data_mode"], c["title"][:60]) for c in plan["components"]],
        "total": plan and plan["total_price"],
        "completeness": plan and plan["completeness"],
        "unknowns": plan and plan["unknowns"],
        "termination": turn["state"]["search_termination"],
        "offers": {
            "transport": len(turn["state"]["transport_offers"]),
            "hotels": len(turn["state"]["hotel_offers"]),
        },
        "cart_status": cart.status_code,
    }
    print("LIVE_PROTOCOL " + json.dumps(protocol, ensure_ascii=False, default=str))
    assert len(counter["data"]) <= DATA_REQUEST_LIMIT
    assert plan is not None and plan["components"], turn["assistant_text"]
    assert {"outbound", "hotel"} <= {component["role"] for component in plan["components"]}
    assert all(component["data_mode"] == "live" for component in plan["components"])
    assert "ДЕМО-вариант" not in turn["assistant_text"]
    assert cart.status_code == 201
