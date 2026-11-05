"""TRF-SELECT-01: маршруты плана привязаны к гостинице, а не к тарифу; матрица OSRM — по всем гостиницам.

Без сети и LLM: синтетическая матрица OSRM, настоящие ``normalize_transfers`` и ``Orchestrator._plan_transfers``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from odin.orchestrator import Orchestrator
from odin.tools.rest_adapters import normalize_transfers

pytestmark = pytest.mark.req("TRF-SELECT-01")

POINT = {"point_id": "st-sochi", "label": "Сочи (вокзал)", "lat": 43.59, "lon": 39.72}


def _routes() -> list:
    point = {**POINT, "directions": ["transfer_in", "transfer_out"]}
    hotels = [{"hotel_id": f"h{i}", "name": f"Отель {i}", "lat": 43.6, "lon": 39.7} for i in range(10)]
    size = 1 + len(hotels)
    payload = {"code": "Ok", "durations": [[600] * size] * size, "distances": [[5000] * size] * size}
    return normalize_transfers(
        payload,
        points=[point],
        hotels=hotels,
        city="Сочи",
        data_mode="live",
        retrieved_at=datetime.now(UTC),
        ttl_minutes=30,
    )


def _selected(hotel_offer: SimpleNamespace) -> dict:
    return {
        "hotel": (hotel_offer, [], 0),
        "outbound": (SimpleNamespace(terms={"arrival_point_id": "st-sochi"}), [], 0),
        "return": (SimpleNamespace(terms={"departure_point_id": "st-sochi"}), [], 0),
    }


def test_twenty_routes_for_ten_hotels_and_plan_takes_selected_hotel_only() -> None:
    routes = _routes()
    assert len(routes) == 20
    unknowns: list[str] = []
    chosen = Orchestrator._plan_transfers(
        SimpleNamespace(transfer_options=routes), _selected(SimpleNamespace(hotel_id="h7")), unknowns
    )
    assert [(r.direction, r.parameters["hotel_id"]) for r in chosen] == [
        ("transfer_in", "h7"),
        ("transfer_out", "h7"),
    ]
    assert unknowns == []
    assert all(r.price.amount is None for r in chosen), "стоимость не придумывается"


def test_hotel_tariffs_share_the_same_routes() -> None:
    state = SimpleNamespace(transfer_options=_routes())
    tariffs = [SimpleNamespace(offer_id=f"h7-{tariff}", hotel_id="h7") for tariff in ("A", "B")]
    picked = [
        [r.offer_id for r in Orchestrator._plan_transfers(state, _selected(tariff), [])] for tariff in tariffs
    ]
    assert picked[0] == picked[1] and len(picked[0]) == 2
