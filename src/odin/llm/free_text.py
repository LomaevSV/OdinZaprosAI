"""Свободный текст → черновик запроса (план §2.4 шаг 2, INTERPRET-01/02, ARCH-04).

LLM-Интерпретатор возвращает ``InterpretationDelta``: только извлечённые значения и **дословные**
фрагменты сообщения (``span``). Код:

1. проверяет, что каждый фрагмент буквально встречается в тексте пользователя — иначе значение
   отклоняется (становится уточнением или неразобранным фрагментом), а не принимается на веру;
2. разрешает даты детерминированным календарём (LLM даёт только фразы, даты не вычисляет);
3. накладывает изменения на прежние условия поездки (UC-02: полученное не теряется);
4. строит ``TripRequest`` тем же кодом, что и структурированная форма (одна схема, DEC-30).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from odin.contracts import TripRequest
from odin.contracts.api import StructuredForm
from odin.contracts.trip import (
    Ambiguity,
    AmbiguityOption,
    Constraint,
    ConstraintGroup,
    DateExpression,
    PetInfo,
)
from odin.core import calendar
from odin.llm.interpreter import InterpretationOutcome, interpret_structured_form
from odin.tools.mock_data import DEMO_CATALOG, DEMO_PLACES, normalize_city

INTERPRETER_SCHEMA_VERSION = "1.0.0"

# Закрытый словарь пожеланий (план §2.5, Itinerary): теги мини-каталога.
WISH_TAGS: tuple[str, ...] = tuple(sorted({tag for entry in DEMO_CATALOG for tag in entry.tags}))

ClearableField = Literal[
    "budget",
    "transport",
    "pets",
    "breakfast",
    "stars_min",
    "free_cancellation",
    "wishes_tags",
    "children_ages",
    "return_trip",
]


class _Delta(BaseModel):
    """Лишние ключи модели игнорируются; типы и диапазоны проверяются строго."""

    model_config = ConfigDict(extra="ignore")


class Spanned(_Delta):
    span: str = Field(min_length=1, max_length=300)


class TextValue(Spanned):
    value: str = Field(min_length=1, max_length=120)


class AdultsValue(Spanned):
    value: int = Field(ge=1, le=9)


class ChildrenCount(Spanned):
    """Дети названы без возраста («с ребёнком», «с двумя детьми»): возраст не выдумывается — вопрос."""

    value: int = Field(ge=1, le=8)


class AgesValue(Spanned):
    value: list[int] = Field(max_length=8)

    @field_validator("value")
    @classmethod
    def _ages(cls, value: list[int]) -> list[int]:
        if any(age < 0 or age > 17 for age in value):
            raise ValueError("возраст ребёнка 0–17")
        return value


class BoolValue(Spanned):
    value: bool


class RequiredBool(BoolValue):
    required: bool = False


class StarsValue(Spanned):
    value: int = Field(ge=1, le=5)
    required: bool = False


class TransportValue(Spanned):
    include: list[Literal["rail", "flight"]] = Field(default_factory=list)
    exclude: list[Literal["rail", "flight"]] = Field(default_factory=list)
    required: bool = False


class BudgetValue(Spanned):
    amount: str | int
    currency: Literal["RUB", "EUR", "USD"]
    required: bool = False

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: str | int) -> str:
        text = str(value).replace(" ", "").replace("\u00a0", "").replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("сумма бюджета не число") from exc
        # NaN/Infinity от модели — ошибка проверки, а не исключение Decimal в ходе диалога (INT-006).
        if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000"):
            raise ValueError("сумма бюджета вне диапазона")
        return format(amount.quantize(Decimal("0.01")), "f")


class PetsValue(Spanned):
    value: list[PetInfo] = Field(min_length=1, max_length=3)
    required: bool = True


class TagsValue(Spanned):
    value: list[str] = Field(max_length=6)


class DatesValue(_Delta):
    date_phrase: str | None = Field(default=None, max_length=200)
    nights_phrase: str | None = Field(default=None, max_length=100)


class ClearItem(Spanned):
    field: ClearableField


class InterpretationDelta(_Delta):
    """Ответ роли «Интерпретатор». Отсутствующее поле — «пользователь об этом не говорил»."""

    origin: TextValue | None = None
    destination: TextValue | None = None
    dates: DatesValue | None = None
    adults: AdultsValue | None = None
    children_ages: AgesValue | None = None
    children_count: ChildrenCount | None = None
    return_trip: BoolValue | None = None
    transport: TransportValue | None = None
    budget: BudgetValue | None = None
    pets: PetsValue | None = None
    breakfast: RequiredBool | None = None
    stars_min: StarsValue | None = None
    free_cancellation: RequiredBool | None = None
    wishes_tags: TagsValue | None = None
    clear: list[ClearItem] = Field(default_factory=list, max_length=10)
    unparsed: list[str] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------- проверка фрагментов


def _norm(text: str) -> str:
    lowered = text.lower().replace("ё", "е")
    lowered = re.sub(r"[«»\"'`“”„]", "", lowered)
    lowered = re.sub(r"[–—]", "-", lowered)
    return " ".join(lowered.split())


def span_in_text(span: str, text: str) -> bool:
    """Фрагмент должен буквально встречаться в сообщении (с точностью до регистра, «ё» и пробелов)."""
    needle = _norm(span).strip(" .,!?;:")
    return bool(needle) and needle in _norm(text)


# ---------------------------------------------------------------- доказательства значений (INT-001…005)
# Дословная цитата ещё не доказывает, что значение модели ей соответствует (аудит v1–v3). Ниже — узкие
# проверки: число в цитате, город в цитате (с русскими падежами), явная обязательность и явная отмена условия.
# Отклоняется только то, что цитате противоречит; если проверяемого признака в цитате нет (например, сумма
# словами «сорок тысяч»), значение принимается, как раньше. Падежи, числительные, диапазоны бюджета и обычные
# формулировки отмены защищены набором tests/unit/test_ru_forms_guard.py.

_WORD_END = r"(?![а-яa-z0-9])"
_WORD_START = r"(?<![а-яa-z0-9])"

# Число: «50 000», «45 500», «1,5», «60000», «1.000.000».
_NUMBER_TOKEN = re.compile(_WORD_START + r"(\d{1,3}(?:[ .]\d{3})+(?![\d])|\d+(?:[.,]\d+)?)")
_MULTIPLIERS: tuple[tuple[re.Pattern[str], Decimal], ...] = (
    (re.compile(r"^\s*(?:млн\.|миллион\w*|млн" + _WORD_END + ")"), Decimal(1_000_000)),
    (re.compile(r"^\s*(?:тыс\w*\.|т\.\s?р\.?|(?:тыс\w*|тр)" + _WORD_END + ")"), Decimal(1000)),
    (re.compile(r"^(?:к|k)" + _WORD_END), Decimal(1000)),  # «50к» — только слитно, «к» отдельно — предлог
)
# Число, за которым идёт не сумма, а состав, длительность, звёзды или возраст.
_NOT_MONEY_TAIL = re.compile(
    r"^\s*(?:человек\w*|чел\b|взросл\w*|дет\w*|ребен\w*|ноч\w*|сут\w*|дн\w*|день|недел\w*|звезд\w*|"
    r"лет\b|год\w*|мес\w*|км\b|час\w*|мин\w*|%|процент\w*|раз\b)"
)
_LOWER_BOUND_HEAD = re.compile(r"(?:^|\s)от\s*$")


def _number_values(raw: str, tail: str) -> list[Decimal]:
    """Возможные значения числа с множителем («тыс.», «млн», «к»). «1.500» без множителя неоднозначно."""
    compact = raw.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", compact):
        values = [Decimal(compact.replace(".", ""))]  # точка как разделитель тысяч
        if compact.count(".") == 1:
            values.append(Decimal(compact))  # «1.500 млн» — возможно, десятичная точка
    else:
        try:
            values = [Decimal(compact.replace(",", "."))]
        except InvalidOperation:
            return []
    for pattern, factor in _MULTIPLIERS:
        if pattern.match(tail):
            return [value * factor for value in values]
    return values


def _budget_evidence_matches(amount: Decimal, span: str) -> bool:
    """Сумма модели должна совпасть с суммой цитаты (с множителем). Бюджет — верхняя граница: в диапазоне
    «от 40 до 50 тысяч» / «40–50 тысяч» лимит — верхний конец; одна нижняя граница («от 40 тысяч») — не лимит."""
    tokens = list(_NUMBER_TOKEN.finditer(span))
    candidates: list[Decimal] = []
    lower_only = False
    for index, match in enumerate(tokens):
        tail = span[match.end() :]
        if _NOT_MONEY_TAIL.match(tail):
            continue
        head = span[: match.start()]
        dash_range = index + 1 < len(tokens) and re.fullmatch(
            r"\s*-\s*", span[match.end() : tokens[index + 1].start()]
        )
        if _LOWER_BOUND_HEAD.search(head) or dash_range:
            lower_only = True  # нижняя граница диапазона — не лимит
            continue
        candidates.extend(_number_values(match.group(1), tail))
    if not candidates:
        return not lower_only
    return any(amount == value for value in candidates)


_COUNT_WORDS = {
    "один": 1,
    "одна": 1,
    "одного": 1,
    "одной": 1,
    "два": 2,
    "две": 2,
    "двое": 2,
    "двух": 2,
    "двоих": 2,
    "три": 3,
    "трое": 3,
    "трех": 3,
    "троих": 3,
    "четыре": 4,
    "четверо": 4,
    "четырех": 4,
    "четверых": 4,
    "пять": 5,
    "пятеро": 5,
    "пяти": 5,
    "пятерых": 5,
    "шесть": 6,
    "шестеро": 6,
    "шести": 6,
    "шестерых": 6,
    "семь": 7,
    "семеро": 7,
    "восемь": 8,
    "девять": 9,
}
_COLLECTIVE = {"вдвоем": 2, "втроем": 3, "вчетвером": 4, "впятером": 5, "вшестером": 6, "всемером": 7}
_COUNT_TOKEN = re.compile(
    _WORD_START
    + r"(\d+|"
    + "|".join(sorted({*_COUNT_WORDS, *_COLLECTIVE}, key=len, reverse=True))
    + r")"
    + _WORD_END
)
_ADULT_TAIL = re.compile(r"^\s*(?:взросл\w*|человек\w*|чел\b|пассажир\w*|турист\w*)")
_CHILD_TAIL = re.compile(
    r"^\s*(?:лет\b|год\w*|мес\w*|дет\w*|ребен\w*|ребят\w*|сын\w*|доч\w*|малыш\w*|младен\w*)"
)
_CHILD_CONTEXT = re.compile(r"ребен|дет(?:и|ей|ьми|ям)|сын|доч|малыш|младен|\bлет\b")


def _adults_evidence_matches(value: int, span: str) -> bool:
    """Число взрослых: явное «2 взрослых», «двое», «вдвоём», «нас четверо»; возраст и число детей не в счёт."""
    explicit: list[int] = []
    generic: list[int] = []
    for match in _COUNT_TOKEN.finditer(span):
        token = match.group(1)
        count = int(token) if token.isdigit() else _COLLECTIVE.get(token) or _COUNT_WORDS[token]
        tail = span[match.end() :]
        if (
            token in _COLLECTIVE
            or _ADULT_TAIL.match(tail)
            or re.search(r"(?:^|\s)нас\s*$", span[: match.start()])
        ):
            explicit.append(count)
        elif not _CHILD_TAIL.match(tail) and not _NOT_MONEY_TAIL.match(tail):
            generic.append(count)
    if explicit:
        return value in explicit
    if generic and not _CHILD_CONTEXT.search(span):
        return value in generic
    return True


def _numeric_evidence_matches(field_name: str, item: Spanned) -> bool:
    """INT-001/INT-004: число модели не должно противоречить числу в цитате."""
    span = _norm(item.span).replace("\u00a0", " ").replace("\u202f", " ")
    if field_name == "budget":
        return _budget_evidence_matches(Decimal(str(item.amount)), span)  # type: ignore[attr-defined]
    if field_name == "adults":
        return _adults_evidence_matches(int(item.value), span)  # type: ignore[attr-defined]
    return True


# Формы, которые не выводятся из основы слова (сокращения, разговорные и латиница).
_CITY_ALIASES = {
    "москва": {"moscow", "мск"},
    "казань": {"kazan"},
    "сочи": {"sochi"},
    "санкт-петербург": {
        "петербург",
        "петербурга",
        "петербурге",
        "петербургу",
        "спб",
        "питер",
        "питера",
        "питере",
        "питеру",
        "saint petersburg",
        "st petersburg",
        "ленинград",
    },
    "нижний новгород": {"нижний", "нижнего", "нижнем", "nizhny novgorod"},
    "калининград": {"kaliningrad"},
    "екатеринбург": {"екб", "екат", "yekaterinburg"},
    "новосибирск": {"новосиб", "novosibirsk"},
    "ростов-на-дону": {"ростов", "ростова", "ростове"},
}
_VOWELS = "аеиоуыэюя"
_ADJECTIVE_ENDINGS = ("ий", "ый", "ой", "ая", "ое", "ые", "ие", "яя")


def _city_word_pattern(word: str) -> str:
    """Слово названия с падежным окончанием до 3 букв: Казань→Казани, Орёл→Орла, Набережные→Набережных."""
    if len(word) <= 2:
        return re.escape(word)
    stems = {word}
    if len(word) >= 5 and word[-2:] in _ADJECTIVE_ENDINGS:
        stems.add(word[:-2])
    if word[-1] in _VOWELS + "йь":
        stems.add(word[:-1])
    elif len(word) >= 4 and word[-2] in "ое" and word[-3] not in _VOWELS:
        stems.add(word[:-2] + word[-1])  # беглая гласная
    return (
        "(?:"
        + "|".join(sorted((re.escape(stem) for stem in stems), key=len, reverse=True))
        + r")[а-яa-z]{0,3}"
    )


def _city_pattern(name: str) -> str:
    parts = re.split(r"([-\s]+)", name)
    pieces = [r"[-\s]*" if index % 2 else _city_word_pattern(part) for index, part in enumerate(parts)]
    return _WORD_START + "".join(pieces) + _WORD_END


_ROLE_MARKER = re.compile(r"(?:^|[\s,.;:!?()])(не\s+)?(из|изо|от|в|во|до|к|ко|на)\s+$")
_ORIGIN_MARKERS = frozenset({"из", "изо", "от"})
_WORD_TOKEN = re.compile(r"[а-яa-z0-9]{2,}")


def _roles_at(span: str, starts: list[int]) -> list[str]:
    """Роль каждого упоминания по предлогу непосредственно перед ним (INT-ROLE).

    ``origin`` — «из/изо/от», ``destination`` — «в/во/до/к/ко/на», ``not_*`` — предлог с отрицанием
    («не из Москвы»), ``none`` — предлога роли нет.
    """
    roles: list[str] = []
    for start in starts:
        marker = _ROLE_MARKER.search(span[:start])
        if marker is None:
            roles.append("none")
            continue
        role = "origin" if marker.group(2) in _ORIGIN_MARKERS else "destination"
        roles.append(f"not_{role}" if marker.group(1) else role)
    return roles


def _span_roles(span: str) -> set[str]:
    """Роли, которые текст цитаты вообще называет: нужно, чтобы отличить маршрут от одного города."""
    starts = [match.start() for match in _WORD_TOKEN.finditer(span)]
    return {role for role in _roles_at(span, starts) if role in ("origin", "destination")}


def _city_evidence_matches(item: TextValue, field_name: str | None = None) -> bool:
    """INT-002: город модели должен быть назван в цитате — в любом падеже или известным сокращением.
    «Из Москвы» → «Сочи» отклоняется; «из Орла» → «Орёл», «в Ростове-на-Дону» → «Ростов-на-Дону» принимаются.

    INT-ROLE: если цитата называет оба направления («из X в Y»), город с предлогом противоположной роли не
    принимается за это поле; «не из X» роль не подтверждает. Когда роль в цитате одна или не выражена,
    действует прежняя проверка упоминания — иначе обычные запросы вроде «в Казань» теряли бы город."""
    city = _norm(item.value)
    span = _norm(item.span)
    names = {city}
    for canonical, aliases in _CITY_ALIASES.items():
        if city == canonical or city in aliases:
            names |= {canonical, *aliases}
    starts: list[int] = []
    seen: set[int] = set()
    for name in names:
        for match in re.finditer(_city_pattern(name), span):
            if match.start() not in seen:
                seen.add(match.start())
                starts.append(match.start())
    roles = _roles_at(span, sorted(starts))
    if not roles:
        return False
    if field_name not in ("origin", "destination"):
        return True
    if field_name in roles or all(role == "none" for role in roles):
        return True
    if f"not_{field_name}" in roles:
        return False  # «не из Москвы»: отправление этим городом не подтверждено
    return field_name not in _span_roles(span)


# INT-003: обязательность, явно выраженная в цитате, определяется кодом (промпт, правило 6).
_REQUIREDNESS_FIELDS = frozenset(
    {"budget", "transport", "pets", "breakfast", "stars_min", "free_cancellation"}
)
_MANDATORY_RE = re.compile(
    _WORD_START + r"(?<!не )(?:обязател\w*|строго|только|исключительно|не\s+дороже|не\s+больше|не\s+выше|"
    r"не\s+более)" + _WORD_END
)
_PREFERRED_RE = re.compile(
    _WORD_START + r"(?:желательно|предпочтительно|хотелось\s+бы|по\s+возможности|если\s+получится|"
    r"если\s+можно|не\s+обязател\w*|необязател\w*|можно\s+без)" + _WORD_END
)


def _required_from_span(name: str, item: Spanned) -> bool | None:
    """True/False — признак обязательности в цитате; None — признаков нет или они противоречат друг другу."""
    if name not in _REQUIREDNESS_FIELDS:
        return None
    span = _norm(item.span)
    mandatory = bool(_MANDATORY_RE.search(span))
    preferred = bool(_PREFERRED_RE.search(span))
    if mandatory == preferred:
        return None
    return mandatory


# INT-005: подлинная цитата не доказывает отмену условия. Нужны тема поля и признак отмены в одной части
# фразы (не дальше 3 слов, без чисел между ними); часть с признаком обязательности отменой не считается.
_REVOKE_COMMON = (
    r"не\s+(?:так\s+|очень\s+|особо\s+)?(?:важ\w*|принципиальн\w*|критичн\w*|имеет\s+значени\w*)|неважн\w*|"
    r"все\s+равно|без\s+разницы|не\s+учитыва\w*|убер\w*|убра\w*|отмен(?:и|ите|яем|им)|забуд\w*|сними\w*|"
    r"снимаем|снять"
)
_CLEAR_TOPIC: dict[str, str] = {
    "budget": r"бюджет\w*|деньг\w*|денег|цен[аеуы]|ценой|стоимост\w*|сумм[аеуы]",
    "transport": r"транспорт\w*|самолет\w*|поезд\w*|авиа\w*|перелет\w*|ж/?д|электричк\w*|на\s+чем",
    "pets": r"собак\w*|пес|пса|псом|кошк\w*|кот|кота|котом|животн\w*|питом\w*",
    "breakfast": r"завтрак\w*",
    "stars_min": r"звезд\w*|звездност\w*|категори\w*|класс\w*\s+(?:отел\w*|гостиниц\w*)",
    "free_cancellation": r"отмен(?:а|ы|у|ой|е)|возврат\w*",
    "wishes_tags": r"пожелани\w*|экскурси\w*|достопримечательност\w*|развлечени\w*|программ\w*",
    "children_ages": r"дет(?:и|ей|ьми|ям|ях)|ребен\w*|ребят\w*|сын\w*|доч\w*|малыш\w*",
    "return_trip": r"обратн\w*|назад|возвращ\w*|вернем\w*|вернуться",
}
_CLEAR_REVOKE: dict[str, str] = {
    "budget": _REVOKE_COMMON
    + r"|любой|любая|любые|любую|не\s+ограничен\w*|без\s+ограничени\w*|без\s+лимит\w*",
    "transport": _REVOKE_COMMON
    + r"|можно\s+(?:и|тоже|также)|тоже\s+(?:можно|подойд\w*|подход\w*|ок|норм\w*)|"
    r"также\s+(?:можно|подойд\w*)|не\s+обязательно|необязательно|люб\w*\s+(?:вид\w*\s+)?транспорт\w*",
    "pets": _REVOKE_COMMON
    + r"|без|не\s+(?:едет|едут|поедет|поедут|берем|возьмем)|оста(?:ется|ются|нется|нутся|вляем|вим)",
    "breakfast": _REVOKE_COMMON
    + r"|без|не\s+(?:нужен|нужны|нужно|надо|обязател\w*)|необязател\w*|можно\s+без",
    "stars_min": _REVOKE_COMMON
    + r"|любой|любая|любые|любую|любого|не\s+(?:нуж(?:ен|на|но|ны)|обязател\w*)|необязател\w*",
    "free_cancellation": _REVOKE_COMMON
    + r"|без|не\s+(?:нуж(?:ен|на|но|ны)|надо|обязател\w*)|необязател\w*|можно\s+без",
    "wishes_tags": _REVOKE_COMMON + r"|без|не\s+(?:нуж(?:ен|на|но|ны)|надо)",
    "children_ages": _REVOKE_COMMON + r"|без|не\s+(?:едет|едут|поедет|поедут|летит|летят|будет|будут)|"
    r"оста(?:ется|ются|нется|нутся|вляем|вим)",
    "return_trip": _REVOKE_COMMON
    + r"|без|не\s+(?:нуж(?:ен|на|но|ны)|надо|нужно|обязател\w*|планиру\w*|будет|будем|"
    r"собираемся)|необязател\w*",
}
# Устойчивые обороты отмены, где тема и признак не разделяются (проверяются по всей цитате).
_CLEAR_IDIOMS: dict[str, str] = {
    "budget": r"без\s+бюджет\w*|(?:не\s*важно|все\s+равно|без\s+разницы),?\s+сколько\s+(?:стоит|будет\s+стоить)",
    "transport": r"(?:не\s*важно|все\s+равно|без\s+разницы),?\s+(?:на\s+чем|чем)\s+(?:ехать|добираться|лететь)",
    "children_ages": r"только\s+взросл\w*|одни\s+взросл\w*",
    "return_trip": r"в\s+одну\s+сторону|в\s+один\s+конец",
}
_CLAUSE_SPLIT = re.compile(r"[,.;:!?()]|\s(?:но|а|однако|зато|хотя)\s")


def _clear_evidence_matches(field_name: str, span: str) -> bool:
    text = _norm(span)
    idiom = _CLEAR_IDIOMS.get(field_name)
    if idiom and re.search(_WORD_START + "(?:" + idiom + ")" + _WORD_END, text):
        return True
    topic_re = re.compile(_WORD_START + "(?:" + _CLEAR_TOPIC[field_name] + ")" + _WORD_END)
    revoke_re = re.compile(_WORD_START + "(?:" + _CLEAR_REVOKE[field_name] + ")" + _WORD_END)
    for clause in _CLAUSE_SPLIT.split(text):
        if _MANDATORY_RE.search(clause):
            continue
        for topic in topic_re.finditer(clause):
            for revoke in revoke_re.finditer(clause):
                if topic.start() < revoke.end() and revoke.start() < topic.end():
                    return True  # «любой транспорт»: признак включает тему
                if topic.end() <= revoke.start():
                    gap = clause[topic.end() : revoke.start()]
                else:
                    gap = clause[revoke.end() : topic.start()]
                if len(gap.split()) <= 3 and not re.search(r"\d", gap):
                    return True
    return False


@dataclass(slots=True)
class DeltaReport:
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    questions: list[Ambiguity] = field(default_factory=list)
    date_expressions: list[DateExpression] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Длительность названа раньше даты отъезда («на 3 ночи»): хранится до появления даты (UC-02).
    pending_nights: str | None = None
    # Цитаты условий, распознанных кодом без отдельного поля дельты (INT-007: тип номера).
    spans: dict[str, str] = field(default_factory=dict)


# Уточнения, которые переносятся между ходами через ``TripRequest.ambiguities`` (UC-02: полученное не теряется).
NIGHTS_PENDING_ID = "amb-llm-nights"
# DEC-34: относительная фраза даты ждёт часовой пояс пункта отправления.
DATES_TZ_PENDING_ID = "amb-llm-dates-tz"
# DATE-003: период («в ноябре») ждёт выбора окна поездки или длительности.
DATES_PERIOD_PENDING_ID = "amb-llm-dates-period"
# DATE-002: дата отъезда понятна («завтра»), ждёт дату возвращения или длительность.
DATES_END_PENDING_ID = "amb-llm-dates-end"
_DATE_PENDING_IDS = (DATES_TZ_PENDING_ID, DATES_PERIOD_PENDING_ID, DATES_END_PENDING_ID)
# REPLAN-001: глаголы пребывания — контекст, в котором число без единицы означает ночи.
_STAY_RE = re.compile(
    _WORD_START
    + r"(?:остан\w*|оста[её]м\w*|побуд\w*|задерж\w*|прожив\w*|пожив\w*|продл\w*|переноч\w*)"
    + _WORD_END
)
# INT-006/INT-007: фразы, которые учитываются кодом и не должны попадать в «не удалось учесть».
_HOTEL_NEEDED_RE = re.compile(
    r"^(?:и\s+|а\s+|также\s+|еще\s+|ну\s+)?(?:нам\s+|мне\s+)?(?:(?:обязательно|тоже|также)\s+)?"
    r"(?:нуж(?:ен|на|но|ны)|требуется|надо|понадобится|с)\s+(?:(?:и|тоже|также)\s+)?"
    r"(?:отел\w*|гостиниц\w*|жиль\w*|проживани\w*|ночлег\w*|размещени\w*)$"
)
CHILDREN_PENDING_ID = "amb-llm-children"
_CHILD_WORDS = re.compile(r"(ребен|ребён|дет(и|ей|ьми|ям)\b|сын|доч|малыш|младен)", re.IGNORECASE)


FIELD_TITLES = {
    "origin": "город отправления",
    "destination": "направление",
    "dates": "даты",
    "adults": "число взрослых",
    "children_ages": "дети",
    "children_count": "дети",
    "return_trip": "обратный путь",
    "transport": "вид транспорта",
    "budget": "бюджет",
    "pets": "животные",
    "breakfast": "завтрак",
    "stars_min": "звёздность",
    "free_cancellation": "бесплатная отмена",
    "wishes_tags": "пожелания",
}


def _question(
    field_name: str,
    text: str,
    span: str | None,
    blocking: str = "blocking_now",
    ambiguity_id: str | None = None,
    options: list[AmbiguityOption] | None = None,
) -> Ambiguity:
    return Ambiguity(
        ambiguity_id=ambiguity_id or f"amb-llm-{field_name}",
        field=field_name,
        question=text,
        blocking_class=blocking,  # type: ignore[arg-type]
        source_span=span,
        options=options or [],
    )


def _date_options(windows: tuple[tuple[Any, Any], ...]) -> list[AmbiguityOption]:
    """DATE-003: предложенные окна поездки — кнопки выбора (не больше четырёх); это не бронь."""
    return [
        AmbiguityOption(
            option_id=calendar.window_option_id(start, end),
            label=calendar.format_window(start, end),
            date_from=start,
            date_to=end,
        )
        for start, end in windows[: calendar.MAX_WINDOWS]
    ]


def _children_question(span: str | None) -> Ambiguity:
    shown = f" (упомянуто: «{span}»)" if span else ""
    return _question(
        "children_ages",
        f"Сколько лет каждому ребёнку{shown}? Возраст нужен для билетов и размещения.",
        span,
        ambiguity_id=CHILDREN_PENDING_ID,
    )


def _nights_pending_question(phrase: str) -> Ambiguity:
    return _question(
        "date_from",
        f"С какой даты начинается поездка? Длительность «{phrase}» учтена.",
        phrase,
        ambiguity_id=NIGHTS_PENDING_ID,
    )


def _date_end_question(phrase: str, start: Any, origin: str | None, timezone_name: str | None) -> Ambiguity:
    basis = f", по местной дате в пункте отправления «{origin}»" if timezone_name and origin else ""
    return _question(
        "date_to",
        f"Отъезд — {calendar.format_day(start)} («{phrase}»{basis}). "
        "Когда обратно: дата возвращения или число ночей?",
        phrase,
        ambiguity_id=DATES_END_PENDING_ID,
    )


def pending_from(ambiguities: list[Ambiguity] | None) -> tuple[str | None, Ambiguity | None]:
    """Незакрытые уточнения прошлого хода: (фраза длительности, вопрос о возрасте детей)."""
    nights, children = None, None
    for item in ambiguities or []:
        if item.ambiguity_id == NIGHTS_PENDING_ID and item.source_span:
            nights = item.source_span
        elif item.ambiguity_id == CHILDREN_PENDING_ID:
            children = item
    return nights, children


def _city_mention_pattern(name: str) -> str:
    """Строгий шаблон названия для резервного распознавания (CITY-002): название целиком или основа
    с обычным падежным окончанием («Казани», «Калининграде»); «сочный» за «Сочи» не принимается."""
    word = _norm(name)
    if re.search(r"[-\s]", word):
        return _city_pattern(word)
    stem = word[:-1] if word[-1] in _VOWELS + "ьй" else word
    endings = r"(?:а|у|е|и|ы|ь|ью|ом|ем|ой|ю|я)?"
    return _WORD_START + "(?:" + re.escape(word) + "|" + re.escape(stem) + endings + ")" + _WORD_END


_NEGATED_BEFORE = re.compile(r"(?:^|[\s,.;:!?()])(?:не|нет|кроме|вместо)\s+(?:(?:в|во|на|до)\s+)?$")


def _delta_is_empty(delta: InterpretationDelta) -> bool:
    return all(
        getattr(delta, name) in (None, []) for name in InterpretationDelta.model_fields if name != "unparsed"
    )


def _destination_fallback(
    delta: InterpretationDelta,
    text: str,
    values: dict[str, Any],
    previous_ambiguities: list[Ambiguity] | None = None,
) -> tuple[str, str] | None:
    """CITY-002: модель не вернула ни «откуда», ни «куда», но в неразобранном фрагменте (или во всём
    сообщении, если модель ничего не извлекла) назван ровно один известный город без роли «откуда» и без
    отрицания («Нет, лучше Казань»). Тогда это новое направление. Иначе — ничего не угадывается.
    Пока город отправления неизвестен или о нём задан вопрос, голый город может быть ответом «откуда» —
    роль не угадывается."""
    if delta.destination is not None or delta.origin is not None or not values.get("origin"):
        return None
    if any(
        item.field == "origin" or item.ambiguity_id == DATES_TZ_PENDING_ID
        for item in previous_ambiguities or []
    ):
        return None
    sources = [fragment for fragment in delta.unparsed if span_in_text(fragment, text)]
    if _delta_is_empty(delta):
        sources.append(text)
    origin = normalize_city(values.get("origin"))
    found: dict[str, str] = {}
    rejected: set[str] = set()
    for source in sources:
        span = _norm(source)
        for place in DEMO_PLACES:
            names = {place.name, *(alias for alias in place.aliases if re.search(r"[а-я]", alias))}
            for name in names:
                for match in re.finditer(_city_mention_pattern(name), span):
                    role = _roles_at(span, [match.start()])[0]
                    if role in ("origin", "not_destination", "not_origin") or _NEGATED_BEFORE.search(
                        span[: match.start()]
                    ):
                        rejected.add(place.name)
                    else:
                        found.setdefault(place.name, source.strip()[:300])
    chosen = [name for name in found if name not in rejected and (origin is None or name != origin.name)]
    if len(chosen) != 1:
        return None
    name = chosen[0]
    current = normalize_city(values.get("destination"))
    if current is not None and current.name == name:
        return None
    return name, found[name]


def _date_fallback(
    delta: InterpretationDelta, text: str, values: dict[str, Any]
) -> tuple[str, str | None] | None:
    """DATE-004/005: модель не вернула фразу даты. Дословная фраза даты берётся из неразобранного
    фрагмента; относительная («послезавтра», «через два дня») — и из всего сообщения, пока даты поездки
    ещё не известны. «Сегодня» без явной привязки к поездке не используется."""
    if delta.dates is not None and delta.dates.date_phrase:
        return None
    for fragment in delta.unparsed:
        if not span_in_text(fragment, text):
            continue
        literal = calendar.literal_date_phrase(fragment)
        if literal is not None and span_in_text(literal, text):
            return literal, fragment
    if values.get("date_from") is None:
        literal = calendar.literal_date_phrase(text)
        if literal is not None and literal != "сегодня" and calendar.is_relative_phrase(literal):
            return literal, None
    return None


def apply_delta(
    delta: InterpretationDelta,
    *,
    text: str,
    base: StructuredForm,
    reference_datetime: datetime,
    timezone_for: Callable[[str], str | None] | None = None,
    previous_ambiguities: list[Ambiguity] | None = None,
    previous_date_expressions: list[DateExpression] | None = None,
) -> tuple[StructuredForm, DeltaReport]:
    """Накладывает проверенную дельту на прежние условия. Непроверенное значение не применяется."""
    report = DeltaReport()
    values: dict[str, Any] = base.model_dump()
    pending_nights, pending_children = pending_from(previous_ambiguities)

    def check(name: str, item: Spanned | None) -> bool:
        if item is None:
            return False
        if not span_in_text(item.span, text):
            reason = "фрагмент не найден в сообщении"
        elif not _numeric_evidence_matches(name, item):
            reason = "число не совпадает с названным в сообщении"  # INT-001/INT-004
        elif name in ("origin", "destination") and not _city_evidence_matches(item, name):  # type: ignore[arg-type]
            mentioned = _city_evidence_matches(item)  # INT-002: город назван, но в другой роли (INT-ROLE)
            reason = "город назван в другой роли" if mentioned else "город не назван в цитате"
        else:
            report.accepted.append(name)
            return True
        report.rejected.append(name)
        report.notes.append(f"Значение «{FIELD_TITLES.get(name, name)}» не принято: {reason}")
        return False

    def required(name: str, item: Spanned, model_value: bool) -> bool:
        """INT-003: явная обязательность или пожелание в цитате важнее мнения модели."""
        from_span = _required_from_span(name, item)
        if from_span is None or from_span == model_value:
            return model_value
        report.notes.append(
            f"Обязательность условия «{FIELD_TITLES.get(name, name)}» определена по формулировке «{item.span}»"
        )
        return from_span

    # Отмена условий («бюджет не важен», «можно и самолётом»): дословный фрагмент с явной отменой (INT-005).
    for item in delta.clear:
        if not span_in_text(item.span, text) or not _clear_evidence_matches(item.field, item.span):
            report.rejected.append(f"clear:{item.field}")
            report.notes.append(
                f"Условие «{FIELD_TITLES.get(item.field, item.field)}» сохранено: явной отмены в сообщении нет"
            )
            continue
        report.accepted.append(f"clear:{item.field}")
        if item.field == "budget":
            values.update(budget_amount=None, budget_currency=None, budget_required=False)
        elif item.field == "transport":
            values.update(transport_modes=[], transport_modes_required=False)
        elif item.field == "pets":
            values.update(pets=[], pets_required=False)
        elif item.field == "breakfast":
            values.update(breakfast=None, breakfast_required=False)
        elif item.field == "stars_min":
            values.update(stars_min=None, stars_required=False)
        elif item.field == "free_cancellation":
            values.update(free_cancellation=None, free_cancellation_required=False)
        elif item.field == "wishes_tags":
            values.update(wishes_tags=[])
        elif item.field == "children_ages":
            values.update(children_ages=[])
            pending_children = None  # «без детей»: вопрос о возрасте снимается
        elif item.field == "return_trip":
            values.update(return_trip=None)

    if check("origin", delta.origin):
        assert delta.origin is not None
        values["origin"] = delta.origin.value.strip()
    if check("destination", delta.destination):
        assert delta.destination is not None
        values["destination"] = delta.destination.value.strip()
    used_fragments: set[str] = set()
    if (city := _destination_fallback(delta, text, values, previous_ambiguities)) is not None:
        values["destination"], fragment = city
        used_fragments.add(fragment)
        report.accepted.append("destination")
        report.notes.append(f"Направление изменено: «{city[0]}» (по фразе «{fragment[:120]}»)")
    if check("adults", delta.adults):
        assert delta.adults is not None
        values["adults"] = delta.adults.value
    if check("children_ages", delta.children_ages):
        assert delta.children_ages is not None
        values["children_ages"] = list(delta.children_ages.value)
    # UC-02/DEC-25: состав группы не предполагается. Дети без возраста — блокирующий вопрос, а не план
    # «без детей»; вопрос переносится между ходами, пока возрасты не названы.
    children_span: str | None = None
    if check("children_count", delta.children_count):
        assert delta.children_count is not None
        if len(values.get("children_ages") or []) < delta.children_count.value:
            children_span = delta.children_count.span
    if children_span is None and not values.get("children_ages"):
        for fragment in delta.unparsed:
            if _CHILD_WORDS.search(fragment) and span_in_text(fragment, text):
                children_span = fragment.strip()[:300]
                break
    if children_span is not None:
        report.questions.append(_children_question(children_span))
    elif pending_children is not None and not values.get("children_ages"):
        report.questions.append(pending_children)
    if check("return_trip", delta.return_trip):
        assert delta.return_trip is not None
        values["return_trip"] = delta.return_trip.value
    if check("transport", delta.transport):
        assert delta.transport is not None
        modes = _transport_modes(delta.transport)
        if modes:
            values["transport_modes"] = modes
            values["transport_modes_required"] = bool(
                required("transport", delta.transport, delta.transport.required) or delta.transport.exclude
            )
    if check("budget", delta.budget):
        assert delta.budget is not None
        values.update(
            budget_amount=delta.budget.amount,
            budget_currency=delta.budget.currency,
            budget_required=required("budget", delta.budget, delta.budget.required),
        )
    if check("pets", delta.pets):
        assert delta.pets is not None
        values["pets"] = [pet.model_dump() for pet in delta.pets.value]
        values["pets_required"] = required("pets", delta.pets, delta.pets.required)
    if check("breakfast", delta.breakfast):
        assert delta.breakfast is not None
        values.update(
            breakfast=delta.breakfast.value,
            breakfast_required=required("breakfast", delta.breakfast, delta.breakfast.required),
        )
    if check("stars_min", delta.stars_min):
        assert delta.stars_min is not None
        values.update(
            stars_min=delta.stars_min.value,
            stars_required=required("stars_min", delta.stars_min, delta.stars_min.required),
        )
    if check("free_cancellation", delta.free_cancellation):
        assert delta.free_cancellation is not None
        values.update(
            free_cancellation=delta.free_cancellation.value,
            free_cancellation_required=required(
                "free_cancellation", delta.free_cancellation, delta.free_cancellation.required
            ),
        )
    if check("wishes_tags", delta.wishes_tags):
        assert delta.wishes_tags is not None
        known = [tag for tag in (t.strip().lower() for t in delta.wishes_tags.value) if tag in WISH_TAGS]
        unknown = [tag for tag in delta.wishes_tags.value if tag.strip().lower() not in WISH_TAGS]
        values["wishes_tags"] = known[:6]
        if unknown:
            report.unparsed.append(delta.wishes_tags.span)

    pending_dates = _pending_date_phrase(previous_ambiguities, previous_date_expressions)
    # REPLAN-001: число без единицы («останемся на четыре») — ночи, если контекст однозначен: есть даты
    # поездки (или ожидаемая фраза даты) и глагол пребывания, либо прошлый ход спрашивал длительность.
    awaiting_nights = pending_dates is not None and pending_dates[2] in (
        DATES_PERIOD_PENDING_ID,
        DATES_END_PENDING_ID,
    )
    has_dates = base.date_from is not None or pending_dates is not None
    bare_count = awaiting_nights or (has_dates and bool(_STAY_RE.search(_norm(text))))
    dates = delta.dates
    if (found := _date_fallback(delta, text, values)) is not None:
        literal, fragment = found
        dates = DatesValue(date_phrase=literal, nights_phrase=dates.nights_phrase if dates else None)
        if fragment is not None:
            used_fragments.add(fragment)
    if (dates is None or not dates.nights_phrase) and bare_count:
        for fragment in delta.unparsed:
            if span_in_text(fragment, text) and calendar.nights_value(fragment, bare_count=True)[0]:
                # Модель не отнесла фразу к длительности, но контекст однозначен — фраза дословная.
                dates = DatesValue(date_phrase=dates.date_phrase if dates else None, nights_phrase=fragment)
                used_fragments.add(fragment)
                break
    known_nights = (
        (base.date_to - base.date_from).days
        if base.date_from is not None and base.date_to is not None
        else None
    )
    _apply_dates(
        dates,
        text=text,
        values=values,
        reference=reference_datetime,
        report=report,
        resolver=timezone_for,
        carried_nights=pending_nights,
        known_nights=known_nights,
        bare_count=bare_count,
        start_weekday=calendar.weekday_preference(text),
    )
    new_phrase = dates is not None and bool(dates.date_phrase)
    if pending_dates is not None and not new_phrase and values.get("date_from") is None:
        # DEC-34: фраза прошлого хода («завтра») разрешается, когда стал известен пункт отправления,
        # от исходной опорной даты; без пояса вопрос повторяется. DATE-002/DATE-003: так же ждут длительность
        # дата отъезда без возвращения и период («в ноябре»).
        phrase, original_reference, _kind, embedded = pending_dates
        weekday = calendar.weekday_preference(text)
        if weekday is None and embedded:
            weekday = calendar.weekday_preference(embedded)
        embedded_nights = embedded if embedded and calendar.inline_nights(embedded) else None
        _resolve_dates(
            phrase,
            report.pending_nights or pending_nights or embedded_nights,
            values=values,
            reference=original_reference or reference_datetime,
            report=report,
            resolver=timezone_for,
            start_weekday=weekday,
        )
        if values.get("date_to") is not None:
            report.pending_nights = pending_nights = None
    elif (
        pending_dates is not None
        and pending_dates[2] == DATES_END_PENDING_ID
        and new_phrase
        and values.get("date_from") is not None
        and values.get("date_to") is None
    ):
        _answer_return_date(pending_dates, values=values, report=report, resolver=timezone_for)
    _apply_pending_nights(report.pending_nights or pending_nights, values=values, report=report)

    for fragment in delta.unparsed:
        if fragment.strip()[:300] == children_span or fragment in used_fragments:
            continue  # фрагмент о детях стал вопросом о возрасте, о длительности — длительностью
        if _absorbed(fragment):
            continue
        room = _room_preference(fragment) if span_in_text(fragment, text) else None
        if room == "soft":
            # INT-007: мягкое пожелание сохраняется в условиях поездки и переносится между ходами.
            values["room_type"] = "family"
            report.accepted.append("room_type")
            report.spans["hotel_room_type"] = fragment.strip()[:300]
            report.notes.append(
                f"Пожелание «{fragment.strip()[:120]}» сохранено как мягкое предпочтение: варианты по нему "
                "не отсеиваются, тип номера источником не подтверждён"
            )
            continue
        if room == "revoke":
            if values.get("room_type") is not None:
                values["room_type"] = None
                report.accepted.append("clear:room_type")
                report.notes.append("Пожелание о семейном номере снято")
            continue
        if span_in_text(fragment, text) and fragment not in report.unparsed:
            report.unparsed.append(fragment.strip()[:300])

    form = _validated_form(values, base, report)
    return form, report


def _transport_modes(value: TransportValue) -> list[str]:
    include = list(dict.fromkeys(value.include))
    exclude = set(value.exclude)
    if include:
        return [mode for mode in include if mode not in exclude]
    if exclude:
        return [mode for mode in ("rail", "flight") if mode not in exclude]
    return []


def _timezone_for(values: dict[str, Any], resolver: Callable[[str], str | None] | None = None) -> str | None:
    """Часовой пояс для календаря — только пункта отправления (DEC-34). Пояс пункта назначения и пояс по
    умолчанию не подставляются: без достоверного пояса отправления календарь сам решает, влияет ли это на
    результат. В Live — только геокодинг через оркестратор (без демо-справочника, решение 25.10: нет
    незаметной подмены Live на Mock); в Mock — демо-справочник."""
    name = values.get("origin")
    if not name:
        return None
    if resolver is not None:
        return resolver(str(name)) or None
    place = normalize_city(name)
    return place.timezone if place is not None else None


def _timezone_question(phrase: str, origin: str | None) -> Ambiguity:
    if origin:
        reason = f"часовой пояс пункта отправления «{origin}» не определён"
        ask = "Назовите даты числами."
    else:
        reason = "пункт отправления ещё не назван"
        ask = "Назовите город отправления или даты числами."
    return _question(
        "date_from",
        f"Уточните даты: «{phrase}» считается по местной дате в пункте отправления, а {reason}. {ask}",
        phrase,
        ambiguity_id=DATES_TZ_PENDING_ID,
    )


def _pending_date_phrase(
    ambiguities: list[Ambiguity] | None, expressions: list[DateExpression] | None
) -> tuple[str, datetime | None, str, str | None] | None:
    """Фраза даты прошлого хода, ожидающая пояс отправления, длительность или выбор окна: (фраза даты,
    исходная опорная дата, вид ожидания, длительность, сохранённая вместе с периодом)."""
    for item in ambiguities or []:
        if item.ambiguity_id in _DATE_PENDING_IDS and item.source_span:
            span = item.source_span
            for expression in reversed(expressions or []):
                if not span.startswith(expression.raw_text):
                    continue
                if item.ambiguity_id == DATES_TZ_PENDING_ID and expression.timezone is not None:
                    continue
                embedded = span[len(expression.raw_text) :].strip() or None
                return expression.raw_text, expression.reference_datetime, item.ambiguity_id, embedded
            return span, None, item.ambiguity_id, None
    return None


def _absorbed(fragment: str) -> bool:
    """INT-006: «Нужен отель» — проживание и так входит в подбор; это не «неучтённое» условие."""
    cleaned = re.sub(r"[,.;:!?]+", " ", _norm(fragment))
    return bool(_HOTEL_NEEDED_RE.match(" ".join(cleaned.split())))


_FAMILY_ROOM_RE = re.compile(
    _WORD_START + r"(?:семейн\w*\s+(?:номер\w*|комнат\w*|размещени\w*)|номер\w*\s+для\s+семь\w*)" + _WORD_END
)


_ROOM_REVOKE_RE = re.compile(
    _WORD_START
    + r"(?:не\s+(?:нуж(?:ен|на|но|ны)|обязател\w*|важ\w*)|необязател\w*|неважн\w*|без\s+семейн\w*|"
    r"любой\s+номер|все\s+равно)" + _WORD_END
)


def _room_preference(fragment: str) -> str | None:
    """INT-007: «желательно семейный номер» — мягкое пожелание ("soft"); «семейный номер не нужен» — отмена
    ("revoke"). Обязательное требование так не учитывается (проверить его по источнику нельзя) и остаётся
    в «не удалось учесть»."""
    text = _norm(fragment)
    if not _FAMILY_ROOM_RE.search(text):
        return None
    if _ROOM_REVOKE_RE.search(text):
        return "revoke"
    required = _MANDATORY_RE.search(text) or re.search(
        _WORD_START + r"(?:нуж(?:ен|на|но|ны)|требуется)", text
    )
    return None if required else "soft"


def _answer_return_date(
    pending: tuple[str, datetime | None, str, str | None],
    *,
    values: dict[str, Any],
    report: DeltaReport,
    resolver: Callable[[str], str | None] | None,
) -> None:
    """DATE-002: на вопрос «когда обратно?» названа одна дата — это дата возвращения, а не новый отъезд."""
    phrase, reference, _kind, _nights = pending
    if reference is None:
        return
    previous = calendar.resolve_trip_dates(phrase, None, reference, _timezone_for(values, resolver))
    answer = values["date_from"]
    if previous.status != "partial" or previous.start is None or answer <= previous.start:
        return
    report.date_expressions.extend(previous.expressions)
    values["date_from"], values["date_to"] = previous.start, answer
    report.questions = [q for q in report.questions if q.field not in ("date_from", "date_to")]


def _apply_dates(
    dates: DatesValue | None,
    *,
    text: str,
    values: dict[str, Any],
    reference: datetime,
    report: DeltaReport,
    resolver: Callable[[str], str | None] | None = None,
    carried_nights: str | None = None,
    known_nights: int | None = None,
    bare_count: bool = False,
    start_weekday: int | None = None,
) -> None:
    if dates is None or not dates.date_phrase:
        if dates is not None and dates.nights_phrase:
            nights_ok = calendar.nights_value(dates.nights_phrase, bare_count=bare_count)[0]
            if values.get("date_from") is not None:
                # «давай на 4 ночи» при уже известной дате отъезда
                _apply_nights_only(
                    dates.nights_phrase, text=text, values=values, report=report, bare_count=bare_count
                )
            elif span_in_text(dates.nights_phrase, text) and nights_ok:
                # «на 3 ночи» без даты отъезда: длительность сохраняется до появления даты (UC-02)
                report.pending_nights = (
                    dates.nights_phrase.strip() if not bare_count else calendar.nights_phrase(nights_ok)
                )
            else:
                report.rejected.append("nights")
        return
    date_phrase = dates.date_phrase
    if not span_in_text(date_phrase, text):
        # DATE-002: модель вернула вычисленную дату («29 октября») вместо цитаты «завтра». Даты считает код:
        # берётся единственная однозначная фраза даты из самого сообщения, иначе — вопрос.
        literal = calendar.literal_date_phrase(text)
        if literal is None:
            report.rejected.append("dates")
            report.questions.append(
                _question("date_from", "Уточните даты поездки: отъезд и возвращение.", None)
            )
            return
        date_phrase = literal
    nights_phrase = dates.nights_phrase
    if nights_phrase and not span_in_text(nights_phrase, text):
        report.rejected.append("nights")
        nights_phrase = None
    _resolve_dates(
        date_phrase,
        nights_phrase or carried_nights,
        values=values,
        reference=reference,
        report=report,
        resolver=resolver,
        known_nights=known_nights,
        bare_count=bare_count,
        start_weekday=start_weekday,
    )


def _resolve_dates(
    date_phrase: str,
    nights_phrase: str | None,
    *,
    values: dict[str, Any],
    reference: datetime,
    report: DeltaReport,
    resolver: Callable[[str], str | None] | None,
    known_nights: int | None = None,
    bare_count: bool = False,
    start_weekday: int | None = None,
) -> None:
    timezone_name = _timezone_for(values, resolver)
    resolved = calendar.resolve_trip_dates(
        date_phrase,
        nights_phrase,
        reference,
        timezone_name,
        known_nights=known_nights,
        bare_count=bare_count,
        start_weekday=start_weekday,
    )
    report.date_expressions.extend(resolved.expressions)
    if resolved.status == "resolved":
        report.accepted.append("dates")
        values["date_from"] = resolved.start
        values["date_to"] = resolved.end
        if not nights_phrase and known_nights and resolved.rule.endswith(f"сохранена: {known_nights}"):
            report.notes.append(
                f"Длительность текущих условий сохранена: {known_nights} {calendar.nights_word(known_nights)}"
            )
        return
    if resolved.status == "partial":
        # DATE-002: дата отъезда понятна — спрашивается только недостающее (возвращение или длительность);
        # фраза ждёт ответа следующего хода от исходной опорной даты.
        report.accepted.append("dates")
        values["date_from"] = resolved.start
        values["date_to"] = None
        report.questions.append(
            _date_end_question(date_phrase.strip(), resolved.start, values.get("origin"), timezone_name)
        )
        return
    if resolved.status == "period":
        # DATE-003: период — ограничение, а не даты. Прежние даты не выдаются за согласованные; выбор окна
        # или длительности — следующим ходом. Длительность сохраняется вместе с фразой периода.
        values["date_from"] = values["date_to"] = None
        span = date_phrase.strip()
        # Длительность и день отъезда хранятся вместе с фразой периода до выбора варианта.
        if resolved.nights:
            span = f"{span} {calendar.nights_phrase(resolved.nights)}"
        if resolved.start_weekday is not None:
            span = f"{span} {calendar.weekday_preference_phrase(resolved.start_weekday)}"
        report.questions.append(
            _question(
                "date_from",
                resolved.question or "",
                span,
                ambiguity_id=DATES_PERIOD_PENDING_ID,
                options=_date_options(resolved.windows),
            )
        )
        return
    if resolved.needs_timezone:
        # DEC-34: пояс не угадывается; фраза ждёт пункт отправления, длительность сохраняется (UC-02).
        report.questions.append(_timezone_question(date_phrase.strip(), values.get("origin")))
        if nights_phrase and calendar.nights_value(nights_phrase, bare_count=bare_count)[0]:
            report.pending_nights = nights_phrase.strip()
        return
    if resolved.start is not None:
        # Дата отъезда понятна, длительность — нет: дата сохраняется, вопрос — только о ночах.
        values["date_from"] = resolved.start
        values["date_to"] = None
        span = nights_phrase or date_phrase
    else:
        span = date_phrase
    report.questions.append(
        _question(
            "date_from",
            resolved.question or "Уточните даты поездки: отъезд и возвращение.",
            span,
            options=_date_options(resolved.windows),
        )
    )


def _apply_pending_nights(phrase: str | None, *, values: dict[str, Any], report: DeltaReport) -> None:
    """Длительность из прошлого (или текущего) хода применяется, как только известна дата отъезда."""
    report.pending_nights = None
    if not phrase:
        return
    nights, _status, _rule = calendar.nights_value(phrase)
    if not nights:
        return
    if values.get("date_from") is None:
        if any(q.ambiguity_id == DATES_PERIOD_PENDING_ID for q in report.questions):
            return  # длительность уже учтена в вопросе о периоде и хранится вместе с ним
        report.pending_nights = phrase
        report.questions = [
            q
            for q in report.questions
            if q.field not in ("date_from", "date_to") or q.ambiguity_id == DATES_TZ_PENDING_ID
        ]
        report.questions.append(_nights_pending_question(phrase))
        return
    if values.get("date_to") is None:
        values["date_to"] = values["date_from"] + timedelta(days=nights)
        report.questions = [q for q in report.questions if q.field not in ("date_from", "date_to")]
        report.accepted.append("nights")


def _apply_nights_only(
    phrase: str, *, text: str, values: dict[str, Any], report: DeltaReport, bare_count: bool = False
) -> None:
    if not span_in_text(phrase, text):
        report.rejected.append("nights")
        return
    nights, status, _rule = calendar.nights_value(phrase, bare_count=bare_count)
    if nights is None:
        report.questions.append(_question("date_to", f"Сколько ночей длится поездка? («{phrase}»)", phrase))
        return
    values["date_to"] = values["date_from"] + timedelta(days=nights)
    report.accepted.append("nights")


def _validated_form(values: dict[str, Any], base: StructuredForm, report: DeltaReport) -> StructuredForm:
    try:
        return StructuredForm.model_validate(values)
    except ValidationError as exc:
        # Несовместимая комбинация (например, бюджет без валюты): прежние условия сохраняются.
        report.notes.append(f"Изменения не применены: {exc.errors()[0].get('msg', 'ошибка проверки')}")
        return base


# ---------------------------------------------------------------- исход интерпретации


def outcome_from_delta(
    delta: InterpretationDelta,
    *,
    text: str,
    base: StructuredForm,
    session_id: str,
    request_id: str,
    reference_datetime: datetime,
    mode: Literal["free_text_llm", "free_text_replay"],
    previous_constraints: list[Constraint] | None = None,
    timezone_for: Callable[[str], str | None] | None = None,
    previous_ambiguities: list[Ambiguity] | None = None,
    previous_date_expressions: list[DateExpression] | None = None,
) -> InterpretationOutcome:
    """Полный путь: проверка фрагментов → календарь → форма → ``TripRequest`` → вопросы UC-02."""
    previous_spans = {
        constraint.field: (constraint.source_kind, constraint.source_span)
        for constraint in previous_constraints or []
    }
    form, report = apply_delta(
        delta,
        text=text,
        base=base,
        reference_datetime=reference_datetime,
        timezone_for=timezone_for,
        previous_ambiguities=previous_ambiguities,
        previous_date_expressions=previous_date_expressions,
    )
    outcome = interpret_structured_form(
        form,
        session_id=session_id,
        request_id=request_id,
        reference_datetime=reference_datetime,
        demo_places=timezone_for is None,
    )
    assert outcome.request is not None
    # Вопросы календаря заменяют общий вопрос о датах (точнее и со ссылкой на фразу).
    date_fields = {"date_from", "date_to"}
    calendar_dates = bool({question.field for question in report.questions} & date_fields)
    questions = [
        question for question in outcome.questions if not (calendar_dates and question.field in date_fields)
    ]
    questions = [*report.questions, *questions]
    groups = [
        ConstraintGroup(
            group_id=f"g-unparsed-{index}",
            operator="unparsed",
            children=[],
            scope="trip",
            activation=None,
            raw_text=fragment,
        )
        for index, fragment in enumerate(report.unparsed, start=1)
    ]
    constraints = []
    for constraint in outcome.request.constraints:
        span = _span_for(delta, constraint.field) or report.spans.get(constraint.field)
        if span is not None and constraint.field in _fields_from_delta(report):
            constraints.append(constraint.evolve(source_kind="text_span", source_span=span))
        elif constraint.field in previous_spans:
            kind, previous_span = previous_spans[constraint.field]
            constraints.append(constraint.evolve(source_kind=kind, source_span=previous_span))
        else:
            constraints.append(constraint)
    request: TripRequest = outcome.request.evolve(
        raw_text=text,
        input_mode=mode,
        constraints=constraints,
        constraint_groups=[*outcome.request.constraint_groups, *groups],
        ambiguities=list(questions),
        date_expressions=[*outcome.request.date_expressions, *report.date_expressions],
    )
    notes = list(report.notes)
    if report.unparsed:
        notes.append(
            "Не удалось однозначно учесть: " + "; ".join(f"«{fragment}»" for fragment in report.unparsed)
        )
    return InterpretationOutcome(
        request=request,
        mode=mode,
        questions=questions,
        # Фраза даты понята (ждёт длительности, возвращения или выбора варианта): «даты не указаны» неверно.
        notes=[
            *notes,
            *(note for note in outcome.notes if not (calendar_dates and note.startswith("даты не указаны"))),
        ],
        decisions=outcome.decisions,
    )


_CONSTRAINT_SPAN_FIELD = {
    "budget_total": "budget",
    "transport_mode": "transport",
    "pets": "pets",
    "hotel_breakfast": "breakfast",
    "hotel_stars_min": "stars_min",
    "hotel_free_cancellation": "free_cancellation",
    "hotel_room_type": "room_type",
}


def _fields_from_delta(report: DeltaReport) -> set[str]:
    """Поля условий, принятые из текущего сообщения."""
    reverse = {value: key for key, value in _CONSTRAINT_SPAN_FIELD.items()}
    return {reverse[name] for name in report.accepted if name in reverse}


def _span_for(delta: InterpretationDelta, constraint_field: str) -> str | None:
    """Фрагмент сообщения, на котором основано условие (UC-01: доказуемая связь с текстом)."""
    name = _CONSTRAINT_SPAN_FIELD.get(constraint_field)
    item = getattr(delta, name, None) if name else None
    return item.span if isinstance(item, Spanned) else None


def parse_delta(payload: Any) -> InterpretationDelta | None:
    try:
        return InterpretationDelta.model_validate(payload)
    except ValidationError:
        return None


def summarize_context(form: StructuredForm) -> dict[str, Any]:
    """Текущие условия поездки для LLM (только поля поездки, без служебных данных)."""
    data = form.model_dump(mode="json", exclude_defaults=True)
    return {key: value for key, value in data.items() if value not in (None, [], False)}


_AWAITING_TITLES = {
    "origin": "город отправления",
    "destination": "направление",
    "date_from": "дата отъезда",
    "date_to": "дата возвращения или число ночей",
    "adults": "число взрослых",
    "children_ages": "возраст детей",
    "return_trip": "нужен ли обратный путь",
    "budget": "бюджет",
}


def awaiting_fields(ambiguities: list[Ambiguity]) -> list[str]:
    """Названия полей, по которым задан вопрос (без текста вопросов и цитат — минимизация данных)."""
    titles = [_AWAITING_TITLES.get(item.field, item.field) for item in ambiguities]
    return list(dict.fromkeys(titles))[:6]
