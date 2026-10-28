"""PRICE-006-03 / MIXED-LOWER-BOUND-02 (этап 2): явная валюта бюджета и нижняя граница в валюте бюджета.

DEC-17: без утверждённого курса валюты не смешиваются. Валюта бюджета берётся только из запроса
(``TripRequest.budget.currency``), а не из валюты предложения или текста условия. Только Mock и
подмена цен в памяти: без сети, платных API и покупок.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from odin.contracts import Constraint
from odin.core.constraints import OfferFacts, evaluate_constraint
from odin.core.money import known_amount_in_currency
from odin.orchestrator import Orchestrator
from tests.conftest import trip_form
from tests.samples import price

pytestmark = [pytest.mark.e2e_mock, pytest.mark.req("PRICE-006-03", "DEC-17")]

API = "/api/v1"


def _budget_constraint(value: str = "50000", span: str = "бюджет 50000 рублей") -> Constraint:
    return Constraint(
        constraint_id="c-budget_total",
        field="budget_total",
        operator="lte",
        value=value,
        requiredness="required",
        source_span=span,
        source_kind="text_span",
        scope="trip",
        confidence=None,
        ambiguity=None,
    )


# ---------------------------------------------------------------- PRICE-006-03 (unit)


def test_budget_currency_is_taken_from_request_not_from_offer() -> None:
    """EUR-бюджет и RUB-цена не сопоставимы: matched запрещён, даже если числа близки."""
    facts = OfferFacts(
        component_id="hotel-1",
        role="hotel",
        total_amount=Decimal("45000"),
        currency="RUB",
        price_completeness="complete",
        budget_currency="EUR",
    )
    result = evaluate_constraint(_budget_constraint(span="до 50000 евро"), facts)
    assert result.status == "unknown"
    assert result.unknown_reason == "currency_unresolved"
    assert "DEC-17" in result.reason


def test_missing_budget_currency_is_unknown_not_rub_guess() -> None:
    """Без явной валюты бюджета оценка не доказуема: прежняя подстановка «RUB» удалена."""
    facts = OfferFacts(
        component_id="hotel-1",
        role="hotel",
        total_amount=Decimal("45000"),
        currency="RUB",
        price_completeness="complete",
    )
    result = evaluate_constraint(_budget_constraint(), facts)
    assert result.status == "unknown"
    assert result.unknown_reason == "data_missing"


def test_same_currency_budget_still_matches_and_violates() -> None:
    """Одновалютный путь не изменился: сравнение, matched и violated работают как раньше."""
    fits = OfferFacts(
        component_id="hotel-1",
        role="hotel",
        total_amount=Decimal("45000"),
        currency="RUB",
        price_completeness="complete",
        budget_currency="RUB",
    )
    assert evaluate_constraint(_budget_constraint(), fits).status == "matched"
    over = OfferFacts(
        component_id="hotel-1",
        role="hotel",
        total_amount=Decimal("60000"),
        currency="RUB",
        price_completeness="complete",
        budget_currency="RUB",
    )
    violated = evaluate_constraint(_budget_constraint(), over)
    assert violated.status == "violated"
    assert violated.deviation is not None and violated.deviation.amount == Decimal("10000")


def test_known_amount_in_currency_keeps_only_matching_currency() -> None:
    """Нижняя граница считается только по компонентам в валюте бюджета (MIXED-LOWER-BOUND-02)."""
    rub = price(
        amount=Decimal("60000"),
        currency="RUB",
        total_completeness="complete",
        mandatory_fees_status="included",
        known_fees=[],
        unknown_reason=None,
    )
    eur = rub.model_copy(update={"amount": Decimal("100"), "currency": "EUR"})
    unknown = price(
        amount=None, currency=None, total_completeness="unknown", unknown_reason="not_provided_by_source"
    )
    assert known_amount_in_currency([rub, eur, unknown], "RUB") == Decimal("60000")
    assert known_amount_in_currency([eur, unknown], "RUB") is None
    assert known_amount_in_currency([], "RUB") is None


# ---------------------------------------------------------------- нижняя граница (HTTP, Mock)


def _eur(offer: Any, amount: str | None = "100") -> Any:
    known_fees = [] if amount is None else offer.price.known_fees
    new_price = offer.price.model_copy(
        update={
            "amount": None if amount is None else Decimal(amount),
            "currency": "EUR",
            "total_completeness": "unknown" if amount is None else "complete",
            "mandatory_fees_status": "unknown" if amount is None else "included",
            "known_fees": known_fees,
            "unknown_reason": "not_provided_by_source" if amount is None else None,
        }
    )
    return offer.model_copy(update={"price": new_price})


def _inject(monkeypatch: pytest.MonkeyPatch, *, hotel_amount: str, eur_transport: bool = True) -> None:
    """Гостиница в RUB на заданную сумму и (по умолчанию) исходящий транспорт в EUR."""
    original = Orchestrator._build_plan

    def mutated(self: Orchestrator, state: Any, request: Any) -> Any:
        hotels = [
            offer.model_copy(
                update={
                    "price_total": offer.price_total.model_copy(
                        update={
                            "amount": Decimal(hotel_amount),
                            "currency": "RUB",
                            "total_completeness": "complete",
                            "mandatory_fees_status": "included",
                            "unknown_reason": None,
                        }
                    )
                }
            )
            for offer in state.hotel_offers
        ]
        transports = [
            _eur(o) if eur_transport and o.direction == "outbound" else o for o in state.transport_offers
        ]
        return original(
            self, state.model_copy(update={"hotel_offers": hotels, "transport_offers": transports}), request
        )

    monkeypatch.setattr(Orchestrator, "_build_plan", mutated)


def _send(client: Any, form: dict[str, Any]) -> Any:
    session = client.post(f"{API}/sessions").json()
    return client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-lower-0001", "trip_revision": session["trip_revision"], "form": form},
    )


def _budget(plan: dict[str, Any]) -> dict[str, Any]:
    return next(e for e in plan["constraint_evaluations"] if e["constraint_id"] == "c-budget_total")


def test_known_budget_currency_part_over_budget_is_violated(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60 000 RUB + 100 EUR при бюджете 50 000 RUB: известная часть уже больше — violated, не unknown."""
    _inject(monkeypatch, hotel_amount="60000")
    response = _send(client, trip_form(budget_amount="50000", budget_currency="RUB", budget_required=True))
    assert response.status_code == 200, response.text
    plan = response.json()["state"]["plan"]
    assert plan["total_price"]["unknown_reason"] == "currency_unresolved"
    budget = _budget(plan)
    assert budget["status"] == "violated"
    assert budget["deviation"]["unit"] == "RUB"
    assert "других валютах" in budget["reason"]
    assert "другие валюты не учтены" in budget["deviation"]["description"]


def test_known_budget_currency_part_within_budget_stays_unknown(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """40 000 RUB + 100 EUR при бюджете 50 000 RUB: превышение не доказано — unknown, не matched."""
    _inject(monkeypatch, hotel_amount="40000")
    response = _send(
        client,
        trip_form(budget_amount="50000", budget_currency="RUB", budget_required=True, return_trip=False),
    )
    assert response.status_code == 200, response.text
    plan = response.json()["state"]["plan"]
    budget = _budget(plan)
    assert budget["status"] == "unknown"
    assert budget["unknown_reason"] == "currency_unresolved"
    assert plan["rank_class"] != 1, "неизвестный итог не может подтвердить обязательный бюджет"
