"""Regression: a literal span is not evidence that the LLM's numeric value is correct."""
from datetime import UTC, datetime

import pytest

from odin.contracts.api import StructuredForm
from odin.llm.free_text import InterpretationDelta, apply_delta

REF = datetime(2026, 10, 25, 12, tzinfo=UTC)


def _apply(text, payload, base=None):
    return apply_delta(InterpretationDelta.model_validate(payload), text=text,
                       base=base or StructuredForm(), reference_datetime=REF)


@pytest.mark.parametrize('text,amount', [
    ('Бюджет не дороже 50000 рублей', '1000000'),
    ('Не больше 50 000 рублей', '90000'),
    ('Бюджет 50 тысяч рублей', '100000'),
])
def test_budget_cannot_exceed_number_in_cited_span(text, amount):
    form, report = _apply(text, {'budget': {'amount': amount, 'currency': 'RUB',
                                           'required': True, 'span': text}})
    assert form.budget_amount is None
    assert 'budget' in report.rejected


def test_budget_correct_number_is_accepted():
    form, report = _apply('Не дороже 50 000 рублей', {'budget': {
        'amount': '50000', 'currency': 'RUB', 'required': True,
        'span': 'Не дороже 50 000 рублей'}})
    assert str(form.budget_amount) == '50000.00'
    assert 'budget' in report.accepted


def test_numeric_adults_cannot_be_hallucinated():
    form, report = _apply('Нас 2 взрослых', {'adults': {'value': 9, 'span': '2 взрослых'}})
    assert form.adults is None
    assert 'adults' in report.rejected


def test_correct_numeric_adults_accepted():
    form, report = _apply('Нас 2 взрослых', {'adults': {'value': 2, 'span': '2 взрослых'}})
    assert form.adults == 2
    assert 'adults' in report.accepted
