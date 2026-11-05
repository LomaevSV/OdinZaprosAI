"""MIXED-CURRENCY-PLAN-01 (этап 2): разные валюты без курса — неизвестный итог, без HTTP 500 (DEC-17).

Дополняет независимый пакет ``tests/independent/test_mixed_currency_iteration3.py``: путь вдохновения
(``_candidate_estimate``), обязательный бюджет, причина ``currency_unresolved`` в оценке бюджета плана и
смысл ``partial_components``. Только Mock и подмена цен в памяти: без сети, платных API и покупок.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from odin.core.money import CurrencyMixError, sum_prices, sum_prices_or_unresolved
from odin.orchestrator import Orchestrator
from tests.conftest import trip_form
from tests.samples import price

pytestmark = [pytest.mark.e2e_mock, pytest.mark.req("PRICE-01", "DEC-17")]

API = "/api/v1"


def _eur(offer: Any, amount: str = "100") -> Any:
    new_price = offer.price.model_copy(
        update={
            "amount": Decimal(amount),
            "currency": "EUR",
            "total_completeness": "complete",
            "unknown_reason": None,
        }
    )
    return offer.model_copy(update={"price": new_price})


def _inject_eur_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Orchestrator._build_plan

    def mixed(self: Orchestrator, state: Any, request: Any) -> Any:
        offers = [_eur(o) if o.direction == "outbound" else o for o in state.transport_offers]
        return original(self, state.model_copy(update={"transport_offers": offers}), request)

    monkeypatch.setattr(Orchestrator, "_build_plan", mixed)


def _send(client: Any, form: dict[str, Any]) -> Any:
    session = client.post(f"{API}/sessions").json()
    return client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-mixed2-0001", "trip_revision": session["trip_revision"], "form": form},
    )


def _budget(plan: dict[str, Any]) -> dict[str, Any]:
    return next(e for e in plan["constraint_evaluations"] if e["constraint_id"] == "c-budget_total")


def test_partial_components_keep_sum_prices_meaning_under_currency_mix() -> None:
    complete_rub = price(
        amount=Decimal("30000"),
        currency="RUB",
        total_completeness="complete",
        unknown_reason=None,
        known_fees=[],
        mandatory_fees_status="included",
    )
    complete_eur = complete_rub.model_copy(update={"amount": Decimal("100"), "currency": "EUR"})
    unknown = price(
        amount=None, currency=None, total_completeness="unknown", unknown_reason="not_provided_by_source"
    )
    with pytest.raises(CurrencyMixError):
        sum_prices([complete_rub, complete_eur], ["hotel", "outbound"])
    mixed = sum_prices_or_unresolved(
        [complete_rub, complete_eur, unknown], ["hotel", "outbound", "transfer_in"]
    )
    assert (mixed.amount, mixed.currency, mixed.completeness, mixed.unknown_reason) == (
        None,
        None,
        "unknown",
        "currency_unresolved",
    )
    # MIXED-PARTIAL-COMPONENTS-03: только компоненты без известной полной цены, а не все подряд.
    assert mixed.partial_components == ("transfer_in",)
    # Одна валюта — поведение sum_prices без изменений.
    same = sum_prices_or_unresolved([complete_rub, complete_rub], ["hotel", "outbound"])
    assert same == sum_prices([complete_rub, complete_rub], ["hotel", "outbound"])


def test_mixed_plan_budget_is_unknown_with_currency_reason(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_eur_outbound(monkeypatch)
    response = _send(client, trip_form(budget_amount="500000", budget_currency="RUB"))
    assert response.status_code == 200, response.text
    plan = response.json()["state"]["plan"]
    assert plan["total_price"]["unknown_reason"] == "currency_unresolved"
    budget = _budget(plan)
    assert budget["status"] == "unknown"
    assert budget["unknown_reason"] == "currency_unresolved"
    assert "валют" in budget["reason"]


def test_mixed_with_required_budget_does_not_fail_and_keeps_component_prices(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_eur_outbound(monkeypatch)
    response = _send(client, trip_form(budget_amount="1000", budget_currency="RUB", budget_required=True))
    assert response.status_code == 200, response.text
    state = response.json()["state"]
    plan = state["plan"]
    assert plan["total_price"]["amount"] is None
    assert _budget(plan)["status"] != "matched"
    outbound_id = next(c["offer_id"] for c in plan["components"] if c["role"] == "outbound")
    outbound = next(o for o in state["transport_offers"] if o["offer_id"] == outbound_id)
    assert outbound["price"]["currency"] == "EUR" and Decimal(outbound["price"]["amount"]) == Decimal("100")


def test_mixed_rebuild_after_budget_change(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _inject_eur_outbound(monkeypatch)
    turn = _send(client, trip_form(budget_amount="50000", budget_currency="RUB")).json()
    response = client.post(
        f"{API}/trips/{turn['trip_id']}/replan",
        json={
            "request_id": "req-mixed2-0002",
            "trip_revision": turn["trip_revision"],
            "change_set": {"budget_amount": "900000", "budget_currency": "RUB"},
        },
    )
    assert response.status_code == 200, response.text
    plan = response.json()["state"]["plan"]
    assert plan["total_price"]["unknown_reason"] == "currency_unresolved"
    assert _budget(plan)["status"] == "unknown"


def test_inspiration_with_mixed_currencies_is_not_http_500(client: Any, application: Any) -> None:
    """Q-07: путь вдохновения ``_candidate_estimate`` при гостинице в RUB и транспорте в EUR."""
    original = application.registry.invoke

    def foreign_transport(request: Any) -> Any:
        result = original(request)
        if request.tool_name == "search_transport" and isinstance(result.data, dict):
            offers = [_eur(offer) for offer in result.data.get("offers") or []]
            result = result.model_copy(update={"data": {**result.data, "offers": offers}})
        return result

    application.registry.invoke = foreign_transport
    response = _send(client, trip_form(destination=None, wishes_tags=["море"]))
    assert response.status_code == 200, response.text
    turn = response.json()
    candidates = turn["state"]["candidate_destinations"]
    assert candidates
    for candidate in candidates:
        cost = candidate["estimated_cost"]
        assert cost is not None
        assert (cost["amount"], cost["currency"], cost["total_completeness"], cost["unknown_reason"]) == (
            None,
            None,
            "unknown",
            "currency_unresolved",
        )
        assert "разных валютах" in candidate["cost_note"]
        assert candidate["place"]["name"] in turn["assistant_text"]
