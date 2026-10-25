"""REST-адаптеры Live: Open-Meteo (геокодинг) и OSRM (время автотрансфера) — DEC-09.

- Open-Meteo geocoding: город → координаты и часовой пояс IANA (проба 25.10: «Казань» → Europe/Moscow).
  Ответ проверяется: пояс должен существовать, координаты — в диапазоне.
- OSRM (публичный демо-сервер): матрица длительностей на автомобиле **без пробок**. Это оценка
  времени (``estimate_osrm``), не стоимость (DEC-18) и не доказательство стыка (DEC-19).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odin.contracts import Price, TravelOffer
from odin.tools.live_common import live_source, ttl_validity
from odin.tools.places import TRANSIT_POINTS_LIMITATION, ResolvedPlace
from odin.tools.untrusted import safe_text

OPEN_METEO = "open_meteo"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_HOSTS = frozenset({"geocoding-api.open-meteo.com"})

OSRM = "osrm_demo"
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/"
OSRM_HOSTS = frozenset({"router.project-osrm.org"})

USER_AGENT = "odin-zapros/0.2 (hackathon prototype; read-only)"
OSRM_LIMITATION = "оценка времени на автомобиле по дорожной сети без пробок (OSRM); не расписание"
TRANSFER_COST_LIMITATION = "стоимость трансфера не оценивается (DEC-18); оплачивается отдельно"


def geocoding_params(name: str, language: str = "ru") -> dict[str, Any]:
    return {"name": name.strip()[:120], "count": 5, "language": language, "format": "json"}


def normalize_geocoding(
    payload: Any, *, data_mode: str, retrieved_at: datetime, query: str
) -> list[ResolvedPlace]:
    """Результаты геокодинга по порядку источника; непригодные записи отбрасываются."""
    places: list[ResolvedPlace] = []
    results = payload.get("results") if isinstance(payload, dict) else None
    for raw in results or []:
        if not isinstance(raw, dict):
            continue
        name = safe_text(raw.get("name"), 120)
        lat, lon, zone = raw.get("latitude"), raw.get("longitude"), raw.get("timezone")
        if not name or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not isinstance(zone, str):
            continue
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        country = raw.get("country_code") if isinstance(raw.get("country_code"), str) else None
        source = live_source(
            provider=OPEN_METEO,
            tool="geocoding",
            source_type="rest_api",
            data_mode=data_mode,
            retrieved_at=retrieved_at,
            external_id=str(raw.get("id")) if raw.get("id") is not None else None,
            request_parameters={"name": query},
            limitations=["геокодинг Open-Meteo (CC BY 4.0): центр населённого пункта"],
        )
        places.append(
            ResolvedPlace(
                name=name,
                country_code=country.upper() if country and len(country) == 2 else None,
                timezone=zone,
                lat=float(lat),
                lon=float(lon),
                source=source,
            )
        )
    return places


def place_to_dict(place: ResolvedPlace) -> dict[str, Any]:
    return {
        "name": place.name,
        "country_code": place.country_code,
        "timezone": place.timezone,
        "lat": place.lat,
        "lon": place.lon,
        "source": place.source.model_dump(mode="json") if place.source else None,
    }


# --------------------------------------------------------------------- OSRM


def osrm_table_request(coordinates: list[tuple[float, float]]) -> tuple[str, dict[str, Any]]:
    """URL и параметры матрицы: координаты в порядке lon,lat; округление до 5 знаков (≈1 м)."""
    path = ";".join(f"{lon:.5f},{lat:.5f}" for lat, lon in coordinates)
    return OSRM_TABLE_URL + path, {"annotations": "duration,distance"}


def normalize_transfers(
    payload: Any,
    *,
    points: list[dict[str, Any]],
    hotels: list[dict[str, Any]],
    city: str,
    data_mode: str,
    retrieved_at: datetime,
    ttl_minutes: int,
) -> list[TravelOffer]:
    """Матрица OSRM → трансферы «точка → отель» (transfer_in) и «отель → точка» (transfer_out)."""
    if not isinstance(payload, dict) or payload.get("code") != "Ok":
        return []
    durations = payload.get("durations")
    distances = payload.get("distances") if isinstance(payload.get("distances"), list) else None
    if not isinstance(durations, list):
        return []
    offers: list[TravelOffer] = []
    hotel_base = len(points)
    for p_index, point in enumerate(points):
        for h_index, hotel in enumerate(hotels):
            for direction in point.get("directions") or ["transfer_in"]:
                row, column = (p_index, hotel_base + h_index)
                if direction == "transfer_out":
                    row, column = column, row
                try:
                    seconds = durations[row][column]
                except (IndexError, TypeError):
                    continue
                if not isinstance(seconds, (int, float)) or seconds < 0:
                    continue
                meters = None
                if distances is not None:
                    try:
                        meters = distances[row][column]
                    except (IndexError, TypeError):
                        meters = None
                params = {
                    "city": city,
                    "direction": direction,
                    "point_id": point["point_id"],
                    "hotel_id": hotel["hotel_id"],
                    "mode": "car",
                }
                source = live_source(
                    provider=OSRM,
                    tool="table",
                    source_type="rest_api",
                    data_mode=data_mode,
                    retrieved_at=retrieved_at,
                    external_id=f"{point['point_id']}:{hotel['hotel_id']}",
                    request_parameters=params,
                    limitations=[OSRM_LIMITATION],
                )
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
                    as_of=retrieved_at,
                )
                hotel_name = safe_text(hotel.get("name"), 80) or "отель"
                label = (
                    f"{point['label']} → {hotel_name}"
                    if direction == "transfer_in"
                    else f"{hotel_name} → {point['label']}"
                )
                offers.append(
                    TravelOffer(
                        offer_id=f"OSRM-{direction}-{point['point_id']}-{hotel['hotel_id']}"[:120],
                        kind="transfer",
                        direction=direction,  # type: ignore[arg-type]
                        title=f"Трансфер на автомобиле: {label}"[:200],
                        parameters=params,
                        departure_place=None,
                        arrival_place=None,
                        departure_at=None,
                        arrival_at=None,
                        duration_minutes=max(1, math.ceil(seconds / 60)),
                        duration_basis="estimate_osrm",
                        carrier=None,
                        pets_allowed=None,
                        price=price,
                        terms={
                            "informational": True,
                            "distance_km": round(meters / 1000, 1)
                            if isinstance(meters, (int, float))
                            else None,
                        },
                        source=source,
                        availability=_transfer_availability(data_mode, retrieved_at, params),
                        validity=ttl_validity(retrieved_at, ttl_minutes, data_mode),
                        limitations=[OSRM_LIMITATION, TRANSFER_COST_LIMITATION, TRANSIT_POINTS_LIMITATION],
                    )
                )
    return offers


def _transfer_availability(data_mode: str, retrieved_at: datetime, params: dict[str, Any]):
    from odin.contracts import Availability

    return Availability(
        subject="transfer",
        availability_status="unknown",
        evidence_level="none" if data_mode == "live" else "historical",
        verification_status="unverified" if data_mode == "live" else "stale",
        data_mode=data_mode,  # type: ignore[arg-type]
        checked_at=retrieved_at,
        parameters=params,
        unknown_reason="estimate_only",
        seats_left=None,
    )
