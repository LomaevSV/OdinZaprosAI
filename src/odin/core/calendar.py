"""Календарь: детерминированное разрешение выражений дат (INTERPRET-02, план §2.7).

LLM (в S2+) только извлекает фразу; разрешает её этот модуль. Результат — `DateExpression` с исходной
фразой, `reference_datetime`, часовым поясом, правилом и статусом `resolved|ambiguous|unresolved`.
Даты не придумываются: при недостатке данных — `unresolved`, при нескольких толкованиях — `ambiguous`
(вопрос пользователю, K-13). DEC-34: опорная местная дата — по часовому поясу пункта отправления; пояс
по умолчанию (в т. ч. московский) не подставляется.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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
# REPLAN-001: число без единицы («останемся на четыре») — ночи только при однозначном контексте.
# Собирательные («двое», «четверо») здесь не учитываются: обычно это число людей.
_CARDINALS = r"\d{1,2}|одну|одна|один|две|два|три|четыре|пять|шесть|семь|восемь|девять|десять"
_BARE_COUNT = re.compile(r"(?:^|\s)(?:на\s+)?(" + _CARDINALS + r")(?=\s*$|\s*[,.!?;])")

# DATE-003: период — ограничение на возможные даты, а не сами даты.
_MONTH_FORMS = {
    1: r"январ[ьяе]",
    2: r"феврал[ьяе]",
    3: r"март[ае]?",
    4: r"апрел[ьяе]",
    5: r"ма[йяе]",
    6: r"июн[ьяе]",
    7: r"июл[ьяе]",
    8: r"август[ае]?",
    9: r"сентябр[ьяе]",
    10: r"октябр[ьяе]",
    11: r"ноябр[ьяе]",
    12: r"декабр[ьяе]",
}
_MONTH_ANY = "|".join(f"(?P<m{number}>{form})" for number, form in _MONTH_FORMS.items())
_W0, _W1 = r"(?<![а-яa-z0-9])", r"(?![а-яa-z0-9])"
_YEAR = r"(?:\s+(?P<year>\d{4})(?:\s*(?:г\.?|года?))?)?"
_PERIOD_PART = re.compile(
    _W0
    + r"(?:в|во)\s+(?P<part>начале|середине|конце|первой\s+половине|второй\s+половине)\s+(?:"
    + _MONTH_ANY
    + r")"
    + _W1
    + _YEAR
)
_PERIOD_WEEKEND = re.compile(
    _W0 + r"(?P<which>первые|последние)\s+выходные\s+(?:" + _MONTH_ANY + r")" + _W1 + _YEAR
)
_PERIOD_MONTH = re.compile(_W0 + r"(?:(?:в|во|на)\s+)?(?:" + _MONTH_ANY + r")" + _W1 + _YEAR)
_SEASONS = {"весн": (3, 5), "лет": (6, 8), "осен": (9, 11), "зим": (12, 2)}
_PERIOD_SEASON = re.compile(
    _W0
    + r"(?:(?P<next>следующ\w+)\s+|(?:этой|этим|этого)\s+)?(?P<season>весной|летом|осенью|зимой)"
    + _W1
    + _YEAR
)
_PART_DAYS = {
    "начале": (1, 10),
    "середине": (11, 20),
    "конце": (21, 31),
    "первой половине": (1, 15),
    "второй половине": (16, 31),
}
MONTHS_GENITIVE = {number: name for name, number in MONTHS.items()}
WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# Предлагаемые окна поездки внутри периода — не больше этого числа (предложения, не бронь).
MAX_WINDOWS = 4


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str
    start: date | None
    end: date | None
    rule: str
    candidates: tuple[tuple[date, date | None], ...] = field(default=())
    # DATE-003: для status="period" — "range" (месяц, часть месяца, сезон) или "weekend".
    period_kind: str | None = None
    # Самая ранняя допустимая дата отъезда для периода (местное «завтра» в пункте отправления).
    not_before: date | None = None


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


def _last_day(year: int, month: int) -> int:
    following = date(year + month // 12, month % 12 + 1, 1)
    return (following - timedelta(days=1)).day


def _month_of(match: re.Match[str]) -> int:
    return next(int(key[1:]) for key, value in match.groupdict().items() if key[1:].isdigit() and value)


def _weekend_in(year: int, month: int, *, last: bool) -> date:
    """Суббота первых/последних выходных месяца (суббота — в этом месяце)."""
    if last:
        day = date(year, month, _last_day(year, month))
        return day - timedelta(days=(day.weekday() - 5) % 7)
    return _weekend_after(date(year, month, 1))


def _period(phrase: str, today: date) -> Resolution | None:
    """DATE-003: «в ноябре», «в конце ноября», «в середине января», «последние выходные марта», «летом» —
    ограничение на даты. Даты отъезда здесь не выбираются: их выбирает пользователь из предложенных окон.

    Без года — ближайший период, в котором ещё можно уехать (не раньше местного «завтра»)."""
    earliest = today + timedelta(days=1)
    build: Callable[[int], tuple[date, date]]
    kind, base_year, roll = "range", today.year, False
    if match := _PERIOD_WEEKEND.search(phrase):
        month, last = _month_of(match), match["which"] == "последние"

        def build(year: int) -> tuple[date, date]:
            saturday = _weekend_in(year, month, last=last)
            return saturday, saturday + timedelta(days=1)

        kind = "weekend"
    elif match := _PERIOD_PART.search(phrase):
        month = _month_of(match)
        part = " ".join(match["part"].split())
        first, last_day = _PART_DAYS[part]

        def build(year: int) -> tuple[date, date]:
            return date(year, month, first), date(year, month, min(last_day, _last_day(year, month)))

    elif match := _PERIOD_SEASON.search(phrase):
        stem = next(key for key in _SEASONS if match["season"].startswith(key))
        first_month, last_month = _SEASONS[stem]

        def build(year: int) -> tuple[date, date]:
            end_year = year + (1 if last_month < first_month else 0)
            return date(year, first_month, 1), date(end_year, last_month, _last_day(end_year, last_month))

        if first_month > last_month and today.month <= last_month:
            base_year -= 1  # январь–февраль: идущая зима началась в декабре прошлого года
        roll = bool(match["next"])
    elif match := _PERIOD_MONTH.search(phrase):
        month = _month_of(match)

        def build(year: int) -> tuple[date, date]:
            return date(year, month, 1), date(year, month, _last_day(year, month))

    else:
        return None
    label = match.group(0).strip()

    def usable(start: date, end: date) -> bool:
        return start >= earliest if kind == "weekend" else end > earliest

    if match["year"]:
        start, end = build(int(match["year"]))
        if not usable(start, end):
            return Resolution("unresolved", None, None, f"период «{label}» уже прошёл")
        year_rule = "год указан"
    else:
        start, end = build(base_year)
        if roll and start <= today:
            start, end = build(base_year + 1)  # «следующим летом» во время лета — через год
        elif not usable(start, end):
            start, end = build(base_year + 1)
        year_rule = "без года — ближайший период, когда ещё можно уехать"
    effective = start if kind == "weekend" else max(start, earliest)
    rule = (
        f"период «{label}» {effective.isoformat()}–{end.isoformat()} ({year_rule}): ограничение на даты, "
        "конкретные даты выбирает пользователь (DATE-003)"
    )
    return Resolution("period", effective, end, rule, period_kind=kind, not_before=earliest)


def nights_word(nights: int) -> str:
    if nights % 10 == 1 and nights % 100 != 11:
        return "ночь"
    if 2 <= nights % 10 <= 4 and not 12 <= nights % 100 <= 14:
        return "ночи"
    return "ночей"


def nights_phrase(nights: int) -> str:
    """Фраза длительности, которую понимает ``nights_value`` («на 3 ночи»)."""
    return f"на {nights} {nights_word(nights)}"


def format_day(day: date) -> str:
    return f"{day.day} {MONTHS_GENITIVE[day.month]} {day.year} ({WEEKDAY_SHORT[day.weekday()]})"


def format_window(start: date, end: date) -> str:
    """«6–9 ноября 2026 (пт–пн, 3 ночи)»: предложение дат, а не бронь."""
    nights = (end - start).days
    if (start.year, start.month) == (end.year, end.month):
        span = f"{start.day}–{end.day} {MONTHS_GENITIVE[end.month]} {end.year}"
    elif start.year == end.year:
        span = (
            f"{start.day} {MONTHS_GENITIVE[start.month]} – {end.day} {MONTHS_GENITIVE[end.month]} {end.year}"
        )
    else:
        span = (
            f"{start.day} {MONTHS_GENITIVE[start.month]} {start.year} – "
            f"{end.day} {MONTHS_GENITIVE[end.month]} {end.year}"
        )
    days = f"{WEEKDAY_SHORT[start.weekday()]}–{WEEKDAY_SHORT[end.weekday()]}"
    return f"{span} ({days}, {nights} {nights_word(nights)})"


def _spread(items: list[date], limit: int) -> list[date]:
    if len(items) <= limit:
        return items
    return [items[round(index * (len(items) - 1) / (limit - 1))] for index in range(limit)]


_WEEKDAY_GENITIVE = ("понедельника", "вторника", "среды", "четверга", "пятницы", "субботы", "воскресенья")
_WEEKDAY_DATIVE = (
    "понедельникам",
    "вторникам",
    "средам",
    "четвергам",
    "пятницам",
    "субботам",
    "воскресеньям",
)
_WEEKDAY_ACCUSATIVE = ("понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье")
_PREF_FROM = re.compile(_W0 + r"(?:с|со)\s+(" + "|".join(_WEEKDAY_GENITIVE) + r")" + _W1)
_PREF_EVERY = re.compile(_W0 + r"по\s+(" + "|".join(_WEEKDAY_DATIVE) + r")" + _W1)
_PREF_ON = re.compile(_W0 + r"(?:в|во)\s+(" + "|".join(_WEEKDAY_ACCUSATIVE) + r")" + _W1)


def weekday_preference(text: str, *, accusative: bool = False) -> int | None:
    """День отъезда, которому пользователь отдаёт предпочтение: «с пятницы», «по пятницам» (и «в пятницу»
    внутри фразы периода). Несколько разных дней — предпочтения нет."""
    phrase = " ".join(text.lower().replace("ё", "е").split())
    found = {_WEEKDAY_GENITIVE.index(m[1]) for m in _PREF_FROM.finditer(phrase)}
    found |= {_WEEKDAY_DATIVE.index(m[1]) for m in _PREF_EVERY.finditer(phrase)}
    if accusative:
        found |= {_WEEKDAY_ACCUSATIVE.index(m[1]) for m in _PREF_ON.finditer(phrase)}
    return found.pop() if len(found) == 1 else None


def weekday_preference_phrase(weekday: int) -> str:
    return f"с {_WEEKDAY_GENITIVE[weekday]}"


def window_option_id(start: date, end: date) -> str:
    """Канонический идентификатор варианта дат: по нему сервер сверяет выбор пользователя."""
    return f"dates-{start:%Y%m%d}-{end:%Y%m%d}"


def trip_windows(
    resolution: Resolution, nights: int | None, start_weekday: int | None = None
) -> tuple[tuple[date, date], ...]:
    """Окна поездки внутри периода: детерминированно, не больше ``MAX_WINDOWS``.

    Выходные: без длительности — сб–вс и пт–вс (выбор пользователя, а не допущение); с длительностью N —
    окна, включающие ночь на воскресенье. Прочие периоды: длительность обязательна; отъезд в предпочтённый
    день недели («с пятницы»), иначе по пятницам, если их в периоде хотя бы две, иначе равномерно по периоду;
    возвращение — не позже конца периода."""
    if resolution.status != "period" or resolution.start is None or resolution.end is None:
        return ()
    start, end = resolution.start, resolution.end
    not_before = resolution.not_before or start
    if resolution.period_kind == "weekend":
        saturday, sunday = start, end
        if nights is None:
            options = [(saturday, sunday), (saturday - timedelta(days=1), sunday)]
        else:
            first = sunday - timedelta(days=nights)
            options = [(first, sunday), (first + timedelta(days=1), sunday + timedelta(days=1))]
            options = [(s, e) for s, e in options if s <= saturday]
        return tuple((s, e) for s, e in options if s >= not_before)
    if nights is None:
        return ()
    last_start = end - timedelta(days=nights)
    starts = [start + timedelta(days=offset) for offset in range((last_start - start).days + 1)]
    if start_weekday is not None:
        pool = [day for day in starts if day.weekday() == start_weekday]
    else:
        fridays = [day for day in starts if day.weekday() == 4]
        pool = fridays if len(fridays) >= 2 else starts
    return tuple((day, day + timedelta(days=nights)) for day in _spread(pool, MAX_WINDOWS))


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

    if (period := _period(phrase, today)) is not None:
        return period

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


TIMEZONE_UNKNOWN_RULE = "часовой пояс пункта отправления неизвестен: местная опорная дата неизвестна (DEC-34)"


def _resolve_without_timezone(raw_text: str, reference_datetime: datetime) -> Resolution:
    """DATE-001: при неизвестном поясе местная опорная дата лежит в пределах ±1 дня от даты UTC
    (пояса UTC−12…UTC+14). Если толкование одинаково для всех трёх дат («с 1 по 8 марта»), оно от пояса
    не зависит и принимается; относительные фразы («завтра», «в пятницу») остаются `unresolved`."""
    utc_today = reference_datetime.astimezone(UTC).date()
    options = [_resolve(raw_text, utc_today + timedelta(days=shift)) for shift in (-1, 0, 1)]
    first = options[0]
    same = all(
        (item.status, item.start, item.end, item.candidates, item.period_kind)
        == (first.status, first.start, first.end, first.candidates, first.period_kind)
        for item in options
    )
    if same and first.status in ("resolved", "ambiguous", "period"):
        # Период: самое позднее из возможных местных «завтра» — окно не начнётся раньше допустимого.
        not_before = max((item.not_before for item in options if item.not_before), default=None)
        return replace(
            first, rule=f"{first.rule}; не зависит от часового пояса (DEC-34)", not_before=not_before
        )
    if same:
        return first  # фраза не распознана при любой местной дате: дело не в часовом поясе
    return Resolution("unresolved", None, None, TIMEZONE_UNKNOWN_RULE)


def resolve_date_expression(
    raw_text: str, reference_datetime: datetime, timezone_name: str | None
) -> tuple[DateExpression, Resolution]:
    """Разрешает фразу в поясе пункта отправления. Без пояса другой пояс не подставляется (DEC-34):
    фраза принимается, только если её толкование одинаково для любой возможной местной даты,
    иначе — `unresolved`."""
    if timezone_name is None:
        resolution = _resolve_without_timezone(raw_text, reference_datetime)
    else:
        resolution = _resolve(raw_text, _local_today(reference_datetime, timezone_name))
    expression = DateExpression(
        raw_text=raw_text,
        reference_datetime=reference_datetime,
        timezone=timezone_name,
        rule=resolution.rule,
        # Период — несколько допустимых толкований до выбора пользователя (DATE-003).
        status="ambiguous" if resolution.status == "period" else resolution.status,  # type: ignore[arg-type]
        # Границы периода — в правиле; как даты поездки они не выдаются (DATE-003).
        resolved_start=None if resolution.status == "period" else resolution.start,
        resolved_end=None if resolution.status == "period" else resolution.end,
    )
    return expression, resolution


def _count(raw: str | None, default: int = 1) -> int:
    if not raw:
        return default
    return int(raw) if raw.isdigit() else COUNT_WORDS.get(raw, default)


def nights_value(raw_text: str, *, bare_count: bool = False) -> tuple[int | None, str, str]:
    """Длительность в ночах: (число | None, статус, правило).

    «N дней» неоднозначно (N или N−1 ночей) — вопрос, а не догадка. Число без единицы («на четыре»)
    считается ночами только при ``bare_count`` — когда вызывающий установил однозначный контекст длительности
    (REPLAN-001); иначе это не длительность.
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
    if bare_count and (match := _BARE_COUNT.search(phrase)):
        nights = _count(match[1])
        if 1 <= nights <= 30:
            return nights, "resolved", "число без единицы в контексте длительности = ночи (REPLAN-001)"
    return None, "unresolved", "длительность не распознана"


