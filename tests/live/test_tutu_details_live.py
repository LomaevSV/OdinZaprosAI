"""T-INT-LIVE-TUTU-02: Live-детали гостиницы и авиа Tutu (бесплатно, только чтение).

Запуск ТОЛЬКО явно и только этого файла:
    ODIN_RUN_LIVE=1 uv run pytest tests/live/test_tutu_details_live.py -q -s
Разрешение руководителя 25.10 (второй Live-прогон): не больше 6 сетевых запросов СУММАРНО, включая служебные
сообщения MCP. План: ``initialize``, ``notifications/initialized``, ``tools/list`` + ``search_hotels`` (Казань),
``get_offer_details`` (первая гостиница выдачи), ``search_avia`` (Москва → Казань, в одну сторону) = 6.
Седьмой запрос не уходит в сеть (транспорт-ограничитель), повторы выключены. Сырые ответы не сохраняются.

Проверяется честность наличия (AVAIL-01): выдача поиска — ``unknown``; ``available`` — только по деталям
поставщика (``offer_details``); авиа-выдача наличие не подтверждает.
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
    pytest.mark.req("T-INT-LIVE-TUTU-02", "AVAIL-01", "DEC-11"),
    pytest.mark.skipif(os.environ.get("ODIN_RUN_LIVE") != "1", reason="Live только при ODIN_RUN_LIVE=1"),
]

TOTAL_REQUEST_LIMIT = 6
ALLOWED_METHODS = {"initialize", "notifications/initialized", "tools/list", "tools/call"}
ALLOWED_TOOLS = {"search_hotels", "get_offer_details", "search_avia"}


class TotalCapTransport(httpx.BaseTransport):
    """Только ``mcp.tutu.ru``, только разрешённые методы и инструменты, не больше ``limit`` запросов всего."""

    def __init__(self, limit: int) -> None:
        self.inner = httpx.HTTPTransport()
        self.limit = limit
        self.log: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "mcp.tutu.ru":
            raise httpx.ConnectError(f"хост вне плана прогона: {request.url.host}", request=request)
        message = json.loads(request.content or b"{}")
        method = message.get("method", "")
        label = method
        if method not in ALLOWED_METHODS:
            raise httpx.ConnectError(f"метод MCP вне разрешения: {method}", request=request)
        if method == "tools/call":
            name = message["params"]["name"]
            if name not in ALLOWED_TOOLS:
                raise httpx.ConnectError(f"инструмент вне плана: {name}", request=request)
            label = f"tools/call:{name}"
        if len(self.log) >= self.limit:
            raise httpx.ConnectError("лимит сетевых запросов исчерпан (решение 25.10)", request=request)
        self.log.append(label)
        return self.inner.handle_request(request)


def _dump(item: Any) -> dict[str, Any]:
    return item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)


def test_live_hotel_details_and_avia_are_honest_about_availability(tmp_path: Path) -> None:
    from odin.config import Settings
    from odin.tools.live_handlers import build_live_toolset
    from odin.tools.policy import ToolPolicy, load_provider_policies

    settings = Settings.from_env({"ODIN_DATA_MODE": "live", "ODIN_LLM_MODE": "off"})
    policy = ToolPolicy(data_mode="live", policies=load_provider_policies(None))
    cap = TotalCapTransport(TOTAL_REQUEST_LIMIT)
    toolset = build_live_toolset(
        policy=policy,
        data_mode="live",
        replay_dir=None,
        timeout_s=settings.limits.tool_timeout_s,
        retries=0,
        mcp_transport=cap,
        http_transport=cap,
    )
    start = date.today() + timedelta(days=30)
    stay = {"check_in": start.isoformat(), "check_out": (start + timedelta(days=2)).isoformat(), "adults": 2}
    protocol: dict[str, Any] = {"dates": stay}
    started = time.perf_counter()

    hotels = toolset.search_hotels({"city": "Казань", **stay})
    data = hotels.get("data") or {}
    offers = [_dump(offer) for offer in data.get("offers") or []]
    listed = [_dump(hotel) for hotel in data.get("hotels") or []]
    protocol["search_hotels"] = {
        "status": hotels["status"],
        "error_code": hotels.get("error_code"),
        "offers": len(offers),
        "availability": sorted({o["availability"]["availability_status"] for o in offers}),
        "evidence": sorted({o["availability"]["evidence_level"] for o in offers}),
        "price_scope": sorted({o["price_total"]["price_scope"] for o in offers}),
        "completeness": sorted({o["price_total"]["total_completeness"] for o in offers}),
    }

    details_offers: list[dict[str, Any]] = []
    if listed:
        first = next(h for h in listed if any(o["hotel_id"] == h["hotel_id"] for o in offers))
        details = toolset.get_offer_details({"hotels": [first], **stay})
        ddata = details.get("data") or {}
        details_offers = [_dump(offer) for offer in ddata.get("offers") or []]
        refined = [_dump(h) for h in ddata.get("hotels") or []]
        protocol["get_offer_details"] = {
            "status": details["status"],
            "error_code": details.get("error_code"),
            "hotel": first["name"][:60],
            "rates": len(details_offers),
            "availability": sorted({o["availability"]["availability_status"] for o in details_offers}),
            "evidence": sorted({o["availability"]["evidence_level"] for o in details_offers}),
            "unknown_reasons": sorted({str(o["availability"].get("unknown_reason")) for o in details_offers}),
            "breakfast": sorted({str(o["terms"]["breakfast_included"]) for o in details_offers}),
            "free_cancellation": sorted({str(o["terms"]["free_cancellation"]) for o in details_offers}),
            "check_in_from": refined[0].get("check_in_from") if refined else None,
            "unavailable_hotel_ids": len(ddata.get("unavailable_hotel_ids") or []),
            "min_price": min(
                (o["price_total"]["amount"] for o in details_offers if o["price_total"]["amount"]),
                default=None,
            ),
        }

    avia = toolset.search_transport(
        {
            "origin": "Москва",
            "destination": "Казань",
            "date": start.isoformat(),
            "adults": 2,
            "modes": ["flight"],
        }
    )
    adata = avia.get("data") or {}
    flights = [_dump(offer) for offer in adata.get("offers") or []]
    protocol["search_avia"] = {
        "status": avia["status"],
        "error_code": avia.get("error_code"),
        "offers": len(flights),
        "availability": sorted({f["availability"]["availability_status"] for f in flights}),
        "price_scope": sorted({f["price"]["price_scope"] for f in flights}),
        "carriers": sorted({str(f["carrier"]) for f in flights})[:5],
        "first": flights
        and {k: flights[0][k] for k in ("title", "departure_at", "arrival_at", "duration_minutes")},
    }
    protocol["elapsed_s"] = round(time.perf_counter() - started, 1)
    protocol["network_requests"] = cap.log
    print("LIVE_PROTOCOL " + json.dumps(protocol, ensure_ascii=False, default=str))

    assert len(cap.log) <= TOTAL_REQUEST_LIMIT
    # AVAIL-01: выдача поиска наличие не подтверждает.
    assert all(o["availability"]["availability_status"] == "unknown" for o in offers)
    assert all(f["availability"]["availability_status"] != "available" for f in flights)
    # «Есть номер» — только по деталям поставщика, никогда по выдаче.
    for offer in details_offers:
        if offer["availability"]["availability_status"] == "available":
            assert offer["availability"]["evidence_level"] == "offer_details"
    assert offers, protocol["search_hotels"]
