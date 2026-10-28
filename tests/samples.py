"""Синтетические образцы контрактов для контрактных и модульных тестов.

Все данные — Mock (DATA-MODE-02): реальных ответов провайдеров здесь нет (DEC-24).
Каждый вызов возвращает новый объект; образцы проходят все валидаторы моделей.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from odin.contracts import (
    AgentResult,
    Availability,
    CandidateDestination,
    Cart,
    CartItem,
    ConnectionCheck,
    Constraint,
    ConstraintEvaluation,
    ConstraintGroup,
    FallbackActivation,
    Fee,
    Hotel,
    HotelOffer,
    Party,
    Place,
    PlanComponent,
    Price,
    PricePeriod,
    ReplanEvent,
    ReviewEvidence,
    SearchTermination,
    SourceReference,
    ToolRequest,
    ToolResult,
    TravelOffer,
    TripPlan,
    TripRequest,
    TripState,
    Validity,
)
from odin.contracts.offers import HotelLocation, HotelTerms, Occupancy, Rating
from odin.contracts.plan import HistoryEntry
from odin.contracts.trip import Budget, DateRange

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
NAIVE = datetime(2026, 10, 24, 12, 0)
CHECK_IN = date(2026, 11, 6)
CHECK_OUT = date(2026, 11, 8)


def source_reference(**overrides: Any) -> SourceReference:
    data: dict[str, Any] = {
        "source_type": "mock_generator",
        "provider": "odin_mock",
        "tool": "search_hotels",
        "url": None,
        "retrieved_at": None,
        "generated_at": NOW,
        "published_at": None,
        "external_id": "DEMO-H-001",
        "verification_status": "unverified",
        "data_mode": "mock",
        "limitations": ["ДЕМО-данные"],
    }
    data.update(overrides)
    return SourceReference(**data)


def price(**overrides: Any) -> Price:
    data: dict[str, Any] = {
        "amount": Decimal("27000.00"),
        "currency": "RUB",
        "price_scope": "stay_total",
        "guests": 2,
        "rooms": 1,
        "period": PricePeriod(start=CHECK_IN, end=CHECK_OUT),
        "inclusions": ["проживание"],
        "known_fees": [Fee(name="туристический налог", amount=None, currency="RUB", included=None)],
        "mandatory_fees_status": "unknown",
        "total_completeness": "partial",
        "unknown_reason": "price_incomplete",
        "source": source_reference(),
        "as_of": NOW,
    }
    data.update(overrides)
    return Price(**data)


def availability(**overrides: Any) -> Availability:
    data: dict[str, Any] = {
        "subject": "hotel",
        "availability_status": "unknown",
        "evidence_level": "mock",
        "verification_status": "unverified",
        "data_mode": "mock",
        "checked_at": None,
        "parameters": {"check_in": "2026-11-06", "check_out": "2026-11-08", "adults": 2},
        "unknown_reason": "mock_data",
    }
    data.update(overrides)
    return Availability(**data)


def validity() -> Validity:
    return Validity(observed_at=NOW, valid_until=NOW + timedelta(minutes=30), basis="provisional_ttl")


def place(name: str = "Казань") -> Place:
    return Place(name=name, country_code="RU", timezone="Europe/Moscow", lat=55.79, lon=49.12, source=None)


def travel_offer() -> TravelOffer:
    return TravelOffer(
        offer_id="DEMO-T-001",
        kind="rail",
        direction="outbound",
        title="ДЕМО-поезд Москва — Казань",
        parameters={"adults": 2},
        departure_place=place("Москва"),
        arrival_place=place("Казань"),
        departure_at=datetime(2026, 11, 5, 22, 0, tzinfo=MSK),
        arrival_at=datetime(2026, 11, 6, 10, 0, tzinfo=MSK),
        duration_minutes=720,
        duration_basis="estimate_mock",
        carrier=None,
        pets_allowed=None,
        price=price(price_scope="party_total", period=None, rooms=None, known_fees=[], inclusions=[]),
        source=source_reference(tool="search_transport", external_id="DEMO-T-001"),
        availability=availability(subject="transport", parameters={"adults": 2}),
        validity=validity(),
    )


def hotel() -> Hotel:
    return Hotel(
        hotel_id="DEMO-H-001",
        source_hotel_id="mock-1",
        name="ДЕМО-отель «Тест»",
        location=HotelLocation(city="Казань", address=None, timezone="Europe/Moscow"),
        geo={"lat": 55.79, "lon": 49.12},
        amenities=["wifi"],
        rating=Rating(value=Decimal("8.7"), scale=Decimal("10"), count=None),
        reviews_summary=None,
        official_website=None,
        phone=None,
        star_rating=4,
        pets_allowed=None,
        check_in_from=time(14, 0),
        check_out_until=time(12, 0),
        source=source_reference(),
    )


def hotel_offer() -> HotelOffer:
    return HotelOffer(
        offer_id="DEMO-HO-001",
        hotel_id="DEMO-H-001",
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        occupancy=Occupancy(adults=2, children_ages=[], rooms=1),
        room_type=None,
        price_total=price(),
        currency="RUB",
        price_scope="stay_total",
        availability=availability(),
        source=source_reference(),
        terms=HotelTerms(
            breakfast_included=None, meals=None, free_cancellation=None, free_cancellation_until=None
        ),
        validity=validity(),
    )


def review_evidence() -> ReviewEvidence:
    return ReviewEvidence(
        hotel_id="DEMO-H-001",
        source=source_reference(tool="get_reviews"),
        review_rating=Decimal("8.7"),
        rating_scale=Decimal("10"),
        review_count=None,
        observed_at=NOW,
        review_summary=None,
    )


def constraint() -> Constraint:
    return Constraint(
        constraint_id="c-breakfast",
        field="hotel_breakfast",
        operator="eq",
        value=True,
        requiredness="preferred",
        source_span="с завтраком",
        source_kind="form_field",
        scope="hotel",
        confidence=None,
        ambiguity=None,
    )


def fallback_activation() -> FallbackActivation:
    return FallbackActivation(
        group_id="g-fallback",
        trigger="нет 4★ с завтраком",
        search_completeness="unknown",
        status="undetermined",
    )


def constraint_group() -> ConstraintGroup:
    return ConstraintGroup(
        group_id="g-fallback",
        operator="FALLBACK",
        children=["c-stars", "c-breakfast"],
        scope="hotel",
        activation=fallback_activation(),
        raw_text="если нет 4★ — 3★ с завтраком",
    )


def constraint_evaluation() -> ConstraintEvaluation:
    return ConstraintEvaluation(
        constraint_id="c-breakfast",
        component_id="hotel",
        plan_id="DEMO-P-001",
        status="unknown",
        unknown_reason="mock_data",
        reason="Mock-данные не подтверждают завтрак",
        deviation=None,
    )


def trip_request() -> TripRequest:
    return TripRequest(
        session_id="s-1",
        request_id="req-00000001",
        raw_text=None,
        input_mode="structured_form",
        reference_datetime=NOW,
        origin=place("Москва"),
        destination=place("Казань"),
        date_range=DateRange(start=CHECK_IN, end=CHECK_OUT, timezone="Europe/Moscow"),
        party=Party(adults=2, children_ages=[], pets=[], rooms=None),
        budget=Budget(amount=Decimal("30000.00"), currency="RUB", scope="trip_total"),
        return_trip=None,
        constraints=[constraint()],
    )


def connection_check() -> ConnectionCheck:
    return ConnectionCheck(
        check_id="cc-1",
        from_component="outbound",
        to_component="hotel",
        status="unknown",
        basis="прибытие 10:00, заезд с 14:00",
        times_used={"arrival_at": "2026-11-06T10:00:00+03:00", "check_in_from": "14:00"},
        timezones=["Europe/Moscow"],
        buffer_minutes=None,
        buffer_status="proposed_not_approved",
        margin_minutes=None,
        unknown_reason="buffer_not_approved",
    )


def trip_plan() -> TripPlan:
    return TripPlan(
        plan_id="DEMO-P-001",
        trip_revision=1,
        components=[
            PlanComponent(
                component_id="hotel",
                role="hotel",
                offer_id="DEMO-HO-001",
                kind="hotel",
                title="ДЕМО-отель «Тест»",
                data_mode="mock",
            )
        ],
        total_price=price(price_scope="trip_total"),
        completeness="partial",
        connection_checks=[connection_check()],
        constraint_evaluations=[constraint_evaluation()],
        unknowns=["стоимость трансфера"],
        sources=[source_reference()],
        rank_class=2,
        trip_data_mode="mock",
        data_mode_breakdown={"hotel": "mock"},
        fully_agreed=False,
        relevance_note=None,
    )


def cart() -> Cart:
    return Cart(
        cart_id="DEMO-C-001",
        trip_id="t-1",
        trip_revision=1,
        items=[
            CartItem(
                item_id="i-1",
                component_id="hotel",
                role="hotel",
                kind="hotel",
                title="ДЕМО-отель «Тест»",
                data_mode="mock",
                price=price(),
                availability_status="unknown",
                informational=False,
                note=None,
                source=source_reference(),
            )
        ],
        total_price=price(price_scope="trip_total"),
        completeness="partial",
        status="awaiting_confirmation",
        confirmed_at=None,
        demo_receipt_id=None,
        risk_acknowledged_at=None,
        confirmation_id=None,
        significant_unknowns=["доступность номера не подтверждена"],
        created_at=NOW,
    )


def replan_event() -> ReplanEvent:
    return ReplanEvent(
        request_id="req-00000002",
        old_revision=1,
        change_set={"adults": 3},
        invalidated_components=["hotel"],
        reused_components=["outbound"],
        new_revision=2,
        validation_result="ok",
    )


def tool_request() -> ToolRequest:
    return ToolRequest(
        request_id="req-00000001",
        call_id="call-1",
        tool_name="search_hotels",
        arguments={"city": "Казань", "adults": 2},
        deadline=NOW + timedelta(seconds=20),
        policy_scope="read_only",
    )


def tool_result() -> ToolResult:
    return ToolResult(
        call_id="call-1",
        tool_name="search_hotels",
        provider="odin_mock",
        status="ok",
        data={"hotels": ["DEMO-H-001"]},
        sources=[source_reference()],
        retrieved_at=NOW,
        error_code=None,
        latency_ms=12,
    )


def agent_result() -> AgentResult:
    return AgentResult(
        agent="hotels",
        status="partial",
        structured_output={"offers": ["DEMO-HO-001"]},
        requested_tools=[tool_request()],
        evidence_refs=["DEMO-H-001"],
        uncertainties=["доступность номера unknown"],
    )


def candidate_destination() -> CandidateDestination:
    return CandidateDestination(
        candidate_id="cd-1",
        place=place("Казань"),
        tags=["город", "история"],
        matched_tags=["город"],
        description="ДЕМО-описание направления",
        climate_status="not_loaded",
        climate_note=None,
        estimated_cost=None,
        cost_note="стоимость требует дат",
        visa_status="not_verified",
        source=source_reference(source_type="editorial", provider="odin_demo_catalog", tool=None),
    )


def trip_state() -> TripState:
    return TripState(
        trip_id="t-1",
        session_id="s-1",
        revision=1,
        request=trip_request(),
        candidate_destinations=[candidate_destination()],
        transport_offers=[travel_offer()],
        hotels=[hotel()],
        hotel_offers=[hotel_offer()],
        selected_components={"hotel": "DEMO-HO-001"},
        constraint_evaluations=[constraint_evaluation()],
        warnings=["ДЕМО-данные"],
        cart=cart(),
        status="cart",
        history=[HistoryEntry(revision=1, request_id="req-00000001", event="planned", at=NOW)],
        plan=trip_plan(),
        search_termination=SearchTermination(
            reason="sources_exhausted",
            checked_sources=["odin_mock"],
            unchecked_sources=[],
            candidates_seen=1,
            confirmed_matches=0,
            unknown_candidates=1,
            can_continue=False,
        ),
    )


# Образец на каждый из 19 нормативных типов (ключи совпадают с NORMATIVE_MODELS).
NORMATIVE_SAMPLES: dict[str, Callable[[], BaseModel]] = {
    "ToolRequest": tool_request,
    "ToolResult": tool_result,
    "AgentResult": agent_result,
    "TripRequest": trip_request,
    "TripState": trip_state,
    "TravelOffer": travel_offer,
    "TripPlan": trip_plan,
    "Cart": cart,
    "ReplanEvent": replan_event,
    "SourceReference": source_reference,
    "Constraint": constraint,
    "ConstraintGroup": constraint_group,
    "FallbackActivation": fallback_activation,
    "ConstraintEvaluation": constraint_evaluation,
    "Hotel": hotel,
    "HotelOffer": hotel_offer,
    "ReviewEvidence": review_evidence,
    "Price": price,
    "Availability": availability,
}