_RELATIVE_WORD = re.compile(_W0 + r"(послезавтра|завтра|сегодня)" + _W1)


def literal_date_phrase(text: str) -> str | None:
    """DATE-002: дословная фраза даты из сообщения, если она единственная и однозначна по форме:
    «завтра»/«послезавтра»/«сегодня» или период («в ноябре», «летом»). Нужна, когда модель вернула
    вычисленную дату («29 октября») вместо цитаты: даты считает календарь, а не модель."""
    phrase = " ".join(text.lower().replace("ё", "е").split())
    relative = {match[1] for match in _RELATIVE_WORD.finditer(phrase)}
    if len(relative) == 1:
        return relative.pop()
    if relative or _DAY_MONTH.search(phrase) or _NUMERIC.search(phrase) or _ISO.search(phrase):
        return None
    for pattern in (_PERIOD_WEEKEND, _PERIOD_PART, _PERIOD_SEASON, _PERIOD_MONTH):
        found = [match.group(0).strip() for match in pattern.finditer(phrase)]
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def inline_nights(raw_text: str) -> int | None:
    """Длительность, названная внутри фразы даты с явной единицей («завтра на 3 ночи»)."""
    phrase = " ".join(raw_text.lower().replace("ё", "е").split())
    if match := _NIGHTS.search(phrase):
        nights = _count(match[1])
        return nights if 1 <= nights <= 30 else None
    return 1 if _SUTKI.search(phrase) else None


