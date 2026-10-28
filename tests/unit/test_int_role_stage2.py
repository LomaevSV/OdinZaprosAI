"""INT-ROLE (этап 2): город отправления и назначения определяется ролью, а не порядком слов.

Аудит 26.10: при тексте «из Москвы в Казань» модель могла вернуть переставленные города, и код принимал
значение, потому что город назван в цитате. Здесь фиксируются запланированные формы, инверсия, отрицание
и контрольные случаи, где роль в цитате не выражена (поведение прежнее — проверка упоминания).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from odin.contracts.api import StructuredForm
from odin.llm.free_text import InterpretationDelta, apply_delta

pytestmark = pytest.mark.req("INT-ROLE", "INTERPRET-01")

REFERENCE = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)

# (текст, поле, значение модели, ожидается ли принятие) — роль доказуемо противоречит цитате.
ROLE_CONFLICTS = [
    ("Из Москвы в Казань", "origin", "Казань", False),
    ("Из Москвы в Казань", "destination", "Москва", False),
    ("В Казань из Москвы", "origin", "Казань", False),
    ("В Казань из Москвы", "destination", "Москва", False),
    ("Хочу из Перми поехать в Орёл", "origin", "Орёл", False),
    ("Хочу из Перми поехать в Орёл", "destination", "Пермь", False),
    ("Не из Москвы, из Казани", "origin", "Москва", False),
    ("Не из Москвы, из Казани", "origin", "Казань", True),
    ("Из Москвы в Казань", "origin", "Москва", True),
    ("Из Москвы в Казань", "destination", "Казань", True),
    ("В Казань из Москвы", "origin", "Москва", True),
    ("В Казань из Москвы", "destination", "Казань", True),
    # Роль в цитате не выражена предлогом: прежнее поведение (проверка упоминания) сохраняется.
    ("Казань, Москва", "origin", "Казань", True),
    ("Отдохнуть в Ростове-на-Дону", "destination", "Ростов-на-Дону", True),
    ("Из Москвы", "origin", "Сочи", False),
]


@pytest.mark.parametrize(("text", "field", "value", "accepted"), ROLE_CONFLICTS)
def test_city_role_conflicts_are_rejected(text: str, field: str, value: str, accepted: bool) -> None:
    delta = InterpretationDelta.model_validate({field: {"value": value, "span": text}})
    form, report = apply_delta(delta, text=text, base=StructuredForm(), reference_datetime=REFERENCE)
    assert (field in report.accepted) is accepted, (report.rejected, report.notes)
    assert (getattr(form, field) == value) is accepted


def test_swapped_route_is_not_accepted_as_a_whole() -> None:
    """Аудит: «из Москвы в Казань» с переставленными городами не должен дать обратный маршрут."""
    text = "Хочу из Москвы в Казань"
    delta = InterpretationDelta.model_validate(
        {
            "origin": {"value": "Казань", "span": text},
            "destination": {"value": "Москва", "span": text},
        }
    )
    form, report = apply_delta(delta, text=text, base=StructuredForm(), reference_datetime=REFERENCE)
    assert form.origin is None and form.destination is None
    assert set(report.rejected) >= {"origin", "destination"}
    assert any("другой роли" in note for note in report.notes)


def test_role_rejection_keeps_previous_conditions() -> None:
    """Отклонённая роль не стирает уже подтверждённые города (UC-02)."""
    base = StructuredForm(origin="Москва", destination="Казань")
    text = "Из Москвы в Казань"
    delta = InterpretationDelta.model_validate({"origin": {"value": "Казань", "span": text}})
    form, _ = apply_delta(delta, text=text, base=base, reference_datetime=REFERENCE)
    assert (form.origin, form.destination) == ("Москва", "Казань")
