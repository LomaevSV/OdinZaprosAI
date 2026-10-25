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
from odin.contracts.trip import Ambiguity, Constraint, ConstraintGroup, DateExpression, PetInfo
from odin.core import calendar
from odin.llm.interpreter import InterpretationOutcome, interpret_structured_form
from odin.tools.mock_data import DEMO_CATALOG, normalize_city

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
        if amount <= 0 or amount > Decimal("100000000"):
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


# Уточнения, которые переносятся между ходами через ``TripRequest.ambiguities`` (UC-02: полученное не теряется).
NIGHTS_PENDING_ID = "amb-llm-nights"
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
) -> Ambiguity:
    return Ambiguity(
        ambiguity_id=ambiguity_id or f"amb-llm-{field_name}",
        field=field_name,
        question=text,
        blocking_class=blocking,  # type: ignore[arg-type]
        source_span=span,
    )


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


def pending_from(ambiguities: list[Ambiguity] | None) -> tuple[str | None, Ambiguity | None]:
    """Незакрытые уточнения прошлого хода: (фраза длительности, вопрос о возрасте детей)."""
    nights, children = None, None
    for item in ambiguities or []:
        if item.ambiguity_id == NIGHTS_PENDING_ID and item.source_span:
            nights = item.source_span
        elif item.ambiguity_id == CHILDREN_PENDING_ID:
            children = item
    return nights, children


def apply_delta(
    delta: InterpretationDelta,
    *,
    text: str,
    base: StructuredForm,
    reference_datetime: datetime,
    timezone_for: Callable[[str], str | None] | None = None,
    previous_ambiguities: list[Ambiguity] | None = None,
) -> tuple[StructuredForm, DeltaReport]:
    """Накладывает проверенную дельту на прежние условия. Непроверенное значение не применяется."""
    report = DeltaReport()
    values: dict[str, Any] = base.model_dump()
    pending_nights, pending_children = pending_from(previous_ambiguities)

    def check(name: str, item: Spanned | None) -> bool:
        if item is None:
            return False
        if span_in_text(item.span, text):
            report.accepted.append(name)
            return True
        report.rejected.append(name)
        report.notes.append(
            f"Значение «{FIELD_TITLES.get(name, name)}» не принято: фрагмент не найден в сообщении"
        )
        return False

    # Отмена условий («бюджет не важен», «можно и самолётом»): только с дословным фрагментом.
    for item in delta.clear:
        if not span_in_text(item.span, text):
            report.rejected.append(f"clear:{item.field}")
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
            values["transport_modes_required"] = bool(delta.transport.required or delta.transport.exclude)
    if check("budget", delta.budget):
        assert delta.budget is not None
        values.update(
            budget_amount=delta.budget.amount,
            budget_currency=delta.budget.currency,
            budget_required=delta.budget.required,
        )
    if check("pets", delta.pets):
        assert delta.pets is not None
        values["pets"] = [pet.model_dump() for pet in delta.pets.value]
        values["pets_required"] = delta.pets.required
    if check("breakfast", delta.breakfast):
        assert delta.breakfast is not None
        values.update(breakfast=delta.breakfast.value, breakfast_required=delta.breakfast.required)
    if check("stars_min", delta.stars_min):
        assert delta.stars_min is not None
        values.update(stars_min=delta.stars_min.value, stars_required=delta.stars_min.required)
    if check("free_cancellation", delta.free_cancellation):
        assert delta.free_cancellation is not None
        values.update(
            free_cancellation=delta.free_cancellation.value,
            free_cancellation_required=delta.free_cancellation.required,
        )
    if check("wishes_tags", delta.wishes_tags):
        assert delta.wishes_tags is not None
        known = [tag for tag in (t.strip().lower() for t in delta.wishes_tags.value) if tag in WISH_TAGS]
        unknown = [tag for tag in delta.wishes_tags.value if tag.strip().lower() not in WISH_TAGS]
        values["wishes_tags"] = known[:6]
        if unknown:
            report.unparsed.append(delta.wishes_tags.span)

    _apply_dates(
        delta.dates,
        text=text,
        values=values,
        reference=reference_datetime,
        report=report,
        resolver=timezone_for,
    )
    _apply_pending_nights(report.pending_nights or pending_nights, values=values, report=report)

    for fragment in delta.unparsed:
        if fragment.strip()[:300] == children_span:
            continue  # фрагмент о детях стал вопросом о возрасте
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
    """Часовой пояс для календаря. В Live — только геокодинг через оркестратор (без демо-справочника,
    решение 25.10: нет незаметной подмены Live на Mock); в Mock — демо-справочник."""
    if resolver is not None:
        for key in ("origin", "destination"):
            name = values.get(key)
            if name:
                zone = resolver(str(name))
                if zone:
                    return zone
        return None
    for key in ("origin", "destination"):
        place = normalize_city(values.get(key))
        if place is not None:
            return place.timezone
    return None