@dataclass(frozen=True, slots=True)
class TripDates:
    """Итог разрешения дат поездки из фраз пользователя (INTERPRET-02)."""

    status: str  # resolved | partial | ambiguous | period | unresolved
    start: date | None
    end: date | None
    expressions: tuple[DateExpression, ...]
    question: str | None
    rule: str
    # DEC-34: толкование зависит от местной даты, а пояс пункта отправления неизвестен.
    needs_timezone: bool = False
    # DATE-003: предлагаемые окна поездки (start, end) — предложения, а не согласованные даты.
    windows: tuple[tuple[date, date], ...] = ()
    # Длительность, по которой построены окна (None — окна с разной длительностью или окон нет).
    nights: int | None = None
    # Предпочтённый день отъезда, учтённый в окнах (0 — понедельник).
    start_weekday: int | None = None


def _windows_text(windows: tuple[tuple[date, date], ...]) -> str:
    return "; ".join(f"{index}) {format_window(start, end)}" for index, (start, end) in enumerate(windows, 1))


_OFFER_NOTE = "Это только предложения дат: цены и наличие проверяются после выбора."


def _period_dates(
    date_phrase: str,
    resolution: Resolution,
    nights: int | None,
    expressions: tuple[DateExpression, ...],
    start_weekday: int | None = None,
) -> TripDates:
    assert resolution.start is not None and resolution.end is not None
    if resolution.period_kind == "weekend":
        start_weekday = None  # у выходных день отъезда задан самим периодом
    windows = trip_windows(resolution, nights, start_weekday)
    period = f"{format_day(resolution.start)} – {format_day(resolution.end)}"
    departure = f" с отъездом в {_WEEKDAY_ACCUSATIVE[start_weekday]}" if start_weekday is not None else ""
    if windows:
        same = {(end - start).days for start, end in windows}
        duration = f" на {nights} {nights_word(nights)}" if nights else ""
        question = (
            f"«{date_phrase}» — это период ({period}), а не конкретные даты. Варианты поездки{duration}"
            f"{departure}: {_windows_text(windows)}. {_OFFER_NOTE} Выберите вариант или назовите свои даты."
        )
        return TripDates(
            "period",
            None,
            None,
            expressions,
            question,
            resolution.rule,
            windows=windows,
            nights=nights if len(same) == 1 and nights else None,
            start_weekday=start_weekday,
        )
    if nights is None:
        question = (
            f"«{date_phrase}» — это период ({period}), а не конкретные даты. "
            "Сколько ночей длится поездка? Тогда предложу варианты дат."
        )
    else:
        question = (
            f"В период «{date_phrase}» ({period}) не помещается поездка на {nights} {nights_word(nights)}"
            f"{departure}. Назовите другие даты, длительность или год."
        )
    return TripDates(
        "period",
        None,
        None,
        expressions,
        question,
        resolution.rule,
        nights=nights,
        start_weekday=start_weekday,
    )


