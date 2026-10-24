"""S5: минимальное вдохновение P0 на Mock (UC-04, K-01, K-02, DEC-18, DEC-28 по умолчанию).

- K-01: направления нет, дат нет — кандидаты без цены («стоимость требует дат»), даты не придумываются;
- K-02: направления нет, город отправления, даты и состав известны — ДЕМО-оценка стоимости и времени пути
  для лучших N кандидатов; итог неполный (трансфер не оценивается, DEC-18);
- климатическая норма не выдаётся за прогноз, визовых утверждений нет;
- трансфер с неизвестной стоимостью делает итог плана `partial` (K-17).
Live (K-01/K-02 на Live, направление вне каталога через геокодинг) — BLOCKED: DEC-09, DEC-12.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from odin.orchestrator import INSPIRATION_TOP_N
from tests.conftest import trip_form

pytestmark = [pytest.mark.e2e_mock, pytest.mark.req("UC-04")]

API = "/api/v1"
FORBIDDEN_CLAIMS = re.compile(
    r"виза не нужна|без визы|визы не требу|безвиз|прогноз погоды:|ожидается \+?\d|°", re.IGNORECASE
)


def _send(client: Any, form: dict[str, Any], calls: list[str] | None = None, application: Any = None) -> dict:
    if calls is not None:
        original = application.registry.invoke

        def spy(request: Any) -> Any:
            calls.append(request.tool_name)
            return original(request)

        application.registry.invoke = spy
    session = client.post(f"{API}/sessions").json()
    response = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-s5-00001", "trip_revision": session["trip_revision"], "form": form},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.req("K-01", "UC-03", "DEC-28")
def test_inspiration_without_dates_does_not_price_or_invent_dates(client: Any, application: Any) -> None:
    calls: list[str] = []
    turn = _send(
        client,
        trip_form(destination=None, wishes_tags=["море"], date_from=None, date_to=None),
        calls,
        application,
    )
    assert turn["questions"] == [], "для вдохновения даты не блокируют"
    assert calls == ["suggest_destinations"], "без дат цены и наличие не запрашиваются"
    state = turn["state"]
    assert state["request"]["date_range"] is None
    for candidate in state["candidate_destinations"]:
        assert candidate["estimated_cost"] is None and candidate["cost_note"] == "стоимость требует дат"
    assert "стоимость требует дат" in turn["assistant_text"]


@pytest.mark.req("K-02", "DEC-18", "PRICE-01")
def test_inspiration_with_origin_and_dates_estimates_top_candidates(client: Any, application: Any) -> None:
    calls: list[str] = []
    turn = _send(client, trip_form(destination=None, wishes_tags=["море"]), calls, application)
    state = turn["state"]
    candidates = state["candidate_destinations"]
    assert 0 < len(candidates) <= INSPIRATION_TOP_N
    assert "Москва" not in {c["place"]["name"] for c in candidates}, "город отправления не предлагается"
    assert calls.count("search_transport") == len(candidates)
    assert calls.count("search_hotels") == len(candidates)
    text = turn["assistant_text"]
    for candidate in candidates:
        cost = candidate["estimated_cost"]
        assert cost is not None and Decimal(cost["amount"]) > 0
        assert cost["total_completeness"] == "partial", "трансфер не оценивается (DEC-18)"
        assert cost["price_scope"] == "trip_total" and cost["source"]["data_mode"] == "mock"
        assert "трансфер не оценён" in candidate["cost_note"] and "ДЕМО" in candidate["cost_note"]
        assert "в пути туда" in candidate["cost_note"]
        assert candidate["place"]["name"] in text
    assert "ДЕМО" in text and "от " in text
    # Числа в ответе — только из таблицы фактов.
    finished = [e for e in application.storage.list_audit(turn["trip_id"]) if e["event"] == "turn_finished"]
    assert finished[-1]["payload"]["facts_verified"] is True


@pytest.mark.req("K-01", "K-02", "DEC-12", "T-NEG-CLIMATE-01", "T-NEG-VISA-01")
@pytest.mark.parametrize("with_dates", [False, True])
def test_no_climate_forecast_and_no_visa_claims(client: Any, with_dates: bool) -> None:
    overrides: dict[str, Any] = {"destination": None, "wishes_tags": ["море", "история"]}
    if not with_dates:
        overrides.update(date_from=None, date_to=None)
    turn = _send(client, trip_form(**overrides))
    text = turn["assistant_text"]
    assert not FORBIDDEN_CLAIMS.search(text)
    assert "прогноз погоды не выполнялся" in text and "визовые требования не проверены" in text
    for candidate in turn["state"]["candidate_destinations"]:
        assert candidate["visa_status"] == "not_verified"
        # Климат не загружен (DEC-12): нормы нет — и она не подменяется прогнозом или догадкой.
        assert candidate["climate_status"] == "not_loaded"
        assert "DEC-12" in (candidate["climate_note"] or "")
        assert not FORBIDDEN_CLAIMS.search(candidate["description"] + " " + candidate["cost_note"])


@pytest.mark.req("K-02", "UC-03")
def test_inspiration_without_party_asks_instead_of_assuming(client: Any) -> None:
    turn = _send(client, trip_form(destination=None, wishes_tags=["море"], adults=None))
    assert [q["field"] for q in turn["questions"]] == ["adults"]
    assert turn["state"]["candidate_destinations"] == []


@pytest.mark.req("K-17", "DEC-18", "PRICE-01")
def test_transfer_with_unknown_cost_makes_total_partial(client: Any) -> None:
    turn = _send(client, trip_form())
    plan = turn["state"]["plan"]
    transfers = [c for c in plan["components"] if c["kind"] == "transfer"]
    assert transfers, "трансфер в плане есть"
    transfer_prices = [o["price"] for o in turn["state"]["transfer_options"]]
    assert transfer_prices and all(price["amount"] is None for price in transfer_prices)
    assert plan["completeness"] == "partial"
    assert plan["total_price"]["amount"] is not None, "известная часть суммы показана"
    assert "стоимость трансфера не подтверждена" in plan["unknowns"]
    assert "стоимость известна частично" in turn["assistant_text"]


@pytest.mark.req("UC-04")
def test_named_destination_is_not_limited_to_catalog(client: Any) -> None:
    from odin.tools.mock_data import DEMO_CATALOG

    assert "Новосибирск" not in {entry.place_name for entry in DEMO_CATALOG}
    turn = _send(client, trip_form(destination="Новосибирск"))
    assert turn["status"] == "planned"
    assert {"outbound", "hotel"} <= {c["role"] for c in turn["state"]["plan"]["components"]}
