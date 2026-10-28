"""Регрессия интеграции аудита v2–v6 (25.10.2026): исправления INT-001…005, TREE-001, MONEY-001, PRICE-006-01
не ломают корректные русские формулировки, а ошибочные значения модели по-прежнему отклоняются.

Дополняет tests/unit/test_ru_forms_guard.py (падежи городов, состав, суммы) и тесты аудита
test_audit_v1_numeric_evidence.py, test_audit_v2_interpreter.py, test_audit_v3.py, test_audit_v6.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from odin.contracts import Constraint, ConstraintEvaluation, ConstraintGroup
from odin.contracts.api import StructuredForm
from odin.contracts.common import Money
from odin.core.constraints import OfferFacts, evaluate_constraint
from odin.core.tree import evaluate_tree
from odin.llm.free_text import InterpretationDelta, apply_delta, parse_delta

pytestmark = pytest.mark.req("INTERPRET-01", "UC-01", "T-UNIT-INTEGRATION-V2-V6")

REFERENCE = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def _apply(
    text: str, payload: dict[str, Any], base: StructuredForm | None = None
) -> tuple[StructuredForm, Any]:
    delta = InterpretationDelta.model_validate(payload)
    return apply_delta(delta, text=text, base=base or StructuredForm(), reference_datetime=REFERENCE)


# ---------------------------------------------------------------- INT-001/INT-004: бюджет


@pytest.mark.parametrize(
    ("text", "span", "amount", "accepted"),
    [
        ("Бюджет от 40 до 50 тысяч рублей", "от 40 до 50 тысяч рублей", "50000", True),
        ("Бюджет от 40 до 50 тысяч рублей", "от 40 до 50 тысяч рублей", "40000", False),
        ("Бюджет 40–50 тысяч рублей", "40–50 тысяч рублей", "50000", True),
        ("Бюджет 40-50 тысяч рублей", "40-50 тысяч рублей", "40000", False),
        ("Бюджет от 40 тысяч рублей", "от 40 тысяч рублей", "40000", False),  # только нижняя граница
        ("До 50к рублей", "До 50к рублей", "50000", True),
        ("До 1,5 млн рублей", "До 1,5 млн рублей", "1500000", True),
        ("До 1,5 млн рублей", "До 1,5 млн рублей", "15000000", False),
        ("Не больше 120 тыс.руб.", "Не больше 120 тыс.руб.", "120000", True),
        ("Бюджет сорок тысяч рублей", "сорок тысяч рублей", "40000", True),  # слова не проверяются
        ("Бюджет 60 000 рублей на двоих на 3 ночи", "60 000 рублей на двоих на 3 ночи", "60000", True),
        ("Бюджет 60 000 рублей на двоих на 3 ночи", "60 000 рублей на двоих на 3 ночи", "3", False),
        ("Примерно 800 евро", "800 евро", "800", True),
    ],
)
def test_budget_amount_must_match_quote(text: str, span: str, amount: str, accepted: bool) -> None:
    currency = "EUR" if "евро" in text else "RUB"
    payload = {"budget": {"amount": amount, "currency": currency, "required": False, "span": span}}
    form, report = _apply(text, payload)
    assert (form.budget_amount is not None) is accepted, report.notes
    if not accepted:
        assert "budget" in report.rejected
        assert any("число не совпадает" in note for note in report.notes)


# ---------------------------------------------------------------- INT-001/INT-004: состав


@pytest.mark.parametrize(
    ("text", "span", "adults", "accepted"),
    [
        ("Едем вдвоём с ребёнком 5 лет", "вдвоём с ребёнком 5 лет", 2, True),
        ("Я и двое детей", "Я и двое детей", 1, True),
        ("Семья из 4 человек: двое взрослых и двое детей", "двое взрослых и двое детей", 2, True),
        ("Нас трое", "Нас трое", 3, True),
        ("Нас трое", "Нас трое", 2, False),
        ("Два взрослых и сын 7 лет", "Два взрослых и сын 7 лет", 7, False),
    ],
)
def test_adults_number_must_match_quote(text: str, span: str, adults: int, accepted: bool) -> None:
    form, report = _apply(text, {"adults": {"value": adults, "span": span}})
    assert (form.adults == adults) is accepted, report.notes


# ---------------------------------------------------------------- INT-002: подмена города


@pytest.mark.parametrize(
    ("text", "field", "value", "span"),
    [
        ("Выезжаем из Твери", "origin", "Тула", "из Твери"),
        ("Возвращаемся из Перми", "origin", "Пенза", "из Перми"),
        ("Хочу из Орла в Москву", "origin", "Омск", "из Орла"),
        ("Билеты до Сочи", "destination", "Самара", "до Сочи"),
        ("Хочу из Москвы в Казань", "destination", "Москва", "в Казань"),
    ],
)
def test_substituted_city_is_rejected(text: str, field: str, value: str, span: str) -> None:
    form, report = _apply(text, {field: {"value": value, "span": span}})
    assert getattr(form, field) is None
    assert field in report.rejected
    assert any("город не назван" in note for note in report.notes)


# ---------------------------------------------------------------- INT-003: обязательность


@pytest.mark.parametrize(
    ("text", "field", "payload", "attr", "expected"),
    [
        # явная обязательность важнее ответа модели
        (
            "Не дороже 40000 рублей",
            "budget",
            {"amount": "40000", "currency": "RUB", "required": False},
            "budget_required",
            True,
        ),
        ("Только 4 звезды", "stars_min", {"value": 4, "required": False}, "stars_required", True),
        # явное пожелание не превращается в обязательное
        (
            "Желательно с завтраком",
            "breakfast",
            {"value": True, "required": True},
            "breakfast_required",
            False,
        ),
        # «не обязательно» — не признак обязательности (ошибка исходного регулярного выражения аудита)
        (
            "Завтрак не обязательно",
            "breakfast",
            {"value": True, "required": True},
            "breakfast_required",
            False,
        ),
        (
            "Отмена необязательна",
            "free_cancellation",
            {"value": True, "required": True},
            "free_cancellation_required",
            False,
        ),
        # противоречивые признаки — решение модели сохраняется
        (
            "Желательно не дороже 50 тысяч рублей",
            "budget",
            {"amount": "50000", "currency": "RUB", "required": False},
            "budget_required",
            False,
        ),
        # признаков нет — решение модели сохраняется
        (
            "Уложиться в 50 тысяч рублей",
            "budget",
            {"amount": "50000", "currency": "RUB", "required": True},
            "budget_required",
            True,
        ),
    ],
)
def test_requiredness_follows_explicit_wording(
    text: str, field: str, payload: dict[str, Any], attr: str, expected: bool
) -> None:
    form, report = _apply(text, {field: {**payload, "span": text}})
    assert field in report.accepted, report.notes
    assert getattr(form, attr) is expected


# ---------------------------------------------------------------- INT-005: отмена условий

_BASE = StructuredForm(
    budget_amount="40000",
    budget_currency="RUB",
    budget_required=True,
    transport_modes=["rail"],
    transport_modes_required=True,
    pets=[{"kind": "dog", "count": 1}],
    pets_required=True,
    breakfast=True,
    breakfast_required=True,
    stars_min=4,
    stars_required=True,
    free_cancellation=True,
    free_cancellation_required=True,
    children_ages=[5],
    return_trip=True,
)

_CLEARED = {
    "budget": ("budget_amount", None),
    "transport": ("transport_modes", []),
    "pets": ("pets", []),
    "breakfast": ("breakfast", None),
    "stars_min": ("stars_min", None),
    "free_cancellation": ("free_cancellation", None),
    "children_ages": ("children_ages", []),
    "return_trip": ("return_trip", None),
}

VALID_CANCELLATIONS = [
    ("budget", "Бюджет не важен"),
    ("budget", "Бюджет больше не важен"),
    ("budget", "Без ограничений по бюджету"),
    ("budget", "Любой бюджет"),
    ("budget", "Цена не важна"),
    ("budget", "Неважно, сколько стоит"),
    ("budget", "Забудь про бюджет"),
    ("budget", "Бюджет не ограничен"),
    ("transport", "Можно и самолётом"),
    ("transport", "Самолёт тоже подойдёт"),
    ("transport", "Любой транспорт"),
    ("transport", "Не обязательно поездом"),
    ("transport", "Всё равно, на чём ехать"),
    ("pets", "Теперь без собаки"),
    ("pets", "Собака не едет"),
    ("breakfast", "Завтрак не нужен"),
    ("breakfast", "Можно без завтрака"),
    ("breakfast", "Завтрак больше не обязателен"),
    ("stars_min", "Звёзды не важны"),
    ("stars_min", "Любая звёздность"),
    ("free_cancellation", "Бесплатная отмена не нужна"),
    ("children_ages", "Едем без детей"),
    ("children_ages", "Дети остаются дома"),
    ("children_ages", "Только взрослые"),
    ("return_trip", "Обратный билет не нужен"),
    ("return_trip", "Только в одну сторону"),
]


@pytest.mark.parametrize(("field", "text"), VALID_CANCELLATIONS)
def test_valid_russian_cancellations_are_applied(field: str, text: str) -> None:
    form, report = _apply(text, {"clear": [{"field": field, "span": text}]}, _BASE)
    attr, cleared = _CLEARED[field]
    assert getattr(form, attr) == cleared, report.notes
    assert f"clear:{field}" in report.accepted


MISLEADING_CANCELLATIONS = [
    ("budget", "Бюджет до 40000, транспорт не важен"),
    ("budget", "Бюджет до 40000 и транспорт не важен"),
    ("budget", "Дорогой отель не нужен"),
    ("pets", "С собакой, транспорт не важен"),
    ("breakfast", "Завтрак обязательно, звёзды не важны"),
    ("transport", "Поезд без пересадок"),
    ("transport", "Только поездом и звёзды не важны"),
    ("stars_min", "Не ниже 4 звёзд"),
    ("return_trip", "Туда и обратно"),
]


@pytest.mark.parametrize(("field", "text"), MISLEADING_CANCELLATIONS)
def test_cancellation_without_explicit_revocation_keeps_condition(field: str, text: str) -> None:
    form, report = _apply(text, {"clear": [{"field": field, "span": text}]}, _BASE)
    attr, _ = _CLEARED[field]
    assert getattr(form, attr) == getattr(_BASE, attr)
    assert f"clear:{field}" in report.rejected
    assert any("явной отмены" in note for note in report.notes)


def test_change_of_agreed_budget_and_transport_in_one_message() -> None:
    """Изменение ранее согласованных параметров: новое значение и отмена в одном сообщении."""
    text = "Теперь бюджет до 60 тысяч рублей, и можно и самолётом"
    payload = {
        "budget": {
            "amount": "60000",
            "currency": "RUB",
            "required": False,
            "span": "бюджет до 60 тысяч рублей",
        },
        "clear": [{"field": "transport", "span": "можно и самолётом"}],
    }
    form, report = _apply(text, payload, _BASE)
    assert form.budget_amount == Decimal("60000")
    assert form.transport_modes == []
    assert form.breakfast is True and form.stars_min == 4  # прочие условия не потеряны


# ---------------------------------------------------------------- TREE-001: циклы и семантика OR


def _evaluation(cid: str, status: str) -> ConstraintEvaluation:
    return ConstraintEvaluation(
        constraint_id=cid,
        component_id="hotel",
        plan_id=None,
        status=status,  # type: ignore[arg-type]
        unknown_reason="data_missing" if status == "unknown" else None,
        evidence_refs=[],
        reason="test",
        deviation=None,
    )


def _group(gid: str, operator: str, children: list[str]) -> ConstraintGroup:
    return ConstraintGroup(
        group_id=gid,
        operator=operator,
        children=children,
        scope="hotel",
        activation=None,
        raw_text=None,  # type: ignore[arg-type]
    )


def test_cycle_result_does_not_depend_on_group_order() -> None:
    a, b = _group("a", "OR", ["b", "known"]), _group("b", "AND", ["a", "known"])
    evaluations = [_evaluation("known", "matched")]
    forward = evaluate_tree([a, b], evaluations).group_status
    backward = evaluate_tree([b, a], evaluations).group_status
    assert forward == backward == {"a": "unknown", "b": "unknown"}


def test_or_outside_cycle_keeps_matched_branch() -> None:
    """Семантика OR вне цикла не меняется: выполненная независимая ветвь даёт matched."""
    groups = [
        _group("root", "OR", ["known", "a"]),
        _group("a", "OR", ["b", "known"]),
        _group("b", "AND", ["a", "known"]),
    ]
    result = evaluate_tree(groups, [_evaluation("known", "matched")])
    assert result.group_status == {"root": "matched", "a": "unknown", "b": "unknown"}
    assert "цикл" in result.notes["a"]


def test_acyclic_or_semantics_unchanged() -> None:
    groups = [_group("inner", "AND", ["x", "y"]), _group("root", "OR", ["inner", "z"])]
    evaluations = [_evaluation("x", "matched"), _evaluation("y", "unknown"), _evaluation("z", "matched")]
    result = evaluate_tree(groups, evaluations)
    assert result.group_status == {"inner": "unknown", "root": "matched"}


# ---------------------------------------------------------------- INT-006 (найден при интеграции)


@pytest.mark.parametrize("amount", ["NaN", "nan", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_budget_from_model_is_rejected_not_raised(amount: str) -> None:
    """Ответ модели с «NaN» — неразобранная дельта (None), а не decimal.InvalidOperation в ходе диалога."""
    assert parse_delta({"budget": {"amount": amount, "currency": "RUB", "span": "бюджет"}}) is None


# ---------------------------------------------------------------- MONEY-001 и PRICE-006-01


@pytest.mark.parametrize("value", ["1000000000000", "1000000000000.00", "1E+12", 10**12, Decimal("1E+12")])
def test_money_rejects_more_than_twelve_integer_digits(value: Any) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(value)


def test_money_upper_bound_still_accepted() -> None:
    assert TypeAdapter(Money).validate_python("999999999999.99") == Decimal("999999999999.99")


@pytest.mark.parametrize("amount", [Decimal("10000"), Decimal("90000")])
def test_component_budget_with_unknown_currency_is_unknown(amount: Decimal) -> None:
    constraint = Constraint(
        constraint_id="c-budget_total",
        field="budget_total",
        operator="lte",
        value="50000",
        requiredness="required",
        source_span="не дороже 50000 рублей",
        source_kind="text_span",
        scope="trip",
        confidence=None,
        ambiguity=None,
    )
    facts = OfferFacts(
        component_id="o-1", role="hotel", total_amount=amount, currency=None, price_completeness="complete"
    )
    assert evaluate_constraint(constraint, facts).status == "unknown"
