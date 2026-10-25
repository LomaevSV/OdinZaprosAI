"""T-E2E-LIVE-02: сквозной план на реальных Tutu, Open-Meteo и OSRM — авиа туда и обратно (бесплатно, только чтение).

Запуск ТОЛЬКО после разрешения владельца и только этого файла:
    ODIN_RUN_LIVE_E2E=1 uv run pytest tests/live/test_live_e2e_roundtrip.py -q -s
Не больше 10 HTTP-запросов СУММАРНО (служебные сообщения MCP включены): MCP ``initialize``,
``notifications/initialized``, ``tools/list`` (3) + геокодинг Open-Meteo ×2 + ``search_avia`` туда и обратно (2) +
``search_hotels`` (1) + ``get_offer_details`` лучшей гостиницы (1, ``ODIN_TOP_K=1``) + матрица OSRM (1) = 10.
Одиннадцатый запрос не уходит в сеть, повторы выключены. Пересборка по бюджету, корзина и демо-покупка
сетевых запросов не делают — это тоже проверяется. Сырые ответы не сохраняются.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.e2e_live,
    pytest.mark.req("T-E2E-LIVE-02", "ACCEPT-01", "AVAIL-01", "REPLAN-01"),
    pytest.mark.skipif(
        os.environ.get("ODIN_RUN_LIVE_E2E") != "1", reason="Live E2E только при ODIN_RUN_LIVE_E2E=1"
    ),
]

TOTAL_LIMIT = 10
ALLOWED_HOSTS = {"mcp.tutu.ru", "geocoding-api.open-meteo.com", "router.project-osrm.org"}
ALLOWED_TOOLS = {"search_avia", "search_hotels", "get_offer_details"}
API = "/api/v1"


class TotalCapTransport(httpx.BaseTransport):
    def __init__(self, limit: int, inner: httpx.BaseTransport | None = None) -> None:
        self.inner = inner or httpx.HTTPTransport()
        self.limit = limit
        self.log: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host not in ALLOWED_HOSTS:
            raise httpx.ConnectError(f"хост вне плана: {host}", request=request)
        label = host
        if host == "mcp.tutu.ru":
            message = json.loads(request.content or b"{}")
            label = message.get("method", "")
            if label == "tools/call":
                name = message["params"]["name"]
                if name not in ALLOWED_TOOLS:
                    raise httpx.ConnectError(f"инструмент вне плана: {name}", request=request)
                label = f"tools/call:{name}"
            elif label not in {"initialize", "notifications/initialized", "tools/list"}:
                raise httpx.ConnectError(f"метод MCP вне плана: {label}", request=request)
        if len(self.log) >= self.limit:
            raise httpx.ConnectError("лимит сетевых запросов исчерпан", request=request)
        self.log.append(label)
        return self.inner.handle_request(request)


def make_transport() -> TotalCapTransport:  # подменяется в сухом прогоне
    return TotalCapTransport(TOTAL_LIMIT)


def _post(client: Any, path: str, body: dict[str, Any], expected: int = 200) -> dict[str, Any]:
    response = client.post(API + path, json=body)
    assert response.status_code == expected, (path, response.status_code, response.text[:300])
    return response.json()


def test_live_roundtrip_flight_plan_replan_and_demo_purchase(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.config import Settings

    cap = make_transport()
    settings = Settings.from_env(
        {
            "ODIN_DATA_MODE": "live",
            "ODIN_LLM_MODE": "off",
            "ODIN_TOOL_RETRIES": "0",
            "ODIN_TOP_K": "1",
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
        "transport_modes": ["flight"],
        "transport_modes_required": True,
        "budget_amount": "150000",
        "budget_currency": "RUB",
        "budget_required": True,
    }
    application = build_application(settings, mcp_transport=cap, http_transport=cap)
    started = time.perf_counter()
    with TestClient(create_app(settings, application=application)) as client:
        session = client.post(API + "/sessions").json()
        turn = _post(
            client,
            f"/sessions/{session['session_id']}/messages",
            {"request_id": "req-live2-0001", "trip_revision": session["trip_revision"], "form": form},
        )
        plan_seconds = round(time.perf_counter() - started, 1)
        sent_after_plan = len(cap.log)
        replanned = _post(
            client,
            f"/trips/{turn['trip_id']}/replan",
            {
                "request_id": "req-live2-0002",
                "trip_revision": turn["trip_revision"],
                "change_set": {"budget_amount": "20000", "budget_currency": "RUB"},
            },
        )
        cart = _post(
            client,
            f"/trips/{turn['trip_id']}/cart",
            {"request_id": "req-live2-0003", "trip_revision": replanned["trip_revision"]},
            expected=201,
        )
        body = {
            "request_id": "req-live2-0004",
            "trip_revision": cart["trip_revision"],
            "confirmation_id": "conf-live2-0001",
            "purchase_consent": True,
            "risk_acknowledgement": True,
        }
        receipt = _post(client, f"/carts/{cart['cart']['cart_id']}/confirm", body)
        again = _post(
            client, f"/carts/{cart['cart']['cart_id']}/confirm", {**body, "request_id": "req-live2-0005"}
        )

    state, plan = turn["state"], turn["state"]["plan"]
    after = replanned["state"]
    protocol = {
        "plan_seconds": plan_seconds,
        "network": cap.log,
        "network_after_plan": len(cap.log) - sent_after_plan,
        "status": turn["status"],
        "plan_mode": plan and plan["trip_data_mode"],
        "components": plan
        and [[c["role"], c["kind"], c["data_mode"], c["title"][:70]] for c in plan["components"]],
        "total": plan
        and [plan["total_price"]["amount"], plan["total_price"]["currency"], plan["completeness"]],
        "unknowns": plan and plan["unknowns"],
        "connection_checks": plan
        and [[c["from_component"], c["to_component"], c["status"]] for c in plan["connection_checks"]],
        "offers": {"transport": len(state["transport_offers"]), "hotels": len(state["hotel_offers"])},
        "termination": state["search_termination"],
        "replan": {
            "budget": [
                [c["value"], c["requiredness"]]
                for c in after["request"]["constraints"]
                if c["field"] == "budget_total"
            ],
            "transport": [
                [c["value"], c["requiredness"]]
                for c in after["request"]["constraints"]
                if c["field"] == "transport_mode"
            ],
            "budget_status": sorted(
                {
                    e["status"]
                    for e in after["plan"]["constraint_evaluations"]
                    if e["constraint_id"] == "c-budget_total"
                }
            ),
        },
        "receipt": [receipt["cart"]["status"], receipt["receipt"]["notice"], again.get("idempotent_replay")],
        "answer_head": turn["assistant_text"][:700],
    }
    print("LIVE_PROTOCOL " + json.dumps(protocol, ensure_ascii=False, default=str))

    assert len(cap.log) <= TOTAL_LIMIT
    assert protocol["network_after_plan"] == 0, "пересборка по бюджету, корзина и демо-покупка без сети"
    assert turn["status"] == "planned" and plan and plan["components"], turn["assistant_text"][:500]
    roles = {c["role"]: c for c in plan["components"]}
    assert {"outbound", "return", "hotel"} <= set(roles)
    assert roles["outbound"]["kind"] == "flight" and roles["return"]["kind"] == "flight"
    assert all(c["data_mode"] == "live" for c in plan["components"]), "в live нет подмены на ДЕМО"
    assert plan["completeness"] != "complete"  # сборы при оформлении не подтверждены
    assert "наличие (туда) не подтверждено источником" in plan["unknowns"]  # выдача ≠ наличие
    budget = protocol["replan"]["budget"]
    assert budget and budget[0][1] == "required" and str(budget[0][0]).startswith("20000")
    assert protocol["replan"]["transport"] == [[["flight"], "required"]]
    assert receipt["cart"]["status"] == "demo_purchased" and again.get("idempotent_replay") is True
    assert "Реального бронирования и списания средств нет" in receipt["receipt"]["notice"]
