"""Independent historical-snapshot QA: no network, real API and persisted cart."""

from decimal import Decimal

import pytest

from odin.core.money import CurrencyMixError, sum_prices
from odin.core.trip import evaluate_budget
from odin.orchestrator import Orchestrator
from tests.conftest import trip_form
from tests.samples import price, trip_state


def test_price_totals_contract():
    def complete(amount, currency):
        return price(
            amount=Decimal(amount) if amount is not None else None,
            currency=currency,
            total_completeness="complete" if amount is not None else "unknown",
            unknown_reason=None if amount is not None else "not_provided_by_source",
            known_fees=[],
            mandatory_fees_status="included",
        )

    x = complete("30000", "RUB")
    y = complete("100", "RUB")
    result = sum_prices([x, y], ["hotel", "outbound"])
    assert (
        result.amount,
        result.currency,
        result.completeness,
        result.unknown_reason,
        result.partial_components,
    ) == (Decimal("30100"), "RUB", "complete", None, ())
    missing = complete(None, "RUB")
    partial = sum_prices([x, missing], ["hotel", "outbound"])
    assert (
        partial.amount,
        partial.currency,
        partial.completeness,
        partial.unknown_reason,
        partial.partial_components,
    ) == (Decimal("30000"), "RUB", "partial", "price_incomplete", ("outbound",))
    with pytest.raises(CurrencyMixError):
        sum_prices([x, complete("100", "EUR")], ["hotel", "outbound"])
    with pytest.raises(CurrencyMixError):
        sum_prices([x, complete("100", "EUR"), complete("100", "USD")], ["hotel", "outbound", "transfer_in"])
    assert x.amount == Decimal("30000") and x.currency == "RUB"


def test_mixed_build_plan_exact(application):
    state = trip_state()
    hotel = state.hotel_offers[0]
    transport = state.transport_offers[0]
    eur = transport.model_copy(
        update={
            "price": transport.price.model_copy(
                update={
                    "amount": Decimal("100"),
                    "currency": "EUR",
                    "total_completeness": "complete",
                    "unknown_reason": None,
                }
            )
        }
    )
    state = state.model_copy(update={"transport_offers": [eur], "selected_components": {}})
    built = application.orchestrator._build_plan(state, state.request)
    total = built.plan.total_price
    assert (total.amount, total.currency, total.total_completeness, total.unknown_reason) == (
        None,
        None,
        "unknown",
        "currency_unresolved",
    )
    assert built.plan.completeness == "unknown"
    assert any("валют" in x for x in built.plan.unknowns)
    assert built.transport_offers[0].price.amount == Decimal("100")
    assert built.transport_offers[0].price.currency == "EUR"
    assert built.hotel_offers[0].price_total == hotel.price_total
    budget = [e for e in built.plan.constraint_evaluations if e.constraint_id == "c-budget_total"]
    assert all(e.status != "matched" for e in budget)


