"""Доступность, бюджет, стыки, актуальность и покрытие (AVAIL-01, CONNECTION-01/02, COVERAGE-01, PRICE-01).

Все решения принимает код; LLM к ним не допускается (план §2.7).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from odin.contracts import Availability, ConnectionCheck, ConstraintEvaluation, HotelOffer, Price
from odin.contracts.common import Deviation, EvaluationStatus

SearchCompleteness = Literal["complete", "incomplete", "unknown"]
BufferStatus = Literal["approved", "proposed_not_approved", "none"]

# --------------------------------------------------------------------- доступность


@dataclass(frozen=True, slots=True)
class AvailabilityVerdict:
    """Вывод о доступности по строгой семантике AVAIL-01 (план §2.9, §2.19)."""

    status: Literal["available", "unavailable", "unknown"]
    evidence_level: str
    reason: str


def judge_availability(
    availability: Availability, *, search_completeness: SearchCompleteness = "unknown"
) -> AvailabilityVerdict:
    """Поисковая выдача, Replay и Mock не доказывают наличие; отказ тоже требует основания."""
    level = availability.evidence_level
    if availability.data_mode == "mock":
        return AvailabilityVerdict("unknown", level, "ДЕМО-данные не подтверждают наличие")
    if availability.data_mode == "replay":
        return AvailabilityVerdict("unknown", level, "Replay не подтверждает текущее наличие (DATA-MODE-02)")
    strong = level in ("offer_details", "provider_checked")
    if availability.availability_status == "available":
        if strong and availability.checked_at is not None:
            return AvailabilityVerdict("available", level, "источник подтвердил наличие на даты и состав")
        return AvailabilityVerdict("unknown", level, "поисковая запись не является доказательством наличия")
    if availability.availability_status == "unavailable":
        if strong and search_completeness == "complete":
            return AvailabilityVerdict(
                "unavailable", level, "источник явно подтвердил отсутствие для полного запроса"
            )
        return AvailabilityVerdict("unknown", level, "отрицательный ответ не покрывает полный запрос")
    return AvailabilityVerdict("unknown", level, "наличие не подтверждено источником")


def select_hotel_candidates(
    offers: Sequence[HotelOffer], *, search_completeness: SearchCompleteness = "unknown"
) -> tuple[list[HotelOffer], list[HotelOffer], list[HotelOffer]]:
    """Подтверждённые, неподтверждённые (остаются кандидатами) и отвергнутые отели.

    План §2.20 п. 4: при ``unavailable`` берётся следующий отель; при ``unknown`` поиск не прекращается,
    отель сохраняется как неподтверждённая альтернатива.
    """
    confirmed: list[HotelOffer] = []
    unconfirmed: list[HotelOffer] = []
    rejected: list[HotelOffer] = []
    for offer in offers:
        verdict = judge_availability(offer.availability, search_completeness=search_completeness)
        {"available": confirmed, "unavailable": rejected, "unknown": unconfirmed}[verdict.status].append(
            offer
        )
    return confirmed, unconfirmed, rejected


# --------------------------------------------------------------------- бюджет


def evaluate_budget(
    budget_amount: Decimal,
    budget_currency: str,
    known_amount: Decimal | None,
    known_currency: str | None,
    completeness: str,
) -> tuple[EvaluationStatus, Deviation | None, str]:
    """План §2.23: известная нижняя граница больше бюджета → ``violated``; при неполной цене — ``unknown``."""
    if known_amount is None:
        return "unknown", None, "известная часть стоимости неизвестна"
    if known_currency != budget_currency:
        return (
            "unknown",
            None,
            "валюта итога отличается от валюты бюджета: конвертация не утверждена (DEC-17)",
        )
    if known_amount > budget_amount:
        deviation = Deviation(
            amount=known_amount - budget_amount,
            unit=budget_currency,
            description="известная нижняя граница превышает бюджет",
        )
        return "violated", deviation, "известная часть стоимости больше бюджета"
    if completeness == "complete":
        return "matched", None, "полная стоимость в пределах бюджета"
    return "unknown", None, "известная часть в бюджете, но не все обязательные расходы известны"


# --------------------------------------------------------------------- актуальность


def offer_staleness(
    observed_at: datetime, valid_until: datetime | None, now: datetime, ttl_minutes: int = 30
) -> tuple[bool, str]:
    """Актуальность оффера (DEC-21, provisional): без срока источника — осторожный TTL."""
    if valid_until is not None:
        stale = now > valid_until
        return stale, "истёк срок источника" if stale else "срок источника не истёк"
    stale = now > observed_at + timedelta(minutes=ttl_minutes)
    if stale:
        return True, f"истёк provisional TTL {ttl_minutes} мин (DEC-21)"
    return False, f"в пределах provisional TTL {ttl_minutes} мин (DEC-21)"


# --------------------------------------------------------------------- стыки


def _elapsed_minutes(start: datetime, end: datetime) -> int:
    """Реально прошедшие минуты. Вычитание в одной зоне ZoneInfo в Python идёт по «настенному» времени
    и игнорирует переход на летнее время, поэтому разность считается в UTC."""
    return int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 60)


def _check(
    status: EvaluationStatus,
    *,
    check_id: str,
    from_component: str,
    to_component: str,
    basis: str,
    times_used: dict[str, str],
    timezone_name: str | None,
    buffer_minutes: int | None,
    buffer_status: BufferStatus,
    margin_minutes: int | None,
    unknown_reason: str | None = None,
    sources: Sequence[str] = (),
) -> ConnectionCheck:
    return ConnectionCheck(
        check_id=check_id,
        from_component=from_component,
        to_component=to_component,
        status=status,
        basis=basis,
        times_used=times_used,
        timezones=[timezone_name] if timezone_name else [],
        buffer_minutes=buffer_minutes,
        buffer_status=buffer_status,
        margin_minutes=margin_minutes,
        sources=list(sources),
        unknown_reason=unknown_reason if status == "unknown" else None,
    )


def arrival_checkin_check(
    *,
    check_id: str,
    from_component: str,
    to_component: str,
    arrival_at: datetime | None,
    check_in_date: date,
    check_in_from: time | None,
    timezone_name: str | None,
    buffer_minutes: int | None,
    buffer_status: BufferStatus,
    transfer_minutes: int | None = None,
    sources: Sequence[str] = (),
) -> ConnectionCheck:
    """Стык «прибытие → заезд» (CONNECTION-01/02).

    - нет времени прибытия, времени заезда или пояса → ``unknown`` (``data_missing``);
    - прибытие в другой календарный день, чем заезд → ``violated`` (ночь без размещения или пропуск ночи);
    - буфер не утверждён (DEC-20) → ``unknown`` (``buffer_not_approved``), расчёт показывается;
    - буфер утверждён: готовность (прибытие + буфер + трансфер) в день заезда → ``matched``, иначе ``violated``.
    Раннее прибытие — не нарушение: ``margin_minutes`` > 0 означает ожидание до начала заезда.
    """
    times: dict[str, str] = {"check_in_date": check_in_date.isoformat()}
    if arrival_at is not None:
        times["arrival_at"] = arrival_at.isoformat()
    if check_in_from is not None:
        times["check_in_from"] = check_in_from.isoformat(timespec="minutes")
    common = {
        "check_id": check_id,
        "from_component": from_component,
        "to_component": to_component,
        "times_used": times,
        "timezone_name": timezone_name,
        "buffer_minutes": buffer_minutes,
        "buffer_status": buffer_status,
        "sources": sources,
    }
    if arrival_at is None or check_in_from is None or timezone_name is None:
        missing = [
            label
            for label, value in (
                ("время прибытия", arrival_at),
                ("время заезда", check_in_from),
                ("часовой пояс", timezone_name),
            )
            if value is None
        ]
        return _check(
            "unknown",
            basis="нет данных: " + ", ".join(missing),
            margin_minutes=None,
            unknown_reason="data_missing",
            **common,
        )

    zone = ZoneInfo(timezone_name)
    local_arrival = arrival_at.astimezone(zone)
    check_in_start = datetime.combine(check_in_date, check_in_from, tzinfo=zone)
    margin = _elapsed_minutes(local_arrival, check_in_start)
    times["arrival_local"] = local_arrival.isoformat()

    if local_arrival.date() != check_in_date:
        relation = "позже" if local_arrival.date() > check_in_date else "раньше"
        return _check(
            "violated",
            basis=f"прибытие {local_arrival.date().isoformat()} {relation} даты заезда {check_in_date.isoformat()}",
            margin_minutes=margin,
            **common,
        )
    wait_note = f"ожидание до заезда {margin} мин" if margin > 0 else "заезд возможен по прибытии"
    if buffer_minutes is None or buffer_status != "approved":
        return _check(
            "unknown",
            basis=f"{wait_note}; буфер не утверждён (DEC-20)",
            margin_minutes=margin,
            unknown_reason="buffer_not_approved",
            **common,
        )
    if transfer_minutes is None:
        return _check(
            "unknown",
            basis=f"{wait_note}; время трансфера не подтверждено",
            margin_minutes=margin,
            unknown_reason="data_missing",
            **common,
        )
    ready = (local_arrival.astimezone(UTC) + timedelta(minutes=buffer_minutes + transfer_minutes)).astimezone(
        zone
    )
    if ready.date() != check_in_date:
        return _check(
            "violated",
            basis="с учётом буфера и трансфера прибытие в отель переходит на следующие сутки",
            margin_minutes=margin,
            **common,
        )
    return _check("matched", basis=f"{wait_note}; буфер и трансфер учтены", margin_minutes=margin, **common)


def checkout_departure_check(
    *,
    check_id: str,
    from_component: str,
    to_component: str,
    check_out_date: date,
    check_out_until: time | None,
    departure_at: datetime | None,
    timezone_name: str | None,
    buffer_minutes: int | None,
    buffer_status: BufferStatus,
    sources: Sequence[str] = (),
) -> ConnectionCheck:
    """Стык «выезд → обратный путь». Отправление в другой день, чем выезд, — доказанный конфликт."""
    times: dict[str, str] = {"check_out_date": check_out_date.isoformat()}
    if departure_at is not None:
        times["departure_at"] = departure_at.isoformat()
    if check_out_until is not None:
        times["check_out_until"] = check_out_until.isoformat(timespec="minutes")
    common = {
        "check_id": check_id,
        "from_component": from_component,
        "to_component": to_component,
        "times_used": times,
        "timezone_name": timezone_name,
        "buffer_minutes": buffer_minutes,
        "buffer_status": buffer_status,
        "sources": sources,
    }
    if departure_at is None or timezone_name is None:
        return _check(
            "unknown",
            basis="нет времени отправления или пояса",
            margin_minutes=None,
            unknown_reason="data_missing",
            **common,
        )
    local_departure = departure_at.astimezone(ZoneInfo(timezone_name))
    times["departure_local"] = local_departure.isoformat()
    if local_departure.date() != check_out_date:
        relation = "раньше" if local_departure.date() < check_out_date else "позже"
        return _check(
            "violated",
            basis=f"отправление {local_departure.date().isoformat()} {relation} даты выезда {check_out_date.isoformat()}",
            margin_minutes=None,
            **common,
        )
    margin = None
    if check_out_until is not None:
        checkout = datetime.combine(check_out_date, check_out_until, tzinfo=ZoneInfo(timezone_name))
        margin = _elapsed_minutes(checkout, local_departure)
    return _check(
        "unknown",
        basis="отправление в день выезда; буфер до отправления не утверждён (DEC-20)",
        margin_minutes=margin,
        unknown_reason="buffer_not_approved",
        **common,
    )


# --------------------------------------------------------------------- покрытие и полнота


def coverage_ok(constraints: Sequence[object], evaluations: Sequence[ConstraintEvaluation]) -> bool:
    """COVERAGE-01: у каждого применимого условия есть оценка."""
    evaluated = {item.constraint_id for item in evaluations}
    return all(getattr(constraint, "constraint_id", None) in evaluated for constraint in constraints)


def evaluate_completeness(prices: Sequence[Price]) -> str:
    """Сводная полнота: ``complete`` только если полны все компоненты (PRICE-01)."""
    if not prices:
        return "unknown"
    if all(price.total_completeness == "complete" for price in prices):
        return "complete"
    if any(price.amount is not None for price in prices):
        return "partial"
    return "unknown"
