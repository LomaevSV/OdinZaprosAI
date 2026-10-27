"""Календарь: детерминированное разрешение выражений дат (INTERPRET-02, план §2.7).

LLM (в S2+) только извлекает фразу; разрешает её этот модуль. Результат — `DateExpression` с исходной
фразой, `reference_datetime`, часовым поясом, правилом и статусом `resolved|ambiguous|unresolved`.
Даты не придумываются: при недостатке данных — `unresolved`, при нескольких толкованиях — `ambiguous`
(вопрос пользователю, K-13). Часовой пояс по умолчанию не подставляется (DEC-34 открыт).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
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
# Количественные слова для длительности («на три ночи», «на двое суток»).
COUNT_WORDS = {
    "одну": 1,
    "одна": 1,
    "один": 1,
    "сутки": 1,
    "две": 2,
    "два": 2,
    "двое": 2,
    "три": 3,
    "трое": 3,
    "четыре": 4,
    "четверо": 4,
    "пять": 5,
    "пятеро": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}

_MONTHS_RE = "|".join(MONTHS)
_RANGE = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+(" + _MONTHS_RE + r")(?:\s+(\d{4}))?")
_FROM_TO = re.compile(r"(?:^|\s)с\s+(\d{1,2})\s+по\s+(\d{1,2})\s+(" + _MONTHS_RE + r")(?:\s+(\d{4}))?")
_DAY_MONTH = re.compile(r"(\d{1,2})\s+(" + _MONTHS_RE + r")(?:\s+(\d{4}))?")
_NUMERIC = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_IN_PERIOD = re.compile(r"через\s+(\d+|" + "|".join(NUMBER_WORDS) + r")?\s*(дн\w*|недел\w*)")
_COUNT_RE = r"(\d{1,2}|" + "|".join(COUNT_WORDS) + r")"
_NIGHTS = re.compile(_COUNT_RE + r"\s+(ноч\w*|сут\w*)")
_SUTKI = re.compile(r"\bна\s+сутки\b")
_WEEKS = re.compile(r"(?:(\d{1,2}|" + "|".join(COUNT_WORDS) + r")\s+)?недел[юиь]\w*")
_DAYS = re.compile(_COUNT_RE + r"\s+(дн\w*|дня)")


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


def _mentions(phrase: str) -> list[tuple[int, int, int, int | None]]:
    """Абсолютные упоминания дат по порядку: (позиция, день, месяц, год|None), без перекрытий."""
    found: list[tuple[int, int, int, int, int | None]] = []
    for match in _ISO.finditer(phrase):
        found.append((match.start(), match.end(), int(match[3]), int(match[2]), int(match[1])))
    for match in _DAY_MONTH.finditer(phrase):
        year = int(match[3]) if match[3] else None
        found.append((match.start(), match.end(), int(match[1]), MONTHS[match[2]], year))
    for match in _NUMERIC.finditer(phrase):
        year = int(match[3]) if match[3] else None
        found.append((match.start(), match.end(), int(match[1]), int(match[2]), year))
    found.sort()
    result: list[tuple[int, int, int, int | None]] = []
    last_end = -1
    for start, end, day, month, year in found:
        if start < last_end:
            continue
        result.append((start, day, month, year))
        last_end = end
    return result


def _mention_date(day: int, month: int, year: int | None, not_before: date) -> date | None:
    if not 1 <= month <= 12:
        return None
    if year is not None:
        return _safe_date(year, month, day)
    return _next_occurrence(not_before, month, day)


def _two_dates(phrase: str, today: date) -> Resolution | None:
    """«с 10 ноября по 12 ноября», «30 декабря – 5 января», «10.11–12.11»: начало и конец."""
    mentions = _mentions(phrase)
    if len(mentions) < 2:
        return None
    if len(mentions) > 2:
        return Resolution("unresolved", None, None, "больше двух дат во фразе: требуется уточнение")
    (_, d1, m1, y1), (_, d2, m2, y2) = mentions
    first = _mention_date(d1, m1, y1, today)
    if first is None:
        return Resolution("unresolved", None, None, "первая дата не существует")
    second = _mention_date(d2, m2, y2, first + timedelta(days=1))
    if second is None or second <= first:
        return Resolution("unresolved", None, None, "вторая дата не позже первой или не существует")
    rule = "две даты: начало и конец" + ("" if y1 and y2 else "; без года — ближайшее будущее вхождение")
    return Resolution("resolved", first, second, rule)


def _resolve(text: str, today: date) -> Resolution:
    phrase = " ".join(text.lower().replace("ё", "е").split())

    if (pair := _two_dates(phrase, today)) is not None:
        return pair

    if match := _FROM_TO.search(phrase):
        month = MONTHS[match[3]]
        first, last = int(match[1]), int(match[2])
        if match[4]:
            start, end = _safe_date(int(match[4]), month, first), _safe_date(int(match[4]), month, last)
        else:
            start = _next_occurrence(today, month, first)
            end = _safe_date(start.year, month, last) if start else None
        if start is None or end is None or end <= start:
            return Resolution("unresolved", None, None, "диапазон дат некорректен")
        return Resolution("resolved", start, end, "диапазон «с Д по Д месяца»")

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


def _resolve_without_timezone(raw_text: str, reference_datetime: datetime) -> Resolution:
    """DATE-001: при неизвестном поясе местная опорная дата лежит в пределах ±1 дня от даты UTC
    (пояса UTC−12…UTC+14). Если толкование одинаково для всех трёх дат («с 1 по 8 марта»), оно от пояса
    не зависит и принимается; относительные фразы («завтра», «в пятницу») остаются `unresolved`."""
    utc_today = reference_datetime.astimezone(UTC).date()
    options = [_resolve(raw_text, utc_today + timedelta(days=shift)) for shift in (-1, 0, 1)]
    first = options[0]
    same = all(
        (item.status, item.start, item.end, item.candidates)
        == (first.status, first.start, first.end, first.candidates)
        for item in options
    )
    if same and first.status in ("resolved", "ambiguous"):
        return replace(first, rule=f"{first.rule}; не зависит от часового пояса (DEC-34)")
    return Resolution(
        "unresolved", None, None, "часовой пояс не задан: местная опорная дата неизвестна (DEC-34)"
    )


def resolve_date_expression(
    raw_text: str, reference_datetime: datetime, timezone_name: str | None
) -> tuple[DateExpression, Resolution]:
    """Разрешает фразу. Без часового пояса пояс не подставляется (DEC-34): фраза принимается, только если
    её толкование одинаково для любой возможной местной даты, иначе — `unresolved`."""
    if timezone_name is None:
        resolution = _resolve_without_timezone(raw_text, reference_datetime)
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


def _count(raw: str | None, default: int = 1) -> int:
    if not raw:
        return default
    return int(raw) if raw.isdigit() else COUNT_WORDS.get(raw, default)


def nights_value(raw_text: str) -> tuple[int | None, str, str]:
    """Длительность в ночах: (число | None, статус, правило).

    «N дней» неоднозначно (N или N−1 ночей) — вопрос, а не догадка.
    """
    phrase = " ".join(raw_text.lower().replace("ё", "е").split())
    if match := _NIGHTS.search(phrase):
        nights = _count(match[1])
        if 1 <= nights <= 30:
            return nights, "resolved", "«N ночей/суток» = N ночей"
        return None, "unresolved", "число ночей вне диапазона 1–30"
    if _SUTKI.search(phrase):
        return 1, "resolved", "«на сутки» = 1 ночь"
    if match := _WEEKS.search(phrase):
        weeks = _count(match[1])
        if 1 <= weeks <= 4:
            return weeks * 7, "resolved", "«N недель» = 7×N ночей"
        return None, "unresolved", "длительность вне диапазона"
    if _DAYS.search(phrase):
        return None, "ambiguous", "«N дней» допускает N или N−1 ночей: нужен вопрос"
    return None, "unresolved", "длительность не распознана"


@dataclass(frozen=True, slots=True)
class TripDates:
    """Итог разрешения дат поездки из фраз пользователя (INTERPRET-02)."""

    status: str  # resolved | partial | ambiguous | unresolved
    start: date | None
    end: date | None
    expressions: tuple[DateExpression, ...]
    question: str | None
    rule: str


def resolve_trip_dates(
    date_phrase: str | None,
    nights_phrase: str | None,
    reference_datetime: datetime,
    timezone_name: str | None,
) -> TripDates:
    """Даты отъезда и возвращения: календарь + длительность. LLM даёт только фразы, даты считает код."""
    if not date_phrase:
        return TripDates("unresolved", None, None, (), None, "даты не названы")
    expression, resolution = resolve_date_expression(date_phrase, reference_datetime, timezone_name)
    expressions = (expression,)
    if resolution.status == "ambiguous":
        options = "; ".join(
            start.isoformat() + (f" – {end.isoformat()}" if end else "")
            for start, end in resolution.candidates
        )
        question = f"Уточните даты: «{date_phrase}» можно понять по-разному ({options})."
        return TripDates("ambiguous", None, None, expressions, question, resolution.rule)
    if resolution.status != "resolved" or resolution.start is None:
        question = (
            f"Не удалось определить даты по фразе «{date_phrase}». Назовите даты отъезда и возвращения."
        )
        return TripDates("unresolved", None, None, expressions, question, resolution.rule)
    if resolution.end is not None:
        return TripDates("resolved", resolution.start, resolution.end, expressions, None, resolution.rule)
    if not nights_phrase:
        return TripDates("partial", resolution.start, None, expressions, None, resolution.rule)
    nights, status, rule = nights_value(nights_phrase)
    if nights is None:
        question = (
            f"Сколько ночей длится поездка? Фразу «{nights_phrase}» можно понять по-разному."
            if status == "ambiguous"
            else f"Не удалось определить длительность по фразе «{nights_phrase}». Сколько ночей?"
        )
        return TripDates(status, resolution.start, None, expressions, question, rule)
    end = resolution.start + timedelta(days=nights)
    return TripDates(
        "resolved", resolution.start, end, expressions, None, f"{resolution.rule}; {rule}: {nights}"
    )
