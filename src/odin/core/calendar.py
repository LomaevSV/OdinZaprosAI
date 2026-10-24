"""Календарь: детерминированное разрешение выражений дат (INTERPRET-02, план §2.7).

LLM (в S2+) только извлекает фразу; разрешает её этот модуль. Результат — `DateExpression` с исходной
фразой, `reference_datetime`, часовым поясом, правилом и статусом `resolved|ambiguous|unresolved`.
Даты не придумываются: при недостатке данных — `unresolved`, при нескольких толкованиях — `ambiguous`
(вопрос пользователю, K-13). Часовой пояс по умолчанию не подставляется (DEC-34 открыт).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from odin.contracts.trip import DateExpression

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2,
    "среда": 2,
    "четверг": 3,
    "пятницу": 4,
    "пятница": 4,
    "субботу": 5,
    "суббота": 5,
    "воскресенье": 6,
}
NUMBER_WORDS = {"одну": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "неделю": 1}

_RANGE = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?")
_DAY_MONTH = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?")
_NUMERIC = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_IN_PERIOD = re.compile(r"через\s+(\d+|" + "|".join(NUMBER_WORDS) + r")?\s*(дн\w*|недел\w*)")


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str
    start: date | None
    end: date | None
    rule: str
    candidates: tuple[tuple[date, date | None], ...] = field(default=())


def _local_today(reference: datetime, timezone_name: str) -> date:
    return reference.astimezone(ZoneInfo(timezone_name)).date()


def _next_occurrence(today: date, month: int, day: int) -> date | None:
    """Ближайшая будущая (или сегодняшняя) дата без указанного года."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _weekend_after(day: date) -> date:
    """Ближайшая суббота не раньше `day`."""
    return day + timedelta(days=(5 - day.weekday()) % 7)


def _resolve(text: str, today: date) -> Resolution:
    phrase = " ".join(text.lower().replace("ё", "е").split())

    if match := _ISO.search(phrase):
        value = _safe_date(int(match[1]), int(match[2]), int(match[3]))
        if value is None:
            return Resolution("unresolved", None, None, "ISO-дата не существует")
        return Resolution("resolved", value, None, "ISO-дата YYYY-MM-DD")

    if match := _RANGE.search(phrase):
        month = MONTHS[match[3]]
        first, last = int(match[1]), int(match[2])
        if match[4]:
            start, end = _safe_date(int(match[4]), month, first), _safe_date(int(match[4]), month, last)
            rule = "диапазон «Д–Д месяца ГГГГ»"
        else:
            start = _next_occurrence(today, month, first)
            end = _safe_date(start.year, month, last) if start else None
            rule = "диапазон «Д–Д месяца» без года: ближайшее будущее вхождение"
        if start is None or end is None or end <= start:
            return Resolution("unresolved", None, None, "диапазон дат некорректен")
        return Resolution("resolved", start, end, rule)

    if match := _DAY_MONTH.search(phrase):
        month, day = MONTHS[match[2]], int(match[1])
        if match[3]:
            value = _safe_date(int(match[3]), month, day)
            rule = "«Д месяца ГГГГ»"
        else:
            value = _next_occurrence(today, month, day)
            rule = "«Д месяца» без года: ближайшее будущее вхождение"
        if value is None:
            return Resolution("unresolved", None, None, "дата не существует")
        return Resolution("resolved", value, None, rule)

    if match := _NUMERIC.search(phrase):
        day, month = int(match[1]), int(match[2])
        if match[3]:
            value = _safe_date(int(match[3]), month, day)
            rule = "«ДД.ММ.ГГГГ»"
        else:
            value = _next_occurrence(today, month, day) if 1 <= month <= 12 else None
            rule = "«ДД.ММ» без года: ближайшее будущее вхождение"
        if value is None:
            return Resolution("unresolved", None, None, "дата не существует")
        return Resolution("resolved", value, None, rule)

    if "послезавтра" in phrase:
        return Resolution("resolved", today + timedelta(days=2), None, "послезавтра = опорная дата + 2 дня")
    if "завтра" in phrase:
        return Resolution("resolved", today + timedelta(days=1), None, "завтра = опорная дата + 1 день")
    if "сегодня" in phrase:
        return Resolution("resolved", today, None, "сегодня = опорная дата")

    if match := _IN_PERIOD.search(phrase):
        raw = match[1] or "1"
        count = int(raw) if raw.isdigit() else NUMBER_WORDS[raw]
        days = count * 7 if match[2].startswith("недел") else count
        return Resolution("resolved", today + timedelta(days=days), None, f"через {days} дн. от опорной даты")

    if "выходн" in phrase:
        saturday = _weekend_after(today)
        if today.weekday() == 6:  # воскресенье: «эти» выходные почти закончились
            saturday = today + timedelta(days=6)
        coming = (saturday, saturday + timedelta(days=1))
        following = (saturday + timedelta(days=7), saturday + timedelta(days=8))
        if "следующ" in phrase:
            if today.weekday() >= 4:  # пт–вс: «следующие» однозначно = выходные следующей недели
                start = coming[0] if today.weekday() == 6 else following[0]
                return Resolution(
                    "resolved",
                    start,
                    start + timedelta(days=1),
                    "«следующие выходные» в пт–вс = выходные следующей недели (сб–вс)",
                )
            return Resolution(
                "ambiguous",
                None,
                None,
                "«следующие выходные» в пн–чт: ближайшие или через неделю — нужен вопрос",
                candidates=(coming, following),
            )
        return Resolution("resolved", coming[0], coming[1], "«на выходные» = ближайшие суббота–воскресенье")

    for word, weekday in WEEKDAYS.items():
        if re.search(rf"\b{word}\b", phrase):
            delta = (weekday - today.weekday()) % 7
            if delta == 0:
                return Resolution(
                    "ambiguous",
                    None,
                    None,
                    "день недели совпадает с опорной датой: сегодня или через неделю",
                    candidates=((today, None), (today + timedelta(days=7), None)),
                )
            return Resolution(
                "resolved", today + timedelta(days=delta), None, "ближайший будущий день недели"
            )

    return Resolution("unresolved", None, None, "выражение даты не распознано: требуется уточнение")


def resolve_date_expression(
    raw_text: str, reference_datetime: datetime, timezone_name: str | None
) -> tuple[DateExpression, Resolution]:
    """Разрешает фразу. Без часового пояса — `unresolved`: местная дата зависит от пояса (DEC-34)."""
    if timezone_name is None:
        resolution = Resolution(
            "unresolved", None, None, "часовой пояс не задан: местная опорная дата неизвестна (DEC-34)"
        )
    else:
        resolution = _resolve(raw_text, _local_today(reference_datetime, timezone_name))
    expression = DateExpression(
        raw_text=raw_text,
        reference_datetime=reference_datetime,
        timezone=timezone_name,
        rule=resolution.rule,
        status=resolution.status,  # type: ignore[arg-type]
        resolved_start=resolution.start,
        resolved_end=resolution.end,
    )
    return expression, resolution
