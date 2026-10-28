"""Защитный набор для интеграции патчей Интерпретатора (INT-001…004, MONEY-001): корректные русские формулировки.

Каждый случай — правильная интерпретация с дословной цитатой. После любого ужесточения проверок (города, числа,
обязательность) эти значения должны по-прежнему приниматься: иначе патч ухудшает разбор обычных запросов.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from odin.contracts.api import StructuredForm
from odin.llm.free_text import InterpretationDelta, apply_delta

pytestmark = pytest.mark.req("INTERPRET-01", "UC-01", "T-UNIT-RU-FORMS-01")

REFERENCE = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)

# (текст, поле, значение от модели, цитата) — падежные формы, составные названия, «ё», дефисы.
CITIES = [
    ("Хочу из Москвы в Казань", "origin", "Москва", "из Москвы"),
    ("Хочу из Москвы в Казань", "destination", "Казань", "в Казань"),
    ("Поездка в Санкт-Петербург на выходные", "destination", "Санкт-Петербург", "в Санкт-Петербург"),
    ("Летим из Нижнего Новгорода", "origin", "Нижний Новгород", "из Нижнего Новгорода"),
    ("Хочу в Нижний Новгород", "destination", "Нижний Новгород", "в Нижний Новгород"),
    ("Едем из Ростова-на-Дону к морю", "origin", "Ростов-на-Дону", "из Ростова-на-Дону"),
    ("Отдохнуть в Ростове-на-Дону", "destination", "Ростов-на-Дону", "в Ростове-на-Дону"),
    ("Хочу три ночи в Казани", "destination", "Казань", "в Казани"),
    ("Билеты до Сочи", "destination", "Сочи", "до Сочи"),
    ("Из Екатеринбурга в Пермь", "origin", "Екатеринбург", "Из Екатеринбурга"),
    ("Из Екатеринбурга в Пермь", "destination", "Пермь", "в Пермь"),
    ("Возвращаемся из Перми", "origin", "Пермь", "из Перми"),
    ("Выезжаем из Твери", "origin", "Тверь", "из Твери"),
    ("Хочу из Орла в Москву", "origin", "Орёл", "из Орла"),
    ("Съездить в Йошкар-Олу", "destination", "Йошкар-Ола", "в Йошкар-Олу"),
    ("Поездка в Набережные Челны", "destination", "Набережные Челны", "в Набережные Челны"),
    ("Вылет из Набережных Челнов", "origin", "Набережные Челны", "из Набережных Челнов"),
    ("Отпуск в Великом Новгороде", "destination", "Великий Новгород", "в Великом Новгороде"),
    ("Из Тольятти в Самару", "origin", "Тольятти", "Из Тольятти"),
    ("Из Тольятти в Самару", "destination", "Самара", "в Самару"),
    ("В Ёшкар-Олу не хочу, хочу в Казань", "destination", "Казань", "в Казань"),
    ("Хочу из Питера в Москву", "origin", "Санкт-Петербург", "из Питера"),
]


@pytest.mark.parametrize(("text", "field", "value", "span"), CITIES)
def test_valid_city_forms_are_accepted(text: str, field: str, value: str, span: str) -> None:
    delta = InterpretationDelta.model_validate({field: {"value": value, "span": span}})
    form, report = apply_delta(delta, text=text, base=StructuredForm(), reference_datetime=REFERENCE)
    assert getattr(form, field) == value, (report.rejected, report.notes)
    assert field in report.accepted


PARTY = [
    ("Едем вдвоём", 2, "вдвоём"),
    ("Едем вдвоем", 2, "вдвоем"),
    ("Поедем втроём", 3, "втроём"),
    ("Нас четверо", 4, "Нас четверо"),
    ("Два взрослых", 2, "Два взрослых"),
    ("2 взрослых", 2, "2 взрослых"),
    ("Еду одна", 1, "одна"),
    ("Я с женой", 2, "Я с женой"),
]


@pytest.mark.parametrize(("text", "adults", "span"), PARTY)
def test_valid_party_forms_are_accepted(text: str, adults: int, span: str) -> None:
    delta = InterpretationDelta.model_validate({"adults": {"value": adults, "span": span}})
    form, report = apply_delta(delta, text=text, base=StructuredForm(), reference_datetime=REFERENCE)
    assert form.adults == adults, (report.rejected, report.notes)


BUDGETS = [
    ("Бюджет не дороже 60 000 рублей", "60000", "не дороже 60 000 рублей", True),
    ("Бюджет не дороже 60000 ₽", "60000", "не дороже 60000 ₽", True),
    ("Уложиться в 50 тысяч рублей", "50000", "в 50 тысяч рублей", False),
    ("До 1,5 млн рублей на всё", "1500000", "До 1,5 млн рублей", True),
    ("Не больше 120 тыс. руб.", "120000", "Не больше 120 тыс. руб.", True),
    ("Примерно 800 евро", "800", "800 евро", False),
    ("Бюджет строго 45 500 руб", "45500", "строго 45 500 руб", True),
]


@pytest.mark.parametrize(("text", "amount", "span", "required"), BUDGETS)
def test_valid_budget_forms_are_accepted(text: str, amount: str, span: str, required: bool) -> None:
    currency = "EUR" if "евро" in text else "RUB"
    delta = InterpretationDelta.model_validate(
        {"budget": {"amount": amount, "currency": currency, "required": required, "span": span}}
    )
    form, report = apply_delta(delta, text=text, base=StructuredForm(), reference_datetime=REFERENCE)
    assert form.budget_amount is not None and int(form.budget_amount) == int(amount), (
        report.rejected,
        report.notes,
    )
    assert form.budget_required is required and form.budget_currency == currency


REQUIRED_WORDING = [
    ("Только поездом", {"include": ["rail"], "required": True, "span": "Только поездом"}, ["rail"]),
    ("Не самолётом", {"exclude": ["flight"], "required": True, "span": "Не самолётом"}, ["rail"]),
    (
        "Без самолётов, пожалуйста",
        {"exclude": ["flight"], "required": True, "span": "Без самолётов"},
        ["rail"],
    ),
    (
        "Можно поездом или самолётом",
        {"include": ["rail", "flight"], "span": "поездом или самолётом"},
        ["rail", "flight"],
    ),
]


@pytest.mark.parametrize(("text", "transport", "modes"), REQUIRED_WORDING)
def test_transport_wording_is_accepted(text: str, transport: dict, modes: list[str]) -> None:
    delta = InterpretationDelta.model_validate({"transport": transport})
    form, report = apply_delta(delta, text=text, base=StructuredForm(), reference_datetime=REFERENCE)
    assert form.transport_modes == modes, (report.rejected, report.notes)
    assert form.transport_modes_required is bool(transport.get("required") or transport.get("exclude"))
