"""Independent v6: price integrity and budget currency provenance."""
from decimal import Decimal

import pytest

from odin.contracts import PlanComponent
from odin.core.trip import evaluate_budget
from odin.orchestrator import Orchestrator
from tests.samples import trip_state


@pytest.mark.parametrize('amount,completeness', [
    (Decimal('48000'), 'complete'),
    (Decimal('55000'), 'complete'),
    (Decimal('48000'), 'partial'),
    (Decimal('55000'), 'partial'),
])
def test_unknown_currency_never_matches_or_violates_rub_budget(amount, completeness):
    status, deviation, _ = evaluate_budget(Decimal('50000'), 'RUB', amount, None, completeness)
    assert status == 'unknown'
    assert deviation is None


def test_cart_component_never_falls_back_to_whole_trip_price():
    state = trip_state()
    missing = PlanComponent(
        component_id='outbound', role='outbound', offer_id='not-a-real-offer',
        kind='rail', title='Missing', data_mode='mock',
    )
    orch = object.__new__(Orchestrator)
    with pytest.raises(Exception, match='цена компонента'):
        orch._component_price(state, missing)


def test_known_budget_currency_remains_matched():
    assert evaluate_budget(Decimal('50000'), 'RUB', Decimal('48000'), 'RUB', 'complete')[0] == 'matched'


def test_known_budget_currency_excess_remains_violated():
    assert evaluate_budget(Decimal('50000'), 'RUB', Decimal('55000'), 'RUB', 'partial')[0] == 'violated'


def test_foreign_currency_is_unknown():
    assert evaluate_budget(Decimal('50000'), 'RUB', Decimal('48000'), 'EUR', 'complete')[0] == 'unknown'
