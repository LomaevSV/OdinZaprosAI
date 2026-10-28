"""DATE-001: стандартные диапазоны дат распознаются и без известного часового пояса.

Браузерное испытание 27.10: «с 1 по 8 марта» → «не удалось определить даты». Календарь фразу разбирает,
но в режиме Mock часовой пояс берётся только из демо-справочника, и для города вне него (или до того,
как город назван) пояс был пустым → отказ. Пояс по умолчанию по-прежнему не подставляется (DEC-34):
фраза принимается, только если её толкование одинаково для любой возможной местной даты.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from odin.contracts.api import StructuredForm
from odin.core.calendar import resolve_trip_dates
from odin.llm.free_text import InterpretationDelta, apply_delta

pytestmark = pytest.mark.req("DATE-001", "INTERPRET-02", "DEC-34")

REF = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

RANGES = [
    ("с 1 по 8 марта", date(2027, 3, 1), date(2027, 3, 8)),
    ("с 10 по 13 ноября", date(2026, 11, 10), date(2026, 11, 13)),
    ("с 28 февраля по 3 марта", date(2027, 2, 28), date(2027, 3, 3)),
    ("с 30 декабря по 5 января", date(2026, 12, 30), date(2027, 1, 5)),
]


@pytest.mark.parametrize(("phrase", "start", "end"), RANGES)
@pytest.mark.parametrize("zone", [None, "Europe/Moscow", "Asia/Novosibirsk"])
def test_ranges_resolve_with_and_without_timezone(
    phrase: str, start: date, end: date, zone: str | None
) -> None:
    result = resolve_trip_dates(phrase, None, REF, zone)
    assert (result.status, result.start, result.end) == ("resolved", start, end), result.rule
    assert result.question is None


def test_cross_year_range_end_follows_start() -> None:
    result = resolve_trip_dates("с 30 декабря по 5 января", None, REF, None)
    assert result.end is not None and result.start is not None
    assert result.end.year == result.start.year + 1


@pytest.mark.parametrize("phrase", ["завтра", "сегодня", "послезавтра", "28 октября"])
def test_timezone_dependent_phrases_stay_unresolved_without_zone(phrase: str) -> None:
    """Если местная дата влияет на толкование, пояс не угадывается — уточнение (DEC-34 без изменений)."""
    result = resolve_trip_dates(phrase, None, REF, None)
    assert result.status == "unresolved"
    assert result.start is None


def _apply(text: str, delta: dict, base: StructuredForm | None = None) -> tuple[StructuredForm, object]:
    return apply_delta(
        InterpretationDelta.model_validate(delta),
        text=text,
        base=base or StructuredForm(),
        reference_datetime=REF,
    )


def test_range_before_any_city_is_named_is_applied() -> None:
    """Первое сообщение без города: раньше пояс был пустым и даты отклонялись."""
    form, report = _apply("с 1 по 8 марта", {"dates": {"date_phrase": "с 1 по 8 марта"}})
    assert (form.date_from, form.date_to) == (date(2027, 3, 1), date(2027, 3, 8))
    assert not any("Не удалось определить даты" in q.question for q in report.questions)


def test_range_with_city_outside_demo_directory_is_applied() -> None:
    text = "Из Перми в Ярославль с 1 по 8 марта"
    form, report = _apply(
        text,
        {
            "origin": {"value": "Пермь", "span": "Из Перми"},
            "destination": {"value": "Ярославль", "span": "в Ярославль"},
            "dates": {"date_phrase": "с 1 по 8 марта"},
        },
    )
    assert (form.date_from, form.date_to) == (date(2027, 3, 1), date(2027, 3, 8))
    assert "dates" in report.accepted


def test_range_with_demo_city_still_uses_city_timezone() -> None:
    form, _ = _apply(
        "Из Москвы в Казань с 10 по 13 ноября",
        {
            "origin": {"value": "Москва", "span": "Из Москвы"},
            "destination": {"value": "Казань", "span": "в Казань"},
            "dates": {"date_phrase": "с 10 по 13 ноября"},
        },
    )
    assert (form.date_from, form.date_to) == (date(2026, 11, 10), date(2026, 11, 13))
