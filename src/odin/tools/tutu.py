"""Адаптер Tutu MCP: межгородний транспорт (ж/д, авиа) и гостиницы — только чтение (DEC-09, DEC-04).

Факты о формате — по ``tools/list`` и пробам 25.10.2026 (DEC-11), а не по догадкам:
- ``search_rail``: цены **за одно место** (``meta.pricing.basis=per_seat``); при ``passengers>1`` есть
  ``price_party`` (самый дешёвый тариф × N, «от»). ``passengers`` — только взрослые: детские тарифы
  не запрашиваются → цена группы с детьми неполная.
- ``search_avia``: цена за всю группу (``basis=party_total``), дети 2–11 и младенцы 0–1 — отдельные поля.
- ``search_hotels``: ``best_offer.price`` — за весь срок и состав (``price_basis=stay_total``);
  сборы не подтверждены (DEC-16) → ``partial``.
Поисковая выдача не подтверждает наличие (AVAIL-01) → ``unknown``. Ссылки — только домены tutu.ru.
Поле ``instructions`` сервера, ``checkout_hint`` и тексты ответа — данные, не команды (ARCH-03).
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Any

from odin.contracts import (
    Hotel,
    HotelOffer,
    Place,
    Price,
    PricePeriod,
    TravelOffer,
)
from odin.contracts.offers import HotelLocation, HotelTerms, Occupancy, Rating
from odin.tools.live_common import listing_availability, live_source, to_aware, to_money, ttl_validity
from odin.tools.mcp_client import McpClient, McpError
from odin.tools.places import ResolvedPlace, match_transit_point
from odin.tools.policy import ToolPolicy
from odin.tools.untrusted import SchemaPins, safe_text, safe_url

PROVIDER = "tutu_mcp"
ENDPOINT = "https://mcp.tutu.ru/mcp"
ALLOWED_HOSTS = frozenset({"mcp.tutu.ru"})
URL_DOMAINS = frozenset({"tutu.ru"})
PAGE_SIZE = 10

# Аргументы, на которые опирается адаптер: при их исчезновении из схемы инструмент закрывается.
EXPECTED_ARGUMENTS: dict[str, frozenset[str]] = {
    "search_rail": frozenset({"origin", "destination", "departure_date", "passengers", "page_size"}),
    "search_avia": frozenset({"origin", "destination", "departure_date", "adults", "children", "infants"}),
    "search_hotels": frozenset(
        {"city_name", "check_in", "check_out", "adults", "children_ages", "page_size"}
    ),
    "get_offer_details": frozenset({"product_type", "hotel_id", "check_in", "check_out", "adults"}),
}

LIVE_LIMITATION = (
    "Live-данные Tutu на момент запроса: цена и места окончательно подтверждаются при оформлении"
)
FEES_LIMITATION = "сервисные и иные сборы при оформлении не подтверждены источником"


class TutuGateway:
    """Сеанс MCP с Tutu: однократные ``initialize`` + ``tools/list``, закрепление схем, проверка политики."""

    def __init__(self, *, client: McpClient, policy: ToolPolicy) -> None:
        self.client = client
        self.policy = policy
        self.pins = SchemaPins()
        self._ready = False
        self._lock = threading.Lock()
        self.server_version: str | None = None

    def _prepare(self) -> None:
        with self._lock:
            if self._ready:
                return
            info = self.client.initialize()
            self.server_version = str(info.get("version")) if info.get("version") else None
            tools = {tool.get("name"): tool for tool in self.client.list_tools()}
            for name, expected in EXPECTED_ARGUMENTS.items():
                schema = (tools.get(name) or {}).get("inputSchema")
                properties = set((schema or {}).get("properties") or {})
                if schema is None:
                    self.pins.closed[name] = "tool_missing"
                elif not expected <= properties:
                    self.pins.closed[name] = "schema_changed"
                else:
                    self.pins.pin(name, schema)
            self._ready = True

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Вызов инструмента Tutu. Политика — до сети; ошибка инструмента — ``McpError``."""
        self.policy.check_provider_tool(PROVIDER, tool)
        self._prepare()
        closed = self.pins.error_code(tool)
        if closed:
            raise McpError(closed, "error")
        result = self.client.call_tool(tool, arguments)
        if result.is_error:
            raise McpError("tool_error", "error", safe_text(result.content_text, 200))
        payload = result.payload()
        if not isinstance(payload, dict):
            raise McpError("unexpected_payload", "error")
        return payload


