"""Интерпретатор S1: детерминированный разбор структурированной формы (DEC-08 вариант «а»).

Свободный текст без LLM честно отклоняется: режим `structured_fallback` нельзя выдавать за диалог
(план §2.22). Допущения не делаются: состав группы обязателен (DEC-25).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from odin.contracts import Party, Place, TripRequest
from odin.contracts.api import StructuredForm
from odin.contracts.trip import Ambiguity, Budget, Constraint, DateRange
from odin.core import clarify
from odin.tools.mock_data import normalize_city


@dataclass(frozen=True, slots=True)
class InterpretationOutcome:
    """Результат интерпретации: черновик `TripRequest`, вопросы политики UC-02 и решения по полям."""

    request: TripRequest | None
    mode: str
    questions: list[Ambiguity] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    free_text_refused: bool = False
    decisions: list[clarify.ClarificationDecision] = field(default_factory=list)


def _question(field_name: str, question: str, blocking_class: str = "blocking_now") -> Ambiguity:
    return Ambiguity(
        ambiguity_id=f"amb-{field_name}",
        field=field_name,
        question=question,
        blocking_class=blocking_class,  # type: ignore[arg-type]
        source_span=None,
    )


def _place(name: str | None) -> Place | None:
    if not name:
        return None
    known = normalize_city(name)
    if known is None:
        # Город вне демо-справочника: честно сохраняем название, координат и пояса не выдумываем.
        return Place(name=name.strip(), country_code=None, timezone=None, lat=None, lon=None, source=None)
    return Place(
        name=known.name,
        country_code=known.country_code,
        timezone=known.timezone,
        lat=known.lat,
        lon=known.lon,
        source=None,
    )


def _constraints(form: StructuredForm) -> list[Constraint]:
    """Флаги формы → условия закрытого словаря (CONSTRAINT-01). Без флагов условий нет."""
    result: list[Constraint] = []

    def add(field_name: str, operator: str, value: object, source: str) -> None:
        result.append(
            Constraint(
                constraint_id=f"c-{field_name}",
                field=field_name,  # type: ignore[arg-type]
                operator=operator,  # type: ignore[arg-type]
                value=value,
                requiredness="required" if getattr(form, f"{source}_required", False) else "preferred",
                source_span=source,
                source_kind="form_field",
                scope={
                    "budget_total": "trip",
                    "transport_mode": "transport",
                    "pets": "trip",
                    "hotel_breakfast": "hotel",
                    "hotel_stars_min": "hotel",
                    "hotel_free_cancellation": "hotel",
                }[field_name],  # type: ignore[arg-type]
                confidence=None,
                ambiguity=None,
            )
        )

    if form.budget_amount is not None:
        add("budget_total", "lte", format(form.budget_amount, "f"), "budget")
    if form.transport_modes:
        add("transport_mode", "in", list(form.transport_modes), "transport_modes")
    if form.pets:
        add("pets", "eq", True, "pets")
    if form.breakfast is not None:
        add("hotel_breakfast", "eq", form.breakfast, "breakfast")
    if form.stars_min is not None:
        add("hotel_stars_min", "gte", form.stars_min, "stars")
    if form.free_cancellation is not None:
        add("hotel_free_cancellation", "eq", form.free_cancellation, "free_cancellation")
    return result


def interpret_structured_form(
    form: StructuredForm,
    *,
    session_id: str,
    request_id: str,
    reference_datetime: datetime | None = None,
) -> InterpretationOutcome:
    """Строит `TripRequest` из формы. Блокирующие вопросы — только то, без чего нельзя искать."""
    reference = reference_datetime or datetime.now(tz=UTC)
    notes: list[str] = []

    origin = _place(form.origin)
    destination = _place(form.destination)
    decisions = clarify.decide(form)
    questions = [
        _question(decision.field, decision.question or "", decision.blocking_class)
        for decision in clarify.questions(decisions)
    ]

    date_range = None
    if form.date_from is not None and form.date_to is not None and form.date_to > form.date_from:
        date_range = DateRange(
            start=form.date_from,
            end=form.date_to,
            timezone=origin.timezone if origin else None,
        )
    elif form.date_from is None and form.date_to is None:
        notes.append("даты не указаны: цена и наличие без дат не запрашиваются (UC-03)")

    pets = [pet for pet in form.pets]
    party = Party(
        adults=form.adults,
        children_ages=list(form.children_ages),
        pets=pets,
        rooms=None,
    )
    budget = None
    if form.budget_amount is not None and form.budget_currency is not None:
        budget = Budget(amount=form.budget_amount, currency=form.budget_currency, scope="trip_total")

    request = TripRequest(
        session_id=session_id,
        request_id=request_id,
        raw_text=None,
        input_mode="structured_form",
        reference_datetime=reference,
        origin=origin,
        destination=destination,
        date_range=date_range,
        party=party,
        budget=budget,
        return_trip=form.return_trip,
        wishes_tags=list(form.wishes_tags),
        constraints=_constraints(form),
        ambiguities=list(questions),
    )
    return InterpretationOutcome(
        request=request,
        mode="structured_fallback",
        questions=questions,
        notes=notes,
        decisions=decisions,
    )


def refuse_free_text(*, session_id: str, request_id: str) -> InterpretationOutcome:
    """Свободный текст без LLM: явный ограниченный режим, а не имитация диалога."""
    return InterpretationOutcome(
        request=None,
        mode="llm_unavailable",
        notes=[
            "Свободный диалог недоступен: LLM отключён (DEC-08). "
            "Заполните форму или включите LLM по решению DEC-07."
        ],
        free_text_refused=True,
    )


def request_to_form(request: TripRequest) -> StructuredForm:
    """Обратное преобразование для пересборки: неизменённые условия сохраняются (REPLAN-01, K-22..K-25)."""
    by_field = {constraint.field: constraint for constraint in request.constraints}

    def required(field_name: str) -> bool:
        constraint = by_field.get(field_name)
        return constraint is not None and constraint.requiredness == "required"

    transport = by_field.get("transport_mode")
    return StructuredForm(
        origin=request.origin.name if request.origin else None,
        destination=request.destination.name if request.destination else None,
        date_from=request.date_range.start if request.date_range else None,
        date_to=request.date_range.end if request.date_range else None,
        adults=request.party.adults,
        children_ages=list(request.party.children_ages),
        return_trip=request.return_trip,
        transport_modes=list(transport.value) if transport and isinstance(transport.value, list) else [],
        transport_modes_required=required("transport_mode"),
        budget_amount=request.budget.amount if request.budget else None,
        budget_currency=request.budget.currency if request.budget else None,
        budget_required=required("budget_total"),
        pets=list(request.party.pets),
        pets_required=required("pets"),
        breakfast=by_field["hotel_breakfast"].value if "hotel_breakfast" in by_field else None,
        breakfast_required=required("hotel_breakfast"),
        stars_min=by_field["hotel_stars_min"].value if "hotel_stars_min" in by_field else None,
        stars_required=required("hotel_stars_min"),
        free_cancellation=(
            by_field["hotel_free_cancellation"].value if "hotel_free_cancellation" in by_field else None
        ),
        free_cancellation_required=required("hotel_free_cancellation"),
        wishes_tags=list(request.wishes_tags),
    )


def date_from_phrase(value: str) -> date | None:
    """Строгий разбор ISO-даты. Относительные фразы — S3 (INTERPRET-02), не выдумываются."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
