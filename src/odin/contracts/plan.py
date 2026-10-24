"""Контракты плана, корзины, пересборки и состояния поездки (ТЗ §7: TripPlan, Cart, ReplanEvent, TripState)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from odin.contracts.common import (
    DEMO_PURCHASE_NOTICE,
    AvailabilityStatus,
    Completeness,
    Contract,
    DataMode,
    EvaluationStatus,
    Price,
    SourceReference,
    UnknownReason,
    VersionedContract,
)
from odin.contracts.offers import CandidateDestination, Hotel, HotelOffer, TravelOffer
from odin.contracts.tools import SearchTermination
from odin.contracts.trip import ConstraintEvaluation, TripRequest

ComponentRole = Literal["outbound", "return", "hotel", "transfer_in", "transfer_out"]


class ConnectionCheck(Contract):
    """Проверка стыка (CONNECTION-02): статус, основание, времена и пояса, буфер, источник."""

    check_id: str
    from_component: str
    to_component: str
    status: EvaluationStatus
    basis: str
    times_used: dict[str, str]
    timezones: list[str]
    buffer_minutes: int | None
    buffer_status: Literal["approved", "proposed_not_approved", "none"]
    margin_minutes: int | None
    sources: list[str] = Field(default_factory=list)
    unknown_reason: UnknownReason | None

    @model_validator(mode="after")
    def _rules(self) -> ConnectionCheck:
        if self.status == "unknown" and self.unknown_reason is None:
            raise ValueError("unknown требует unknown_reason")
        if self.status == "matched" and self.buffer_status != "approved":
            raise ValueError("matched по стыку требует утверждённого буфера (DEC-20)")
        return self


class PlanComponent(Contract):
    component_id: str
    role: ComponentRole
    offer_id: str
    kind: str
    title: str
    data_mode: DataMode


class TripPlan(VersionedContract):
    plan_id: str
    trip_revision: int = Field(ge=0)
    components: list[PlanComponent]
    total_price: Price
    completeness: Completeness
    connection_checks: list[ConnectionCheck] = Field(default_factory=list)
    constraint_evaluations: list[ConstraintEvaluation] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    rank_class: Literal[1, 2, 3]
    trip_data_mode: Literal["live", "replay", "mock", "mixed"]
    data_mode_breakdown: dict[str, DataMode] = Field(default_factory=dict)
    fully_agreed: bool
    relevance_note: str | None

    @model_validator(mode="after")
    def _rules(self) -> TripPlan:
        if self.fully_agreed:
            if self.completeness != "complete":
                raise ValueError("поездка с неполной ценой не может быть полностью согласована")
            if any(c.status in ("violated", "unknown") for c in self.connection_checks):
                raise ValueError("неподтверждённые или нарушенные стыки исключают «полностью согласовано»")
            if self.rank_class != 1:
                raise ValueError("«полностью согласовано» только для класса 1 (RANK-01)")
        modes = set(self.data_mode_breakdown.values())
        if self.trip_data_mode == "live" and modes != {"live"}:
            raise ValueError("поездку нельзя называть Live при критическом Mock/Replay (DATA-MODE-01)")
        return self


class CartItem(Contract):
    item_id: str
    component_id: str
    role: ComponentRole
    kind: str
    title: str
    data_mode: DataMode
    price: Price
    availability_status: AvailabilityStatus
    informational: bool
    note: str | None
    source: SourceReference


class Cart(VersionedContract):
    """Корзина (UC-07, UC-08). Согласие на покупку и на риски фиксируются раздельно."""

    cart_id: str
    trip_id: str
    trip_revision: int = Field(ge=0)
    items: list[CartItem]
    total_price: Price
    completeness: Completeness
    status: Literal["draft", "awaiting_confirmation", "demo_purchased"]
    confirmed_at: AwareDatetime | None
    demo_receipt_id: str | None
    risk_acknowledged_at: AwareDatetime | None
    confirmation_id: str | None
    significant_unknowns: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
    declined_at: AwareDatetime | None = None
    demo_notice: Literal["Демонстрационная покупка. Реального бронирования и списания средств нет"] = (
        DEMO_PURCHASE_NOTICE
    )

    @model_validator(mode="after")
    def _rules(self) -> Cart:
        if self.status == "demo_purchased":
            if self.confirmed_at is None or self.demo_receipt_id is None or self.confirmation_id is None:
                raise ValueError("demo_purchased требует confirmed_at, confirmation_id и demo_receipt_id")
            if self.significant_unknowns and self.risk_acknowledged_at is None:
                raise ValueError("при существенных неизвестных нужно отдельное согласие с рисками (UC-08)")
        return self


class DemoReceipt(VersionedContract):
    receipt_id: str
    cart_id: str
    trip_id: str
    trip_revision: int
    confirmation_id: str
    created_at: AwareDatetime
    notice: Literal["Демонстрационная покупка. Реального бронирования и списания средств нет"] = (
        DEMO_PURCHASE_NOTICE
    )
    items: list[CartItem]
    total_price: Price
    completeness: Completeness
    purchase_consent_at: AwareDatetime
    risk_acknowledged_at: AwareDatetime | None
    significant_unknowns: list[str]


class ReplanEvent(VersionedContract):
    request_id: str
    old_revision: int
    change_set: dict[str, Any]
    invalidated_components: list[str]
    reused_components: list[str]
    new_revision: int | None
    validation_result: Literal["ok", "failed", "partial"]
    note: str | None = None


class HistoryEntry(Contract):
    revision: int
    request_id: str
    event: str
    at: AwareDatetime
    note: str | None = None


TripStatus = Literal[
    "collecting",
    "awaiting_clarification",
    "searching",
    "planned",
    "agreed",
    "cart",
    "purchased_demo",
    "failed",
]


class TripState(VersionedContract):
    """Состояние поездки. Изменяется атомарно через ревизии (ARCH-05, REPLAN-02)."""

    trip_id: str
    session_id: str
    revision: int = Field(ge=0)
    request: TripRequest | None
    candidate_destinations: list[CandidateDestination] = Field(default_factory=list)
    transport_offers: list[TravelOffer] = Field(default_factory=list)
    hotels: list[Hotel] = Field(default_factory=list)
    hotel_offers: list[HotelOffer] = Field(default_factory=list)
    transfer_options: list[TravelOffer] = Field(default_factory=list)
    selected_components: dict[str, str] = Field(default_factory=dict)
    constraint_evaluations: list[ConstraintEvaluation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cart: Cart | None
    status: TripStatus
    history: list[HistoryEntry] = Field(default_factory=list)
    plan: TripPlan | None
    search_termination: SearchTermination | None
    rankings: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