def _apply_dates(
    dates: DatesValue | None,
    *,
    text: str,
    values: dict[str, Any],
    reference: datetime,
    report: DeltaReport,
    resolver: Callable[[str], str | None] | None = None,
) -> None:
    if dates is None or not dates.date_phrase:
        if dates is not None and dates.nights_phrase:
            if values.get("date_from") is not None:
                # «давай на 4 ночи» при уже известной дате отъезда
                _apply_nights_only(dates.nights_phrase, text=text, values=values, report=report)
            elif span_in_text(dates.nights_phrase, text) and calendar.nights_value(dates.nights_phrase)[0]:
                # «на 3 ночи» без даты отъезда: длительность сохраняется до появления даты (UC-02)
                report.pending_nights = dates.nights_phrase.strip()
            else:
                report.rejected.append("nights")
        return
    if not span_in_text(dates.date_phrase, text):
        report.rejected.append("dates")
        report.questions.append(_question("date_from", "Уточните даты поездки: отъезд и возвращение.", None))
        return
    nights_phrase = dates.nights_phrase
    if nights_phrase and not span_in_text(nights_phrase, text):
        report.rejected.append("nights")
        nights_phrase = None
    resolved = calendar.resolve_trip_dates(
        dates.date_phrase, nights_phrase, reference, _timezone_for(values, resolver)
    )
    report.date_expressions.extend(resolved.expressions)
    if resolved.status == "resolved":
        report.accepted.append("dates")
        values["date_from"] = resolved.start
        values["date_to"] = resolved.end
        return
    if resolved.status == "partial":
        report.accepted.append("dates")
        values["date_from"] = resolved.start
        values["date_to"] = None
        return
    if resolved.start is not None:
        # Дата отъезда понятна, длительность — нет: дата сохраняется, вопрос — только о ночах.
        values["date_from"] = resolved.start
        values["date_to"] = None
        span = nights_phrase or dates.date_phrase
    else:
        span = dates.date_phrase
    report.questions.append(
        _question(
            "date_from",
            resolved.question or "Уточните даты поездки: отъезд и возвращение.",
            span,
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
        report.pending_nights = phrase
        report.questions = [q for q in report.questions if q.field not in ("date_from", "date_to")]
        report.questions.append(_nights_pending_question(phrase))
        return
    if values.get("date_to") is None:
        values["date_to"] = values["date_from"] + timedelta(days=nights)
        report.questions = [q for q in report.questions if q.field not in ("date_from", "date_to")]
        report.accepted.append("nights")


def _apply_nights_only(phrase: str, *, text: str, values: dict[str, Any], report: DeltaReport) -> None:
    if not span_in_text(phrase, text):
        report.rejected.append("nights")
        return
    nights, status, _rule = calendar.nights_value(phrase)
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
        span = _span_for(delta, constraint.field)
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
        notes=[*notes, *outcome.notes],
        decisions=outcome.decisions,
    )


_CONSTRAINT_SPAN_FIELD = {
    "budget_total": "budget",
    "transport_mode": "transport",
    "pets": "pets",
    "hotel_breakfast": "breakfast",
    "hotel_stars_min": "stars_min",
    "hotel_free_cancellation": "free_cancellation",
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
