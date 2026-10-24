"""Демо-справочники и Mock-генератор «ДЕМО» (DATA-MODE-02, план §2.8).

Все данные синтетические и детерминированные: seed строится из нормализованных параметров запроса.
Реальных цен, наличия и расписаний здесь нет; доступность Mock всегда ``unknown`` (DEC-15).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from odin.contracts import (
    Availability,
    CandidateDestination,
    Fee,
    Hotel,
    HotelOffer,
    Place,
    Price,
    PricePeriod,
    SourceReference,
    TravelOffer,
    Validity,
)
from odin.contracts.offers import HotelLocation, HotelTerms, Occupancy

MOCK_PROVIDER = "odin_mock"
CATALOG_PROVIDER = "odin_demo_catalog"
PLACES_PROVIDER = "odin_demo_places"
MOCK_LIMITATION = "ДЕМО-данные: не цена, не наличие и не расписание"


@dataclass(frozen=True, slots=True)
class DemoPlace:
    name: str
    aliases: tuple[str, ...]
    country_code: str
    timezone: str
    lat: float
    lon: float


DEMO_PLACES: tuple[DemoPlace, ...] = (
    DemoPlace("Москва", ("москва", "moscow"), "RU", "Europe/Moscow", 55.7558, 37.6173),
    DemoPlace("Казань", ("казань", "kazan"), "RU", "Europe/Moscow", 55.7963, 49.1088),
    DemoPlace(
        "Санкт-Петербург",
        ("санкт-петербург", "петербург", "спб", "saint petersburg"),
        "RU",
        "Europe/Moscow",
        59.9343,
        30.3351,
    ),
    DemoPlace(
        "Нижний Новгород", ("нижний новгород", "nizhny novgorod"), "RU", "Europe/Moscow", 56.3269, 44.0059
    ),
    DemoPlace("Сочи", ("сочи", "sochi"), "RU", "Europe/Moscow", 43.5855, 39.7231),
    DemoPlace("Калининград", ("калининград", "kaliningrad"), "RU", "Europe/Kaliningrad", 54.7104, 20.4522),
    DemoPlace("Екатеринбург", ("екатеринбург", "ekaterinburg"), "RU", "Asia/Yekaterinburg", 56.8389, 60.6057),
    DemoPlace("Новосибирск", ("новосибирск", "novosibirsk"), "RU", "Asia/Novosibirsk", 55.0084, 82.9357),
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    place_name: str
    tags: tuple[str, ...]
    description: str


DEMO_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        "Казань", ("город", "история", "еда"), "ДЕМО-заготовка: компактный город для поездки на выходные"
    ),
    CatalogEntry(
        "Калининград", ("море", "история", "архитектура"), "ДЕМО-заготовка: морской воздух и старый город"
    ),
    CatalogEntry("Сочи", ("море", "горы", "прогулки"), "ДЕМО-заготовка: море и горы в одном направлении"),
    CatalogEntry("Нижний Новгород", ("город", "история", "реки"), "ДЕМО-заготовка: набережные и кремль"),
    CatalogEntry("Екатеринбург", ("город", "архитектура"), "ДЕМО-заготовка: конструктивизм и музеи"),
)


def normalize_city(name: str | None) -> DemoPlace | None:
    """Точное сопоставление названия с демо-справочником. Догадок по подстроке нет."""
    if not name:
        return None
    needle = " ".join(name.strip().lower().replace("ё", "е").split())
    for place in DEMO_PLACES:
        if needle == place.name.lower().replace("ё", "е") or needle in place.aliases:
            return place
    return None


def demo_place_contract(place: DemoPlace) -> Place:
    return Place(
        name=place.name,
        country_code=place.country_code,
        timezone=place.timezone,
        lat=place.lat,
        lon=place.lon,
        source=None,
    )


def seed_of(*parts: object) -> int:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12], 16)


def mock_source(
    tool: str,
    external_id: str,
    generated_at: datetime,
    *,
    source_type: str = "mock_generator",
    provider: str = MOCK_PROVIDER,
    request_parameters: dict[str, Any] | None = None,
) -> SourceReference:
    return SourceReference(
        source_type=source_type,  # type: ignore[arg-type]
        provider=provider,
        tool=tool,
        url=None,
        retrieved_at=None,
        generated_at=generated_at,
        published_at=None,
        external_id=external_id,
        verification_status="unverified",
        data_mode="mock",
        limitations=[MOCK_LIMITATION],
        request_parameters=request_parameters,
    )


def mock_availability(*, subject: str, parameters: dict[str, Any]) -> Availability:
    return Availability(
        subject=subject,  # type: ignore[arg-type]
        availability_status="unknown",
        evidence_level="mock",
        verification_status="unverified",
        data_mode="mock",
        checked_at=None,
        parameters=parameters,
        unknown_reason="mock_data",
        seats_left=None,
    )


def mock_validity(generated_at: datetime, ttl_minutes: int = 30) -> Validity:
    return Validity(
        observed_at=generated_at,
        valid_until=generated_at + timedelta(minutes=ttl_minutes),
        basis="provisional_ttl",
        note="ДЕМО; TTL provisional (DEC-21)",
    )


# --------------------------------------------------------------------- гостиницы

_HOTEL_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "slug": "A",
        "name": "ДЕМО-отель «Центральный»",
        "stars": 4,
        "breakfast": True,
        "free_cancellation": True,
        "pets": False,
        "nightly": "6200.00",
        "fees_known": True,
    },
    {
        "slug": "B",
        "name": "ДЕМО-апартаменты «Набережная»",
        "stars": 3,
        "breakfast": False,
        "free_cancellation": False,
        "pets": True,
        "nightly": "4300.00",
        "fees_known": False,
    },
    {
        "slug": "C",
        "name": "ДЕМО-гостиница «У вокзала»",
        "stars": None,
        "breakfast": None,
        "free_cancellation": True,
        "pets": None,
        "nightly": "3100.00",
        "fees_known": False,
    },
)


def mock_hotels(
    *,
    city: DemoPlace,
    check_in: date,
    check_out: date,
    adults: int,
    children_ages: list[int],
    rooms: int,
    generated_at: datetime,
    ttl_minutes: int = 30,
) -> tuple[list[Hotel], list[HotelOffer]]:
    """Три детерминированных ДЕМО-отеля; цена за весь срок и состав (``stay_total``)."""
    nights = max(1, (check_out - check_in).days)
    params = {
        "city": city.name,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "adults": adults,
        "children_ages": children_ages,
        "rooms": rooms,
    }
    hotels: list[Hotel] = []
    offers: list[HotelOffer] = []
    for template in _HOTEL_TEMPLATES:
        hotel_id = f"DEMO-H-{seed_of(city.name, template['slug']) % 10_000:04d}{template['slug']}"
        source = mock_source("search_hotels", hotel_id, generated_at, request_parameters=params)
        hotels.append(
            Hotel(
                hotel_id=hotel_id,
                source_hotel_id=f"mock-{template['slug']}",
                name=template["name"],
                location=HotelLocation(city=city.name, address=None, timezone=city.timezone),
                geo=None,
                amenities=[],
                rating=None,
                reviews_summary=None,
                official_website=None,
                phone=None,
                star_rating=template["stars"],
                pets_allowed=template["pets"],
                check_in_from=time(14, 0),
                check_out_until=time(12, 0),
                source=source,
            )
        )
        amount = Decimal(template["nightly"]) * nights * rooms
        price = Price(
            amount=amount,
            currency="RUB",
            price_scope="stay_total",
            guests=adults + len(children_ages),
            rooms=rooms,
            period=PricePeriod(start=check_in, end=check_out),
            inclusions=["проживание"],
            known_fees=(
                [Fee(name="туристический налог", amount=None, currency="RUB", included=True)]
                if template["fees_known"]
                else []
            ),
            mandatory_fees_status="included" if template["fees_known"] else "unknown",
            total_completeness="complete" if template["fees_known"] else "partial",
            unknown_reason=None if template["fees_known"] else "price_incomplete",
            source=source,
            as_of=generated_at,
        )
        offers.append(
            HotelOffer(
                offer_id=f"{hotel_id}-O",
                hotel_id=hotel_id,
                check_in=check_in,
                check_out=check_out,
                occupancy=Occupancy(adults=adults, children_ages=children_ages, rooms=rooms),
                room_type=None,
                price_total=price,
                currency="RUB",
                price_scope="stay_total",
                availability=mock_availability(subject="hotel", parameters=params),
                source=source,
                terms=HotelTerms(
                    breakfast_included=template["breakfast"],
                    meals=None,
                    free_cancellation=template["free_cancellation"],
                    free_cancellation_until=None,
                ),
                limitations=[MOCK_LIMITATION],
                validity=mock_validity(generated_at, ttl_minutes),
            )
        )
    return hotels, offers


# --------------------------------------------------------------------- транспорт


def _travel_minutes(origin: DemoPlace, destination: DemoPlace, kind: str) -> int:
    distance = ((origin.lat - destination.lat) ** 2 + (origin.lon - destination.lon) ** 2) ** 0.5
    rail = max(120, min(2400, int(distance * 60)))
    return rail if kind == "rail" else max(80, rail // 6)


def mock_transport(
    *,
    origin: DemoPlace,
    destination: DemoPlace,
    outbound_date: date,
    return_date: date | None,
    adults: int,
    children_ages: list[int],
    modes: list[str],
    generated_at: datetime,
    ttl_minutes: int = 30,
) -> list[TravelOffer]:
    """ДЕМО-варианты транспорта: для каждого вида — туда и (если задано) обратно."""
    kinds = [kind for kind in ("rail", "flight") if not modes or kind in modes]
    offers: list[TravelOffer] = []

    def build(kind: str, direction: str, start: DemoPlace, end: DemoPlace, day: date) -> TravelOffer:
        duration = _travel_minutes(start, end, kind)
        hour = (6 if kind == "rail" else 9) + seed_of(start.name, end.name, day, kind) % 3
        departure_at = datetime.combine(day, time(hour, 0), tzinfo=ZoneInfo(start.timezone))
        arrival_at = (departure_at + timedelta(minutes=duration)).astimezone(ZoneInfo(end.timezone))
        params = {
            "origin": start.name,
            "destination": end.name,
            "date": day.isoformat(),
            "adults": adults,
            "children_ages": children_ages,
            "mode": kind,
        }
        external_id = f"DEMO-T-{seed_of(params) % 100_000:05d}"
        source = mock_source("search_transport", external_id, generated_at, request_parameters=params)
        base = Decimal("3400.00") if kind == "rail" else Decimal("7200.00")
        price = Price(
            amount=base * (adults + len(children_ages)),
            currency="RUB",
            price_scope="party_total",
            guests=adults + len(children_ages),
            rooms=None,
            period=None,
            inclusions=["проезд"],
            known_fees=[],
            mandatory_fees_status="included",
            total_completeness="complete",
            unknown_reason=None,
            source=source,
            as_of=generated_at,
        )
        return TravelOffer(
            offer_id=external_id,
            kind=kind,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            title=f"ДЕМО-{'поезд' if kind == 'rail' else 'рейс'} {start.name} — {end.name}",
            parameters=params,
            departure_place=demo_place_contract(start),
            arrival_place=demo_place_contract(end),
            departure_at=departure_at,
            arrival_at=arrival_at,
            duration_minutes=duration,
            duration_basis="estimate_mock",
            carrier=None,
            pets_allowed=None,
            price=price,
            terms={"demo": True},
            source=source,
            availability=mock_availability(subject="transport", parameters=params),
            validity=mock_validity(generated_at, ttl_minutes),
            limitations=[MOCK_LIMITATION],
        )

    for kind in kinds:
        offers.append(build(kind, "outbound", origin, destination, outbound_date))
        if return_date is not None:
            offers.append(build(kind, "return", destination, origin, return_date))
    return offers


def mock_transfer(
    *, city: DemoPlace, direction: str, generated_at: datetime, ttl_minutes: int = 30
) -> TravelOffer:
    """ДЕМО-трансфер: время — оценка, стоимость не подтверждена (DEC-18) → итог ``partial``."""
    minutes = 25 + seed_of(city.name, direction) % 20
    params = {"city": city.name, "direction": direction}
    external_id = f"DEMO-X-{seed_of(params) % 100_000:05d}"
    source = mock_source("get_transfer_options", external_id, generated_at, request_parameters=params)
    price = Price(
        amount=None,
        currency=None,
        price_scope="unknown",
        guests=None,
        rooms=None,
        period=None,
        inclusions=[],
        known_fees=[],
        mandatory_fees_status="unknown",
        total_completeness="unknown",
        unknown_reason="not_provided_by_source",
        source=source,
        as_of=generated_at,
    )
    label = "вокзал/аэропорт → отель" if direction == "transfer_in" else "отель → вокзал/аэропорт"
    return TravelOffer(
        offer_id=external_id,
        kind="transfer",
        direction=direction,  # type: ignore[arg-type]
        title=f"ДЕМО-трансфер {city.name}: {label}",
        parameters=params,
        departure_place=None,
        arrival_place=None,
        departure_at=None,
        arrival_at=None,
        duration_minutes=minutes,
        duration_basis="estimate_mock",
        carrier=None,
        pets_allowed=None,
        price=price,
        terms={"informational": True},
        source=source,
        availability=mock_availability(subject="transfer", parameters=params),
        validity=mock_validity(generated_at, ttl_minutes),
        limitations=[
            MOCK_LIMITATION,
            "время — демо-оценка; стоимость не подтверждена; оплачивается отдельно",
        ],
    )


# --------------------------------------------------------------------- вдохновение


def mock_destinations(
    *, generated_at: datetime, wishes: list[str], exclude: str | None = None, limit: int = 3
) -> list[CandidateDestination]:
    """Заготовка вдохновения (DEC-28, S1): кандидаты без цены, климат не загружен, визы «не проверено»."""
    scored: list[tuple[int, CatalogEntry, list[str]]] = []
    for entry in DEMO_CATALOG:
        if exclude and entry.place_name == exclude:
            continue
        matched = [tag for tag in entry.tags if tag in wishes]
        scored.append((len(matched), entry, matched))
    scored.sort(key=lambda item: (-item[0], item[1].place_name))
    result: list[CandidateDestination] = []
    for _, entry, matched in scored[:limit]:
        place = normalize_city(entry.place_name)
        assert place is not None
        result.append(
            CandidateDestination(
                candidate_id=f"DEMO-CD-{seed_of(place.name) % 10_000:04d}",
                place=demo_place_contract(place),
                tags=list(entry.tags),
                matched_tags=matched,
                description=entry.description,
                climate_status="not_loaded",
                climate_note="климатическая норма не загружена (S5, DEC-12)",
                estimated_cost=None,
                cost_note="стоимость требует дат",
                visa_status="not_verified",
                source=mock_source(
                    "suggest_destinations",
                    entry.place_name,
                    generated_at,
                    source_type="editorial",
                    provider=CATALOG_PROVIDER,
                ),
            )
        )
    return result