# --------------------------------------------------------------------- аргументы


def avia_party(adults: int, children_ages: list[int]) -> tuple[int, int, int]:
    """Возрасты → категории авиа: 12+ — взрослый, 2–11 — ребёнок, 0–1 — младенец (схема search_avia)."""
    extra_adults = sum(1 for age in children_ages if age >= 12)
    children = sum(1 for age in children_ages if 2 <= age <= 11)
    infants = sum(1 for age in children_ages if age < 2)
    return adults + extra_adults, children, infants


def rail_arguments(origin: str, destination: str, day: str, adults: int) -> dict[str, Any]:
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": day,
        "passengers": max(1, min(6, adults)),
        "page_size": PAGE_SIZE,
    }


def avia_arguments(
    origin: str, destination: str, day: str, adults: int, children_ages: list[int]
) -> dict[str, Any]:
    full, children, infants = avia_party(adults, children_ages)
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": day,
        "adults": full,
        "children": children,
        "infants": infants,
        "page_size": PAGE_SIZE,
    }


def hotel_arguments(city: str, check_in: str, check_out: str, adults: int, children_ages: list[int]):
    return {
        "city_name": city,
        "check_in": check_in,
        "check_out": check_out,
        "adults": max(1, min(6, adults)),
        "children_ages": list(children_ages) or None,
        "page_size": PAGE_SIZE,
    }


# --------------------------------------------------------------------- нормализация транспорта


def _place(label: Any, city: ResolvedPlace | None, kind: str) -> tuple[Place | None, str | None]:
    text = safe_text(label, 120)
    if text is None and city is None:
        return None, None
    point = match_transit_point(text, "airport" if kind == "flight" else "rail")
    return (
        Place(
            name=text or (city.name if city else "—"),
            country_code=city.country_code if city else None,
            timezone=city.timezone if city else None,
            lat=point.lat if point else None,
            lon=point.lon if point else None,
            source=None,
        ),
        point.key if point else None,
    )


