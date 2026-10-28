"""Mock-адаптеры логических инструментов S1 (DATA-MODE-02).

Адаптеры возвращают нормализованные контракты §7 с пометкой «ДЕМО»; сеть не используется.
Неизвестный город — честный отказ (`error_code`), а не выдуманные варианты.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from odin.tools.mock_data import (
    mock_destinations,
    mock_hotels,
    mock_transfer,
    mock_transport,
    normalize_city,
)

AdapterResponse = dict[str, Any]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _error(message: str, code: str = "invalid_arguments") -> AdapterResponse:
    return {"status": "error", "data": None, "sources": [], "error_code": code, "warnings": [message]}


def _date(arguments: dict[str, Any], key: str) -> date | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _ok(items: list[Any], key: str, warning: str, *, status: str = "ok", missing: list[str] | None = None):
    if not items:
        return {
            "status": "empty",
            "data": {key: []},
            "sources": [],
            "retrieved_at": None,
            "warnings": [warning],
        }
    return {
        "status": status,
        "data": {key: items},
        "sources": [items[0].source],
        "retrieved_at": None,
        "warnings": [warning],
        "missing": missing or [],
    }


def search_hotels(arguments: dict[str, Any]) -> AdapterResponse:
    city = normalize_city(arguments.get("city"))
    if city is None:
        return _error("город не найден в демо-справочнике", "city_not_in_demo_directory")
    check_in, check_out = _date(arguments, "check_in"), _date(arguments, "check_out")
    if check_in is None or check_out is None or check_out <= check_in:
        return _error("нужны корректные даты заезда и выезда")
    adults = int(arguments.get("adults") or 0)
    if adults < 1:
        return _error("нужен состав группы: adults >= 1 (UC-03)")
    hotels, offers = mock_hotels(
        city=city,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children_ages=[int(age) for age in arguments.get("children_ages") or []],
        rooms=int(arguments.get("rooms") or 1),
        generated_at=_now(),
        ttl_minutes=int(arguments.get("ttl_minutes") or 30),
    )
    result = _ok(offers, "offers", "ДЕМО-данные: наличие и сборы не подтверждены")
    if result["status"] == "ok":
        result["data"]["hotels"] = hotels
    return result


def search_transport(arguments: dict[str, Any]) -> AdapterResponse:
    origin = normalize_city(arguments.get("origin"))
    destination = normalize_city(arguments.get("destination"))
    if origin is None or destination is None:
        return _error(
            "город отправления или назначения не найден в демо-справочнике", "city_not_in_demo_directory"
        )
    outbound = _date(arguments, "date")
    if outbound is None:
        return _error("нужна дата отправления")
    adults = int(arguments.get("adults") or 0)
    if adults < 1:
        return _error("нужен состав группы: adults >= 1")
    offers = mock_transport(
        origin=origin,
        destination=destination,
        outbound_date=outbound,
        return_date=_date(arguments, "return_date"),
        adults=adults,
        children_ages=[int(age) for age in arguments.get("children_ages") or []],
        modes=[str(mode) for mode in arguments.get("modes") or []],
        generated_at=_now(),
        ttl_minutes=int(arguments.get("ttl_minutes") or 30),
    )
    return _ok(offers, "offers", "ДЕМО-данные: расписание и наличие не подтверждены")


def get_transfer_options(arguments: dict[str, Any]) -> AdapterResponse:
    city = normalize_city(arguments.get("city"))
    if city is None:
        return _error("город трансфера не найден в демо-справочнике", "city_not_in_demo_directory")
    directions = [str(item) for item in arguments.get("directions") or ["transfer_in", "transfer_out"]]
    offers = [
        mock_transfer(
            city=city,
            direction=direction,
            generated_at=_now(),
            ttl_minutes=int(arguments.get("ttl_minutes") or 30),
        )
        for direction in directions
    ]
    return _ok(
        offers,
        "offers",
        "время трансфера — демо-оценка; стоимость не подтверждена (DEC-18)",
        status="partial",
        missing=["стоимость трансфера"],
    )


def get_offer_details(arguments: dict[str, Any]) -> AdapterResponse:
    """ДЕМО-детализация ничего не подтверждает: доступность и тарифы остаются неизвестными."""
    if not arguments.get("offer_id"):
        return _error("нужен offer_id")
    return {
        "status": "empty",
        "data": {"offer_id": arguments["offer_id"]},
        "sources": [],
        "retrieved_at": None,
        "warnings": ["ДЕМО-детализация недоступна: подтверждений нет"],
    }


def suggest_destinations(arguments: dict[str, Any]) -> AdapterResponse:
    candidates = mock_destinations(
        generated_at=_now(),
        wishes=[str(tag) for tag in arguments.get("wishes") or []],
        exclude=arguments.get("exclude"),
        limit=int(arguments.get("limit") or 3),
    )
    return _ok(candidates, "candidates", "ДЕМО-каталог: климат и цены не загружены (DEC-12, DEC-28)")


MOCK_HANDLERS: dict[str, Callable[[dict[str, Any]], AdapterResponse]] = {
    "search_hotels": search_hotels,
    "search_transport": search_transport,
    "get_transfer_options": get_transfer_options,
    "get_offer_details": get_offer_details,
    "suggest_destinations": suggest_destinations,
}
