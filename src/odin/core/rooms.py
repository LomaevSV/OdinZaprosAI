"""Комплект номеров (HOTEL-01) и цена за весь срок (PRICE-01, TEST-05).

- `available` для комплекта — только при подтверждённой **совместной** доступности; независимые
  подтверждения отдельных номеров совместность не доказывают → `unknown`.
- Доказанная невозможность хотя бы одного необходимого номера → `unavailable`.
- Цена комплекта — сумма всех номеров за весь срок плюс известные обязательные сборы; неизвестные
  обязательные сборы делают итог `partial`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from odin.contracts import Fee, Price, PricePeriod
from odin.core.money import CurrencyMixError

AvailabilityStatus = str


@dataclass(frozen=True, slots=True)
class RoomOption:
    """Один номер комплекта: цена за ночь или за весь срок, вместимость, доступность."""

    room_id: str
    capacity: int | None
    price: Price
    availability: AvailabilityStatus  # available | unavailable | unknown (уже вычислено по AVAIL-01)


@dataclass(frozen=True, slots=True)
class RoomSet:
    rooms: tuple[RoomOption, ...]
    availability: AvailabilityStatus
    availability_reason: str
    capacity_status: str  # matched | violated | unknown
    price: Price


def nights_between(check_in: date, check_out: date) -> int:
    nights = (check_out - check_in).days
    if nights <= 0:
        raise ValueError("выезд должен быть позже заезда")
    return nights


def room_stay_price(price: Price, nights: int, period: PricePeriod) -> Price:
    """Приводит цену номера к сроку проживания: `per_night` × ночи; `per_room_stay` — без изменений."""
    if price.price_scope == "per_room_stay":
        return price
    if price.price_scope != "per_night":
        raise ValueError(f"цена номера с областью {price.price_scope} не приводится к сроку проживания")
    amount = None if price.amount is None else (price.amount * nights).quantize(Decimal("0.01"))
    return price.evolve(price_scope="per_room_stay", amount=amount, period=period)


def joint_availability(
    statuses: Sequence[AvailabilityStatus], *, jointly_confirmed: bool
) -> tuple[AvailabilityStatus, str]:
    if any(status == "unavailable" for status in statuses):
        return "unavailable", "один из необходимых номеров недоступен — комплект невозможен"
    if statuses and all(status == "available" for status in statuses):
        if jointly_confirmed:
            return "available", "источник подтвердил совместную доступность комплекта"
        return "unknown", "номера подтверждены по отдельности, совместная доступность не подтверждена"
    return "unknown", "доступность хотя бы одного номера не подтверждена"


def build_room_set(
    rooms: Sequence[RoomOption],
    *,
    check_in: date,
    check_out: date,
    guests: int | None,
    jointly_confirmed: bool,
    mandatory_fees: Sequence[Fee] = (),
    fees_status: str = "unknown",
) -> RoomSet:
    """Комплект номеров на весь срок. `fees_status` — статус обязательных сборов источника."""
    if not rooms:
        raise ValueError("комплект без номеров")
    nights = nights_between(check_in, check_out)
    period = PricePeriod(start=check_in, end=check_out)
    stay_prices = [room_stay_price(room.price, nights, period) for room in rooms]

    currencies = {price.currency for price in stay_prices if price.currency} | {
        fee.currency for fee in mandatory_fees if fee.currency and fee.amount is not None
    }
    if len(currencies) > 1:
        raise CurrencyMixError(f"разные валюты в комплекте: {sorted(currencies)} (DEC-17)")
    currency = next(iter(currencies), None)

    unknown_room = any(price.amount is None for price in stay_prices)
    total = sum((price.amount for price in stay_prices if price.amount is not None), Decimal("0.00"))
    known_fees = [fee for fee in mandatory_fees if fee.amount is not None and fee.included is not True]
    total += sum((fee.amount for fee in known_fees if fee.amount is not None), Decimal("0.00"))
    unknown_fee = any(fee.amount is None and fee.included is not True for fee in mandatory_fees)

    if unknown_room and total == 0:
        completeness, amount, reason = "unknown", None, "not_provided_by_source"
    elif unknown_room or unknown_fee or fees_status == "unknown":
        completeness, amount, reason = "partial", total, "price_incomplete"
    else:
        completeness, amount, reason = "complete", total, None
    fees_field = "unknown" if (unknown_fee or fees_status == "unknown") else fees_status

    price = Price(
        amount=amount,
        currency=currency,
        price_scope="stay_total",
        guests=guests,
        rooms=len(rooms),
        period=period,
        inclusions=sorted({item for price in stay_prices for item in price.inclusions}),
        known_fees=list(mandatory_fees),
        mandatory_fees_status=fees_field,  # type: ignore[arg-type]
        total_completeness=completeness,  # type: ignore[arg-type]
        unknown_reason=reason,  # type: ignore[arg-type]
        source=stay_prices[0].source,
        as_of=min(price.as_of for price in stay_prices),
    )
    availability, availability_reason = joint_availability(
        [room.availability for room in rooms], jointly_confirmed=jointly_confirmed
    )
    capacities = [room.capacity for room in rooms]
    if guests is None or any(capacity is None for capacity in capacities):
        capacity_status = "unknown"
    else:
        capacity_status = "matched" if sum(c for c in capacities if c) >= guests else "violated"
    return RoomSet(
        rooms=tuple(rooms),
        availability=availability,
        availability_reason=availability_reason,
        capacity_status=capacity_status,
        price=price,
    )