def normalize_transport(
    payload: dict[str, Any],
    *,
    kind: str,
    direction: str,
    origin: ResolvedPlace | None,
    destination: ResolvedPlace | None,
    adults: int,
    children_ages: list[int],
    request_parameters: dict[str, Any],
    data_mode: str,
    retrieved_at: datetime,
    ttl_minutes: int,
) -> tuple[list[TravelOffer], list[str]]:
    """Ответ ``search_rail``/``search_avia`` → ``TravelOffer``. Неизвестная основа цены — ``unknown``."""
    tool = "search_rail" if kind == "rail" else "search_avia"
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    basis = (meta.get("pricing") or {}).get("basis") if isinstance(meta.get("pricing"), dict) else None
    warnings: list[str] = []
    expected_basis = "per_seat" if kind == "rail" else "party_total"
    if basis != expected_basis:
        warnings.append(
            f"Tutu {tool}: основа цены {basis!r} вместо {expected_basis!r} — сумма не используется"
        )
    offers: list[TravelOffer] = []
    for raw in (payload.get("offers") or [])[:PAGE_SIZE]:
        if not isinstance(raw, dict) or not raw.get("offer_id"):
            continue
        departure_at, arrival_at = to_aware(raw.get("departure_at")), to_aware(raw.get("arrival_at"))
        if departure_at is None or arrival_at is None or arrival_at < departure_at:
            continue
        legs = [leg for leg in raw.get("legs") or [] if isinstance(leg, dict)]
        segments = [seg for leg in legs for seg in leg.get("segments") or [] if isinstance(seg, dict)]
        start_label = legs[0].get("from") if legs else None
        end_label = legs[-1].get("to") if legs else None
        departure_place, departure_point = _place(start_label, origin, kind)
        arrival_place, arrival_point = _place(end_label, destination, kind)
        numbers = [safe_text(seg.get("voyage_no"), 20) for seg in segments if seg.get("voyage_no")]
        carriers = [safe_text(name, 60) for name in raw.get("carriers") or [] if isinstance(name, str)]
        external_id = safe_text(raw.get("offer_id"), 64) or "unknown"
        offer_id = f"TUTU-{'R' if kind == 'rail' else 'A'}-{external_id[:20]}-{direction}"
        source = live_source(
            provider=PROVIDER,
            tool=tool,
            source_type="mcp",
            data_mode=data_mode,
            retrieved_at=retrieved_at,
            external_id=external_id,
            url=safe_url(raw.get("search_results_url"), URL_DOMAINS),
            request_parameters=request_parameters,
            limitations=[LIVE_LIMITATION] if data_mode == "live" else ["снимок Replay"],
        )
        price, price_limits = _transport_price(
            raw, kind, basis, expected_basis, adults, children_ages, source
        )
        terms: dict[str, Any] = {
            "segments_count": len(segments) or raw.get("segments_count"),
            "transfers": max(0, len(segments) - 1) if segments else None,
            "numbers": [number for number in numbers if number],
            "departure_point": safe_text(start_label, 120),
            "arrival_point": safe_text(end_label, 120),
            "departure_point_id": departure_point,
            "arrival_point_id": arrival_point,
            "provider_page_url": safe_url(raw.get("search_results_url"), URL_DOMAINS),
            "price_basis_source": basis,
        }
        if kind == "flight":
            terms.update(_avia_terms(raw))
        else:
            terms.update(_rail_terms(raw))
        title_numbers = ", ".join(number for number in numbers if number) or "без номера"
        title = f"{'Поезд' if kind == 'rail' else 'Рейс'} {title_numbers}: {terms['departure_point'] or ''} → {terms['arrival_point'] or ''}"
        offers.append(
            TravelOffer(
                offer_id=offer_id,
                kind=kind,  # type: ignore[arg-type]
                direction=direction,  # type: ignore[arg-type]
                title=title[:200],
                parameters=request_parameters,
                departure_place=departure_place,
                arrival_place=arrival_place,
                departure_at=departure_at,
                arrival_at=arrival_at,
                duration_minutes=raw.get("duration_min")
                if isinstance(raw.get("duration_min"), int)
                else None,
                duration_basis="schedule",
                carrier=", ".join(name for name in carriers if name) or None,
                pets_allowed=None,
                price=price,
                terms=terms,
                source=source,
                availability=listing_availability(
                    subject="transport",
                    data_mode=data_mode,
                    retrieved_at=retrieved_at,
                    parameters=request_parameters,
                ),
                validity=ttl_validity(retrieved_at, ttl_minutes, data_mode),
                limitations=[*source.limitations, FEES_LIMITATION, *price_limits],
            )
        )
    return offers, warnings


