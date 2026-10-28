"""Контракты запроса и семантики условий (ТЗ §4, §7: TripRequest, Constraint, ConstraintGroup,
FallbackActivation, ConstraintEvaluation)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from odin.contracts.common import (
    Contract,
    Currency,
    Deviation,
    EvaluationStatus,
    Money,
    SourceReference,
    TimeZoneName,
    UnknownReason,
    VersionedContract,
)

# Закрытый словарь условий P0 (DEC-30). Срез S1 — базовые поля; расширение — S3.
ConstraintField = Literal[
    "budget_total",
    "transport_mode",
    "pets",
    "hotel_breakfast",
    "hotel_stars_min",
    "hotel_free_cancellation",
    # INT-007: тип номера — только мягкое пожелание; источник его не подтверждает.
    "hotel_room_type",
]
ConstraintOperator = Literal["eq", "ne", "lte", "gte", "in", "not_in"]
ConstraintScope = Literal["trip", "hotel", "transport", "transfer"]

_FIELD_RULES: dict[str, tuple[set[str], set[str]]] = {
    # поле: (допустимые операторы, допустимые области)
    "budget_total": ({"lte"}, {"trip"}),
    "transport_mode": ({"in", "not_in"}, {"transport"}),
    "pets": ({"eq"}, {"trip"}),
    "hotel_breakfast": ({"eq"}, {"hotel"}),
    "hotel_stars_min": ({"gte"}, {"hotel"}),
    "hotel_free_cancellation": ({"eq"}, {"hotel"}),
    "hotel_room_type": ({"eq"}, {"hotel"}),
}


class Place(Contract):
    name: str = Field(min_length=1, max_length=120)
    country_code: str | None = Field(pattern=r"^[A-Z]{2}$")
    timezone: TimeZoneName | None
    lat: float | None = Field(ge=-90, le=90)
    lon: float | None = Field(ge=-180, le=180)
    source: SourceReference | None


class PetInfo(Contract):
    kind: Literal["dog", "cat", "other"]
    count: int = Field(ge=1, le=5)


class Party(Contract):
    """Состав группы. Неизвестное — null, а не выдуманное значение (UC-03)."""

    adults: int | None = Field(ge=1, le=9)
    children_ages: list[int] = Field(default_factory=list)
    pets: list[PetInfo] = Field(default_factory=list)
    rooms: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def _ages(self) -> Party:
        if any(a < 0 or a > 17 for a in self.children_ages):
            raise ValueError("возраст ребёнка должен быть от 0 до 17")
        return self


class DateRange(Contract):
    start: date
    end: date
    timezone: TimeZoneName | None

    @model_validator(mode="after")
    def _order(self) -> DateRange:
        if self.end <= self.start:
            raise ValueError("дата окончания должна быть позже даты начала")
        return self


class Budget(Contract):
    amount: Money
    currency: Currency
    scope: Literal["trip_total"]


class DateExpression(Contract):
    """Выражение даты (INTERPRET-02): разрешается детерминированным календарём."""

    raw_text: str
    reference_datetime: AwareDatetime
    timezone: TimeZoneName | None
    rule: str
    status: Literal["resolved", "ambiguous", "unresolved"]
    resolved_start: date | None
    resolved_end: date | None


class Assumption(Contract):
    """Только показанные предварительные допущения о составе группы или числе ночей (UC-03)."""

    assumption_id: str
    field: Literal["party", "nights"]
    value: Any
    reason: str
    shown_with_price: bool
    authorized_by: str | None


class AmbiguityOption(Contract):
    """Вариант ответа на уточнение, выбираемый нажатием (DATE-003): предложение дат, а не бронь."""

    option_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def _order(self) -> AmbiguityOption:
        if self.date_to <= self.date_from:
            raise ValueError("дата возвращения варианта должна быть позже даты отъезда")
        return self


class Ambiguity(Contract):
    ambiguity_id: str
    field: str
    question: str
    blocking_class: Literal["blocking_now", "assumable_only_if_authorized", "optional"]
    source_span: str | None
    options: list[AmbiguityOption] = Field(default_factory=list, max_length=4)


class Constraint(VersionedContract):
    """Пользовательское условие с доказуемой связью с фрагментом сообщения (UC-01, INTERPRET-01)."""

    constraint_id: str
    field: ConstraintField
    operator: ConstraintOperator
    value: Any
    requiredness: Literal["required", "preferred"]
    source_span: str = Field(min_length=1, max_length=500)
    source_kind: Literal["text_span", "form_field"]
    scope: ConstraintScope
    confidence: float | None = Field(ge=0, le=1)
    ambiguity: str | None

    @model_validator(mode="after")
    def _vocabulary(self) -> Constraint:
        ops, scopes = _FIELD_RULES[self.field]
        if self.operator not in ops:
            raise ValueError(f"оператор {self.operator} недопустим для поля {self.field}")
        if self.scope not in scopes:
            raise ValueError(f"область {self.scope} недопустима для поля {self.field}")
        return self


class FallbackActivation(VersionedContract):
    """Активация резервной группы (INTERPRET-01, FALLBACK-01). Статусы — DEC-30."""

    group_id: str
    trigger: str
    evidence: list[str] = Field(default_factory=list)
    search_completeness: Literal["complete", "incomplete", "unknown"]
    status: Literal["activated", "not_activated", "undetermined"]

    @model_validator(mode="after")
    def _rules(self) -> FallbackActivation:
        if self.status == "activated" and self.search_completeness != "complete":
            raise ValueError(
                "FALLBACK активируется только при доказанном условии и завершённом поиске (FALLBACK-01)"
            )
        return self


class ConstraintGroup(VersionedContract):
    """Группа условий: AND, OR, FALLBACK не взаимозаменяемы; unparsed не теряется (INTERPRET-01)."""

    group_id: str
    operator: Literal["AND", "OR", "FALLBACK", "unparsed"]
    children: list[str]
    scope: ConstraintScope
    activation: FallbackActivation | None
    raw_text: str | None

    @model_validator(mode="after")
    def _rules(self) -> ConstraintGroup:
        if self.operator == "FALLBACK":
            if len(self.children) != 2:
                raise ValueError("FALLBACK: ровно две ветви — основная и резервная")
            if self.activation is None:
                raise ValueError("FALLBACK требует описания условия активации")
        if self.operator == "unparsed" and not self.raw_text:
            raise ValueError("unparsed-группа должна сохранять исходный фрагмент")
        if self.operator in ("AND", "OR") and len(self.children) < 2:
            raise ValueError("AND/OR требуют минимум двух операндов")
        return self


class ConstraintEvaluation(VersionedContract):
    """Оценка условия для кандидата: unknown не равно matched (CONSTRAINT-01)."""

    constraint_id: str
    component_id: str | None
    plan_id: str | None
    status: EvaluationStatus
    unknown_reason: UnknownReason | None
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str
    deviation: Deviation | None

    @model_validator(mode="after")
    def _rules(self) -> ConstraintEvaluation:
        if self.status == "unknown" and self.unknown_reason is None:
            raise ValueError("unknown требует unknown_reason")
        if self.status != "unknown" and self.unknown_reason is not None:
            raise ValueError("unknown_reason допустим только для unknown")
        return self


class TripRequest(VersionedContract):
    """Запрос поездки после интерпретации (ТЗ §7)."""

    session_id: str
    request_id: str
    raw_text: str | None
    input_mode: Literal["structured_form", "free_text_llm", "free_text_replay"]
    reference_datetime: AwareDatetime
    origin: Place | None
    destination: Place | None
    date_range: DateRange | None
    party: Party
    budget: Budget | None
    return_trip: bool | None
    wishes_tags: list[str] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    constraint_groups: list[ConstraintGroup] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    date_expressions: list[DateExpression] = Field(default_factory=list)