def test_mixed_http_cart_persisted_and_consent(client, application, monkeypatch):
    original = Orchestrator._build_plan

    def mixed(self, state, request):
        if state.transport_offers:
            changed = []
            for offer in state.transport_offers:
                if offer.direction == "outbound":
                    new_price = offer.price.model_copy(
                        update={
                            "amount": Decimal("100"),
                            "currency": "EUR",
                            "total_completeness": "complete",
                            "unknown_reason": None,
                        }
                    )
                    offer = offer.model_copy(update={"price": new_price})
                changed.append(offer)
            state = state.model_copy(update={"transport_offers": changed})
        return original(self, state, request)

    monkeypatch.setattr(Orchestrator, "_build_plan", mixed)
    session = client.post("/api/v1/sessions")
    assert session.status_code == 201
    s = session.json()
    response = client.post(
        f"/api/v1/sessions/{s['session_id']}/messages",
        json={
            "request_id": "req-iter3-0001",
            "trip_revision": s["trip_revision"],
            "form": trip_form(budget_amount=50000, budget_currency="RUB"),
        },
    )
    assert response.status_code == 200, response.text
    turn = response.json()
    plan = turn["state"]["plan"]
    total = plan["total_price"]
    assert (total["amount"], total["currency"], total["total_completeness"], total["unknown_reason"]) == (
        None,
        None,
        "unknown",
        "currency_unresolved",
    )
    assert any("валют" in x for x in plan["unknowns"])
    assert all(
        e["status"] != "matched"
        for e in plan["constraint_evaluations"]
        if e["constraint_id"] == "c-budget_total"
    )
    cart_response = client.post(
        f"/api/v1/trips/{turn['trip_id']}/cart",
        json={"request_id": "req-iter3-0002", "trip_revision": turn["trip_revision"]},
    )
    assert cart_response.status_code == 201, cart_response.text
    cart = cart_response.json()["cart"]
    assert cart["items"]
    assert cart["total_price"]["amount"] is None and cart["total_price"]["currency"] is None
    assert any("валют" in x for x in cart["significant_unknowns"])
    outbound = [i for i in cart["items"] if i["role"] == "outbound"]
    assert len(outbound) == 1
    assert outbound[0]["price"]["currency"] == "EUR" and Decimal(
        str(outbound[0]["price"]["amount"])
    ) == Decimal("100")
    stored = client.get(f"/api/v1/trips/{turn['trip_id']}/cart")
    assert stored.status_code == 200 and stored.json()["cart"]["items"] == cart["items"]
    url = f"/api/v1/carts/{cart['cart_id']}/confirm"
    payload = {
        "request_id": "req-iter3-0003",
        "trip_revision": cart["trip_revision"],
        "confirmation_id": "conf-iter3-0001",
        "purchase_consent": True,
        "risk_acknowledgement": False,
    }
    denied = client.post(url, json=payload)
    assert denied.status_code == 400, denied.text
    approved = client.post(
        url, json={**payload, "request_id": "req-iter3-0004", "risk_acknowledgement": True}
    )
    assert approved.status_code == 200, approved.text
    receipt = approved.json()["receipt"]
    assert receipt["total_price"]["amount"] is None
    assert "Демонстрационная" in receipt["notice"]
    replay = client.post(url, json={**payload, "request_id": "req-iter3-0005", "risk_acknowledgement": True})
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True


def test_mixed_known_part_over_budget_is_not_matched():
    # Core cannot derive a same-currency lower bound from a mixed aggregate with amount=None.
    status, _, _ = evaluate_budget(Decimal("1000"), "RUB", None, None, "unknown")
    assert status == "unknown"


@pytest.mark.parametrize("foreign_role", ["transfer_in", "outbound"])
def test_mixed_rebuild_and_component_preservation(application, foreign_role):
    state = trip_state()
    original = state.transport_offers[0]
    foreign = original.model_copy(
        update={"price": original.price.model_copy(update={"amount": Decimal("111"), "currency": "EUR"})}
    )
    if foreign_role == "transfer_in":
        foreign = foreign.model_copy(update={"kind": "transfer", "direction": "transfer_in"})
        state = state.model_copy(
            update={"transport_offers": [], "transfer_options": [foreign], "selected_components": {}}
        )
    else:
        state = state.model_copy(update={"transport_offers": [foreign], "selected_components": {}})
    first = application.orchestrator._build_plan(state, state.request)
    second = application.orchestrator._build_plan(first, first.request)
    for built in (first, second):
        total = built.plan.total_price
        assert (total.amount, total.currency, total.total_completeness, total.unknown_reason) == (
            None,
            None,
            "unknown",
            "currency_unresolved",
        )
        assert any("валют" in u for u in built.plan.unknowns)
        offers = built.transfer_options if foreign_role == "transfer_in" else built.transport_offers
        assert offers[0].price.currency == "EUR" and offers[0].price.amount == Decimal("111")


def test_missing_currency_and_component_are_not_false_complete():
    missing = price(
        amount=None, currency=None, total_completeness="unknown", unknown_reason="not_provided_by_source"
    )
    known = price(
        amount=Decimal("400"),
        currency="RUB",
        total_completeness="complete",
        unknown_reason=None,
        known_fees=[],
        mandatory_fees_status="included",
    )
    total = sum_prices([known, missing], ["hotel", "outbound"])
    assert (total.amount, total.currency, total.completeness, total.partial_components) == (
        Decimal("400"),
        "RUB",
        "partial",
        ("outbound",),
    )
    with pytest.raises(ValueError, match="сумма без валюты"):
        price(
            amount=Decimal("100"),
            currency=None,
            total_completeness="partial",
            unknown_reason="price_incomplete",
        )
