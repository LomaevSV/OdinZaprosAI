"""Контракты предложений (ТЗ §7: TravelOffer, Hotel, HotelOffer, ReviewEvidence) и кандидатов направлений."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from odin.contracts.common import (
    Availability,
    Contract,
    Currency,
    HttpUrlStr,
    Price,
    PriceScope,
    SourceReference,
    TimeZoneName,
    Validity,
    VersionedContract,
)
from odin.contracts.trip import Place


class TravelOffer(VersionedContract):
    """Транспорт или трансфер. Гостиничная семантика доступности не применяется (CONTRACT-02)."""

    offer_id: str
    kind: Literal["flight", "rail", "hotel", "transfer", "other"]
    direction: Literal["outbound", "return", "transfer_in", "transfer_out", "other"]
    title: str
    parameters: dict[str, Any]
    departure_place: Place | None
    arrival_place: Place | None
    departure_at: AwareDatetime | None
    arrival_at: AwareDatetime | None
    duration_minutes: int | None = Field(ge=0)
    duration_basis: Literal["schedule", "estimate_osrm", "estimate_mock", "unknown"]
    carrier: str | None
    pets_allowed: bool | None
    price: Price
    terms: dict[str, Any] = Field(default_factory=dict)
    source: SourceReference
    availability: Availability
    validity: Validity
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> TravelOffer:
        if self.kind == "hotel":
            raise ValueError("гостиничные предложения описываются HotelOffer")
        if self.availability.subject == "hotel":
            raise ValueError("для транспорта нельзя использовать гостиничную семантику (CONTRACT-02)")
        if self.departure_at and self.arrival_at and self.arrival_at < self.departure_at:
            raise ValueError("прибытие раньше отправления")
        if self.source.data_mode != self.availability.data_mode:
            raise ValueError("режим данных оффера и доступности должен совпадать")
        return self


class Rating(Contract):
    value: Decimal
    scale: Decimal
    count: int | None = Field(ge=0)


class HotelLocation(Contract):
    city: str
    address: str | None
    timezone: TimeZoneName | None


class Hotel(VersionedContract):
    hotel_id: str
    source_hotel_id: str
    name: str
    location: HotelLocation
    geo: dict[str, float] | None
    amenities: list[str] = Field(default_factory=list)
    rating: Rating | None
    reviews_summary: str | None
    official_website: HttpUrlStr | None
    phone: str | None = Field(pattern=r"^\+?[0-9 ()\-]{5,25}$")
    star_rating: int | None = Field(ge=0, le=5)
    pets_allowed: bool | None
    check_in_from: time | None
    check_out_until: time | None
    source: SourceReference


class Occupancy(Contract):
    adults: int = Field(ge=1, le=9)
    children_ages: list[int] = Field(default_factory=list)
    rooms: int = Field(ge=1, le=5)


class HotelTerms(Contract):
    breakfast_included: bool | None
    meals: str | None
    free_cancellation: bool | None
    free_cancellation_until: AwareDatetime | None


class HotelOffer(VersionedContract):
    offer_id: str
    hotel_id: str
    check_in: date
    check_out: date
    occupancy: Occupancy
    room_type: str | None
    price_total: Price
    currency: Currency | None
    price_scope: PriceScope
    availability: Availability
    source: SourceReference
    terms: HotelTerms
    limitations: list[str] = Field(default_factory=list)
    validity: Validity

    @model_validator(mode="after")
    def _rules(self) -> HotelOffer:
        if self.check_out <= self.check_in:
            raise ValueError("выезд должен быть позже заезда")
        if self.availability.subject != "hotel":
            raise ValueError("доступность гостиницы должна иметь subject=hotel")
        if self.price_total.price_scope != self.price_scope:
            raise ValueError("price_scope оффера и цены должны совпадать")
        if self.price_total.currency != self.currency:
            raise ValueError("валюта оффера и цены должны совпадать")
        return self


class ReviewEvidence(VersionedContract):
    """Свидетельства отзывов (REVIEW-01, P1): рейтинг, объём и цитаты различаются."""

    hotel_id: str
    source: SourceReference
    review_rating: Decimal | None
    rating_scale: Decimal | None
    review_count: int | None = Field(ge=0)
    review_text_evidence: list[str] = Field(default_factory=list)
    review_aspects: list[dict[str, Any]] = Field(default_factory=list)
    observed_at: AwareDatetime | None
    review_summary: str | None


class CandidateDestination(VersionedContract):
    """Кандидат направления для вдохновения (UC-04). Визы никогда не объявляются проверенными."""

    candidate_id: str
    place: Place
    tags: list[str]
    matched_tags: list[str]
    description: str
    climate_status: Literal["not_loaded", "norm", "unknown"]
    climate_note: str | None
    estimated_cost: Price | None
    cost_note: str
    visa_status: Literal["not_verified"]
    source: SourceReference
