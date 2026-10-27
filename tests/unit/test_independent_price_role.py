"""Independent reproductions on archived 2026-10-25 snapshot; no production mutations."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from odin.contracts import Constraint
from odin.contracts.api import StructuredForm
from odin.core.constraints import OfferFacts, evaluate_constraint
from odin.llm.free_text import apply_delta, parse_delta


def budget_constraint(source_span):
    return Constraint(
        constraint_id="budget-1",
        field="budget_total",
        operator="lte",
        value="50000",
        requiredness="required",
        source_span=source_span,
        source_kind="text_span",
        scope="trip",
        confidence=None,
        ambiguity=None,
    )


@pytest.mark.parametrize("source_span", ["бюджет 50000 евро", "бюджет 50000 долларов"])
def test_price_006_03_explicit_budget_currency_must_not_be_matched_to_rub(source_span):
    facts = OfferFacts(
        component_id="hotel-1",
        role="hotel",
        total_amount=Decimal("45000"),
        currency="RUB",
        price_completeness="complete",
    )
    assert evaluate_constraint(budget_constraint(source_span), facts).status == "unknown"


@pytest.mark.parametrize("origin,destination", [("Казань", "Москва"), ("Сочи", "Москва")])
def test_int_role_rejects_wrong_direction(origin, destination):
    text = "Хочу из Москвы в Казань"
    delta = parse_delta(
        {"origin": {"value": origin, "span": text}, "destination": {"value": destination, "span": text}}
    )
    assert delta is not None
    result, report = apply_delta(
        delta, text=text, base=StructuredForm(), reference_datetime=datetime(2026, 10, 25, tzinfo=UTC)
    )
    assert "origin" in report.rejected or "destination" in report.rejected