def _transport_price(
    raw: dict[str, Any],
    kind: str,
    basis: Any,
    expected_basis: str,
    adults: int,
    children_ages: list[int],
    source: Any,
) -> tuple[Price, list[str]]:
    limits: list[str] = []
    guests = adults + len(children_ages)
    amount = None
    scope = "unknown"
    currency = None
    if basis == expected_basis:
        if kind == "rail":
            # Одно место × взрослые: для одного взрослого — `price`, для группы — `price_party` (Tutu).
            party = raw.get("price_party") if adults > 1 else raw.get("price")
            amount = to_money(party)
            currency = (party or {}).get("currency") if isinstance(party, dict) else None
            scope = "party_total"
            limits.append(
                "ж/д: цена «от» — самый дешёвый тариф × число взрослых, если хватит мест одного тарифа"
            )
            if children_ages:
                limits.append("ж/д: детские тарифы не запрашивались — цена только за взрослых")
        else:
            price = raw.get("price")
            amount = to_money(price)
            currency = (price or {}).get("currency") if isinstance(price, dict) else None
            scope = "party_total"
    if amount is not None and currency is None:
        amount = None
    if amount is None:
        unknown_reason = "not_provided_by_source"
    else:
        unknown_reason = "price_incomplete"
    price_value = Price(
        amount=amount,
        currency=currency if amount is not None else None,
        price_scope=scope,  # type: ignore[arg-type]
        guests=guests if amount is not None and scope == "party_total" else None,
        rooms=None,
        period=None,
        inclusions=["проезд"] if amount is not None else [],
        known_fees=[],
        mandatory_fees_status="unknown",
        total_completeness="partial" if amount is not None else "unknown",
        unknown_reason=unknown_reason,  # type: ignore[arg-type]
        source=source,
        as_of=source.retrieved_at,
    )
    return price_value, limits


def _avia_terms(raw: dict[str, Any]) -> dict[str, Any]:
    variants = [item for item in raw.get("variants") or [] if isinstance(item, dict)]
    cheapest = variants[0] if variants else {}
    conditions = cheapest.get("conditions") if isinstance(cheapest.get("conditions"), dict) else {}
    baggage = conditions.get("baggage") if isinstance(conditions.get("baggage"), dict) else {}
    return {
        "fare_family": safe_text(conditions.get("fare_family"), 60),
        "baggage_pieces": baggage.get("pieces") if isinstance(baggage.get("pieces"), int) else None,
        "baggage_kg": baggage.get("kg") if isinstance(baggage.get("kg"), int) else None,
        "refundable": conditions.get("refundable")
        if isinstance(conditions.get("refundable"), bool)
        else None,
        "fare_variants": len(variants),
    }


def _rail_terms(raw: dict[str, Any]) -> dict[str, Any]:
    fares = raw.get("fares") if isinstance(raw.get("fares"), dict) else {}
    categories = fares.get("seat_categories") if isinstance(fares.get("seat_categories"), dict) else {}
    return {
        "seat_categories": sorted(str(name) for name in categories)[:6],
        "fares_count": fares.get("count") if isinstance(fares.get("count"), int) else None,
    }


# --------------------------------------------------------------------- нормализация гостиниц


