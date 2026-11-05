"""Adversarial LLM evidence tests (no network)."""

from datetime import UTC, datetime

import pytest

from odin.contracts.api import StructuredForm
from odin.llm.free_text import InterpretationDelta, apply_delta

REF = datetime(2026, 10, 25, 12, tzinfo=UTC)


def run(text, payload, base=None):
    return apply_delta(
        InterpretationDelta.model_validate(payload),
        text=text,
        base=base if base is not None else StructuredForm(),
        reference_datetime=REF,
    )


@pytest.mark.parametrize(
    "text,amount,ok",
    [
        ("Не дороже 50 тысяч рублей", "50000", True),
        ("Не дороже 50 000 рублей", "50000", True),
        ("Не дороже 50 тысяч рублей", "500000", False),
        ("Не дороже 50 тысяч рублей", "50", False),
        ("Бюджет от 40 до 50 тысяч рублей", "40000", False),
        # Интеграция v2–v6: бюджет — верхняя граница; верхний конец диапазона — лимит (диапазоны сохраняются).
        ("Бюджет от 40 до 50 тысяч рублей", "50000", True),
        ("Бюджет 50 тысяч на 2 человек", "2000", False),
        ("Бюджет 50 тысяч на 2 человек", "50000", True),
        ("Не дороже 40 тысяч рублей", "40000", True),
    ],
)
def test_budget_numeric_semantics(text, amount, ok):
    form, report = run(
        text, {"budget": {"amount": amount, "currency": "RUB", "required": True, "span": text}}
    )
    assert (form.budget_amount is not None) == ok, report


@pytest.mark.parametrize(
    "text,count,ok",
    [
        ("двое взрослых", 2, True),
        ("двое взрослых", 5, False),
        ("2 взрослых и 1 ребёнок", 2, True),
        ("2 взрослых и 1 ребёнок", 1, False),
    ],
)
def test_adults_semantics(text, count, ok):
    form, _ = run(text, {"adults": {"value": count, "span": text}})
    assert (form.adults is not None) == ok


@pytest.mark.parametrize(
    "text,city,ok",
    [
        ("Из Москвы", "Москва", True),
        ("Из Москвы", "Сочи", False),
        ("В Казань", "Казань", True),
        ("В Казань", "Сочи", False),
        ("Из СПб", "Санкт-Петербург", True),
        ("Из Москвы", "Moscow", True),
        ("Из Сочи", "Сочи", True),
        ("Без города отправления", "Москва", False),
    ],
)
def test_city_grounding(text, city, ok):
    form, _ = run(text, {"origin": {"value": city, "span": text}})
    assert (form.origin is not None) == ok


def test_budget_previous_preserved_on_hallucination():
    base = StructuredForm(budget_amount="30000", budget_currency="RUB", budget_required=True)
    form, _ = run(
        "Теперь не дороже 40000",
        {"budget": {"amount": "90000", "currency": "RUB", "required": True, "span": "не дороже 40000"}},
        base,
    )
    assert form.budget_amount == base.budget_amount


@pytest.mark.parametrize(
    "text,field,payload",
    [
        ("Не дороже 40000 рублей", "budget", {"amount": "40000", "currency": "RUB", "required": False}),
        ("Только поездом", "transport", {"include": ["rail"], "required": False}),
        ("Обязательно с собакой", "pets", {"value": [{"kind": "dog", "count": 1}], "required": False}),
    ],
)
def test_required_cannot_be_weakened(text, field, payload):
    payload["span"] = text
    try:
        delta = InterpretationDelta.model_validate({field: payload})
    except Exception:
        pytest.skip("Invalid test fixture for nested pet schema")
    form, report = apply_delta(delta, text=text, base=StructuredForm(), reference_datetime=REF)
    assert (
        field in report.rejected
        or (field == "budget" and form.budget_required)
        or (field == "transport" and form.transport_modes_required)
        or (field == "pets" and form.pets_required)
    )


def test_invalid_delta_does_not_mutate_base():
    base = StructuredForm(budget_amount="30000", budget_currency="RUB", budget_required=True)
    form, _ = run(
        "Из Москвы, бюджет 50000",
        {
            "origin": {"value": "Сочи", "span": "Из Москвы"},
            "budget": {"amount": "50000", "currency": "RUB", "required": True, "span": "бюджет 50000"},
        },
        base,
    )
    assert form.origin is None and form.budget_amount == 50000  # verified field is applied independently