def resolve_trip_dates(
    date_phrase: str | None,
    nights_phrase: str | None,
    reference_datetime: datetime,
    timezone_name: str | None,
    *,
    known_nights: int | None = None,
    bare_count: bool = False,
    start_weekday: int | None = None,
) -> TripDates:
    """Даты отъезда и возвращения: календарь + длительность. LLM даёт только фразы, даты считает код.

    ``known_nights`` — длительность уже согласованных условий поездки (пересборка): она сохраняется, если
    в сообщении названа только дата отъезда или период; новая длительность из фразы важнее."""
    if not date_phrase:
        return TripDates("unresolved", None, None, (), None, "даты не названы")
    expression, resolution = resolve_date_expression(date_phrase, reference_datetime, timezone_name)
    expressions = (expression,)
    if not nights_phrase and inline_nights(date_phrase):
        nights_phrase = date_phrase  # «завтра на 3 ночи» одной фразой
    nights: int | None = None
    nights_status, nights_rule = "unresolved", "длительность не названа"
    if nights_phrase:
        nights, nights_status, nights_rule = nights_value(nights_phrase, bare_count=bare_count)
    if nights_phrase and nights is None:
        nights_question = (
            f"Сколько ночей длится поездка? Фразу «{nights_phrase}» можно понять по-разному."
            if nights_status == "ambiguous"
            else f"Не удалось определить длительность по фразе «{nights_phrase}». Сколько ночей?"
        )
    else:
        nights_question = None
    duration = nights or (None if nights_phrase else known_nights)
    if resolution.status == "period":
        if nights_question:
            return TripDates("period", None, None, expressions, nights_question, nights_rule)
        if start_weekday is None:
            start_weekday = weekday_preference(date_phrase, accusative=True)
        return _period_dates(date_phrase, resolution, duration, expressions, start_weekday)
    if resolution.status == "ambiguous":
        windows = tuple(
            (start, end if end is not None else start + timedelta(days=duration))
            for start, end in resolution.candidates
            if end is not None or duration
        )
        options = "; ".join(
            start.isoformat() + (f" – {end.isoformat()}" if end else "")
            for start, end in resolution.candidates
        )
        question = f"Уточните даты: «{date_phrase}» можно понять по-разному ({options})."
        return TripDates("ambiguous", None, None, expressions, question, resolution.rule, windows=windows)
    if resolution.status != "resolved" or resolution.start is None:
        if resolution.rule == TIMEZONE_UNKNOWN_RULE:
            question = (
                f"Уточните даты: «{date_phrase}» зависит от местной даты в пункте отправления, "
                "а его часовой пояс неизвестен. Назовите город отправления или даты числами."
            )
            return TripDates(
                "unresolved", None, None, expressions, question, resolution.rule, needs_timezone=True
            )
        if resolution.rule.endswith("уже прошёл"):
            question = f"Период «{date_phrase}» уже прошёл. Назовите будущие даты поездки."
        else:
            question = (
                f"Не удалось определить даты по фразе «{date_phrase}». Назовите даты отъезда и возвращения."
            )
        return TripDates("unresolved", None, None, expressions, question, resolution.rule)
    if resolution.end is not None:
        return TripDates("resolved", resolution.start, resolution.end, expressions, None, resolution.rule)
    if nights_question:
        return TripDates(nights_status, resolution.start, None, expressions, nights_question, nights_rule)
    if duration is None:
        return TripDates("partial", resolution.start, None, expressions, None, resolution.rule)
    end = resolution.start + timedelta(days=duration)
    rule = nights_rule if nights else "длительность текущих условий сохранена"
    return TripDates(
        "resolved", resolution.start, end, expressions, None, f"{resolution.rule}; {rule}: {duration}"
    )