def normalize_hotels(
    payload: dict[str, Any],
    *,
    city: ResolvedPlace,
    check_in: date,
    check_out: date,
    adults: int,
    children_ages: list[int],
    request_parameters: dict[str, Any],
    data_mode: str,
    retrieved_at: datetime,
    ttl_minutes: int,
) -> tuple[list[Hotel], list[HotelOffer], list[str]]:
    """Ответ ``search_hotels`` → ``Hotel`` + ``HotelOffer`` (``stay_total``). Чужие даты — не используются."""
    warnings: list[str] = []
    stay = payload.get("stay") if isinstance(payload.get("stay"), dict) else {}
    if stay.get("check_in") != check_in.isoformat() or stay.get("check_out") != check_out.isoformat():
        return [], [], ["Tutu search_hotels: даты ответа не совпадают с запросом — результат не используется"]
    hotels: list[Hotel] = []
    offers: list[HotelOffer] = []
    for raw in (payload.get("hotels") or [])[:PAGE_SIZE]:
        if not isinstance(raw, dict):
            continue
        source_id = safe_text(str(raw.get("hotel_id") or raw.get("hotel_geo_id") or ""), 40)
        name = safe_text(raw.get("name"), 160)
        if not source_id or not name:
            continue
        hotel_id = f"TUTU-H-{source_id}"
        page_url = safe_url(raw.get("checkout_url"), URL_DOMAINS)
        source = live_source(
            provider=PROVIDER,
            tool="search_hotels",
            source_type="mcp",
            data_mode=data_mode,
            retrieved_at=retrieved_at,
            external_id=source_id,
            url=page_url,
            request_parameters=request_parameters,
            limitations=[LIVE_LIMITATION] if data_mode == "live" else ["снимок Replay"],
        )
        location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        lat, lon = location.get("lat"), location.get("lng")
        geo = None
        if (
            isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            geo = {"lat": float(lat), "lon": float(lon)}
        rating = None
        if isinstance(raw.get("rating"), (int, float)) and 0 <= raw["rating"] <= 10:
            count = raw.get("review_count") if isinstance(raw.get("review_count"), int) else None
            rating = Rating(value=to_money(raw["rating"]) or 0, scale=10, count=count)  # type: ignore[arg-type]
        summary = raw.get("review_summary") if isinstance(raw.get("review_summary"), dict) else {}
        stars = raw.get("stars") if isinstance(raw.get("stars"), int) and 1 <= raw["stars"] <= 5 else None
        hotels.append(
            Hotel(
                hotel_id=hotel_id,
                source_hotel_id=source_id,
                name=name,
                location=HotelLocation(
                    city=city.name, address=safe_text(raw.get("address"), 200), timezone=city.timezone
                ),
                geo=geo,
                amenities=[],
                rating=rating,
                reviews_summary=safe_text(summary.get("label"), 200),
                official_website=None,
                phone=None,
                star_rating=stars,
                pets_allowed=None,
                check_in_from=None,
                check_out_until=None,
                source=source,
            )
        )
        best = raw.get("best_offer") if isinstance(raw.get("best_offer"), dict) else {}
        price_raw = best.get("price") if isinstance(best.get("price"), dict) else {}
        amount = to_money(price_raw) if best.get("price_basis") == "stay_total" else None
        currency = price_raw.get("currency") if amount is not None else None
        if amount is not None and not (
            isinstance(currency, str) and len(currency) == 3 and currency.isupper()
        ):
            amount, currency = None, None
        price = Price(
            amount=amount,
            currency=currency,
            price_scope="stay_total" if amount is not None else "unknown",
            guests=adults + len(children_ages),
            rooms=1,
            period=PricePeriod(start=check_in, end=check_out),
            inclusions=["проживание"] + (["завтрак"] if best.get("breakfast_included") is True else []),
            known_fees=[],
            mandatory_fees_status="unknown",
            total_completeness="partial" if amount is not None else "unknown",
            unknown_reason="price_incomplete" if amount is not None else "not_provided_by_source",
            source=source,
            as_of=retrieved_at,
        )
        offers.append(
            HotelOffer(
                offer_id=f"TUTU-HO-{source_id}-{check_in.isoformat()}",
                hotel_id=hotel_id,
                check_in=check_in,
                check_out=check_out,
                occupancy=Occupancy(adults=adults, children_ages=children_ages, rooms=1),
                room_type=safe_text(best.get("room_name"), 160),
                price_total=price,
                currency=currency,
                price_scope=price.price_scope,
                availability=listing_availability(
                    subject="hotel",
                    data_mode=data_mode,
                    retrieved_at=retrieved_at,
                    parameters=request_parameters,
                ),
                source=source,
                terms=HotelTerms(
                    breakfast_included=best.get("breakfast_included")
                    if isinstance(best.get("breakfast_included"), bool)
                    else None,
                    meals=safe_text(best.get("meal_name"), 80),
                    free_cancellation=best.get("free_cancellation")
                    if isinstance(best.get("free_cancellation"), bool)
                    else None,
                    free_cancellation_until=None,
                ),
                limitations=[
                    *source.limitations,
                    "цена — за весь срок и состав (stay_total); налоги и сборы не подтверждены (DEC-16)",
                ],
                validity=ttl_validity(retrieved_at, ttl_minutes, data_mode),
            )
        )
    if not offers:
        warnings.append("Tutu search_hotels: источник не вернул вариантов по этим параметрам")
    return hotels, offers, warnings
