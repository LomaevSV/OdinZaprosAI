"""Модульные тесты ядра: деньги и полнота (PRICE-01), бюджет, условия и группы (CONSTRAINT-01, §2.21),
ранжирование RANK-01, доступность (AVAIL-01), стыки (CONNECTION-01/02), актуальность (DEC-21), пересборка."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest

from odin.contracts import Constraint, ConstraintGroup, FallbackActivation
from odin.core.constraints import (
    OfferFacts,
    combine_and,
    combine_or,
    evaluate_constraint,
    evaluate_group,
    fallback_activation,
    rank_class,
    rank_key,
    sort_by_rank,
)
from odin.core.money import CurrencyMixError, price_comparability, sum_prices
from odin.core.replan import REPLAN_TABLE, invalidated_roles
from odin.core.trip import (
    arrival_checkin_check,
    checkout_departure_check,
    evaluate_budget,
    judge_availability,
    offer_staleness,
    select_hotel_candidates,
)
from tests.samples import availability, hotel_offer, price

pytestmark = pytest.mark.unit


def constraint(
    field: str, operator: str, value: Any, scope: str, requiredness: str = "required"
) -> Constraint:
    return Constraint(
        constraint_id=f"c-{field}",
        field=field,  # type: ignore[arg-type]
        operator=operator,  # type: ignore[arg-type]
        value=value,
        requiredness=requiredness,  # type: ignore[arg-type]
        source_span=field,
        source_kind="form_field",
        scope=scope,  # type: ignore[arg-type]
        confidence=None,
        ambiguity=None,
    )


def complete(amount: str, currency: str = "RUB"):
    return price(
        amount=Decimal(amount),
        currency=currency,
        price_scope="stay_total",
        mandatory_fees_status="included",
        known_fees=[],
        total_completeness="complete",
        unknown_reason=None,
    )


# --------------------------------------------------------------------- деньги


@pytest.mark.req("PRICE-01", "T-UNIT-PRICE-27000")
def test_sum_of_complete_prices_is_exact_and_complete() -> None:
    totals = sum_prices([complete("19200.00"), complete("7800.00")], ["outbound", "hotel"])
    assert totals.amount == Decimal("27000.00")
    assert totals.currency == "RUB" and totals.completeness == "complete" and totals.unknown_reason is None


@pytest.mark.req("PRICE-01", "K-17")
def test_unknown_component_makes_total_partial_known_lower_bound() -> None:
    transfer = price(
        amount=None, currency=None, total_completeness="unknown", unknown_reason="not_provided_by_source"
    )
    totals = sum_prices([complete("27000.00"), transfer], ["hotel", "transfer_in"])
    assert totals.amount == Decimal("27000.00")
    assert totals.completeness == "partial"
    assert totals.partial_components == ("transfer_in",)


@pytest.mark.req("T-PRICE-COMPARE-02")
def test_unknown_mandatory_fee_is_never_complete() -> None:
    totals = sum_prices([price()], ["hotel"])  # образец: сборы unknown → partial
    assert totals.completeness == "partial"


@pytest.mark.negative
@pytest.mark.req("T-PRICE-COMPARE-01", "DEC-17")
def test_currencies_are_never_mixed() -> None:
    with pytest.raises(CurrencyMixError):
        sum_prices([complete("100.00", "RUB"), complete("10.00", "EUR")])
    assert price_comparability([complete("100.00", "RUB"), complete("10.00", "EUR")]) == "currency_unresolved"


def test_all_unknown_prices_give_unknown_total_and_empty_is_unknown() -> None:
    unknown = price(
        amount=None, currency=None, total_completeness="unknown", unknown_reason="not_provided_by_source"
    )
    assert sum_prices([unknown]).completeness == "unknown"
    assert sum_prices([unknown]).amount is None
    assert sum_prices([]).completeness == "unknown"


def test_price_comparability_classes() -> None:
    assert price_comparability([complete("1.00"), complete("2.00")]) == "comparable_complete"
    assert price_comparability([complete("1.00"), price()]) == "known_lower_bound"


# --------------------------------------------------------------------- бюджет


@pytest.mark.req("T-PRICE-COMPARE-03", "PRICE-01")
@pytest.mark.parametrize(
    ("known", "completeness", "expected"),
    [
        ("41000.00", "partial", "violated"),
        ("39000.00", "partial", "unknown"),
        ("39000.00", "complete", "matched"),
        (None, "unknown", "unknown"),
    ],
)
def test_budget_rules(known: str | None, completeness: str, expected: str) -> None:
    status, deviation, _ = evaluate_budget(
        Decimal("40000"), "RUB", None if known is None else Decimal(known), "RUB", completeness
    )
    assert status == expected
    if expected == "violated":
        assert deviation is not None and deviation.amount == Decimal("1000.00")
    else:
        assert deviation is None


def test_budget_in_other_currency_is_unknown() -> None:
    status, _, reason = evaluate_budget(Decimal("400"), "EUR", Decimal("100"), "RUB", "complete")
    assert status == "unknown" and "DEC-17" in reason


# --------------------------------------------------------------------- условия


@pytest.mark.req("CONSTRAINT-01", "K-07")
def test_unknown_is_not_not_applicable_and_not_matched() -> None:
    breakfast = constraint("hotel_breakfast", "eq", True, "hotel")
    unknown = evaluate_constraint(breakfast, OfferFacts(component_id="h", role="hotel"))
    assert unknown.status == "unknown" and unknown.unknown_reason == "not_provided_by_source"
    not_applicable = evaluate_constraint(breakfast, OfferFacts(component_id="t", role="outbound"))
    assert not_applicable.status == "not_applicable" and not_applicable.unknown_reason is None


@pytest.mark.req("CONSTRAINT-01")
@pytest.mark.parametrize(
    ("field", "operator", "value", "scope", "facts", "expected"),
    [
        ("hotel_stars_min", "gte", 4, "hotel", {"role": "hotel", "star_rating": 4}, "matched"),
        ("hotel_stars_min", "gte", 4, "hotel", {"role": "hotel", "star_rating": 3}, "violated"),
        ("hotel_breakfast", "eq", True, "hotel", {"role": "hotel", "breakfast_included": False}, "violated"),
        (
            "hotel_free_cancellation",
            "eq",
            True,
            "hotel",
            {"role": "hotel", "free_cancellation": True},
            "matched",
        ),
        (
            "transport_mode",
            "in",
            ["rail"],
            "transport",
            {"role": "outbound", "transport_mode": "rail"},
            "matched",
        ),
        (
            "transport_mode",
            "not_in",
            ["flight"],
            "transport",
            {"role": "outbound", "transport_mode": "flight"},
            "violated",
        ),
        ("transport_mode", "in", ["rail"], "transport", {"role": "outbound"}, "unknown"),
        ("pets", "eq", True, "trip", {"role": "hotel", "pets_allowed": True}, "matched"),
        ("pets", "eq", True, "trip", {"role": "outbound", "pets_allowed": None}, "unknown"),
        ("pets", "eq", True, "trip", {"role": "transfer_in", "pets_allowed": True}, "not_applicable"),
    ],
)
def test_each_evaluator_status(field, operator, value, scope, facts, expected) -> None:  # noqa: ANN001
    evaluation = evaluate_constraint(
        constraint(field, operator, value, scope), OfferFacts(component_id="x", **facts)
    )
    assert evaluation.status == expected
    assert evaluation.reason


@pytest.mark.req("T-CONSTRAINT-LOGIC-01", "T-CONSTRAINT-LOGIC-02", "T-CONSTRAINT-LOGIC-03")
def test_and_or_matrix() -> None:
    assert combine_and(["matched", "unknown"]) == "unknown"
    assert combine_and(["matched", "violated", "unknown"]) == "violated"
    assert combine_and(["matched", "not_applicable"]) == "matched"
    assert combine_or(["violated", "matched"]) == "matched"
    assert combine_or(["violated", "unknown"]) == "unknown"
    assert combine_or(["violated", "violated"]) == "violated"


def _group(
    operator: str, children: list[str], activation: FallbackActivation | None = None, raw: str | None = None
):
    return ConstraintGroup(
        group_id="g",
        operator=operator,
        children=children,
        scope="hotel",
        activation=activation,
        raw_text=raw,  # type: ignore[arg-type]
    )


@pytest.mark.req("T-CONSTRAINT-LOGIC-04", "FALLBACK-01")
def test_fallback_without_proven_activation_keeps_primary_branch() -> None:
    undetermined = fallback_activation(
        group_id="g",
        trigger="нет 4★",
        primary_status="unknown",
        search_completeness="incomplete",
        evidence=[],
    )
    assert undetermined.status == "undetermined"
    status, note = evaluate_group(
        _group("FALLBACK", ["c-4", "c-3"], undetermined), {"c-4": "unknown", "c-3": "matched"}
    )
    assert status == "unknown" and "не подменяется" in note

    activated = fallback_activation(
        group_id="g",
        trigger="нет 4★",
        primary_status="violated",
        search_completeness="complete",
        evidence=["s1"],
    )
    assert activated.status == "activated"
    status, note = evaluate_group(
        _group("FALLBACK", ["c-4", "c-3"], activated), {"c-4": "violated", "c-3": "matched"}
    )
    assert status == "matched" and "активирован" in note


def test_fallback_is_not_activated_when_primary_matched_or_search_incomplete() -> None:
    assert (
        fallback_activation(
            group_id="g",
            trigger="t",
            primary_status="matched",
            search_completeness="complete",
            evidence=["x"],
        ).status
        == "not_activated"
    )
    assert (
        fallback_activation(
            group_id="g",
            trigger="t",
            primary_status="violated",
            search_completeness="incomplete",
            evidence=["x"],
        ).status
        == "undetermined"
    )


def test_unparsed_group_is_kept_as_unknown() -> None:
    status, note = evaluate_group(_group("unparsed", [], raw="с видом на что-нибудь"), {})
    assert status == "unknown" and "с видом на что-нибудь" in note


# --------------------------------------------------------------------- ранжирование


@pytest.mark.req("RANK-01", "K-21", "DEC-26")
def test_rank_classes_and_stable_order_without_weights() -> None:
    assert rank_class({"r": "matched"}, ["r"]) == 1
    assert rank_class({"r": "unknown"}, ["r"]) == 2
    assert rank_class({"r": "violated", "p": "matched"}, ["r"]) == 3
    violated_but_many_prefs = rank_key("A", {"r": "violated", "p1": "matched", "p2": "matched"}, ["r"])
    unknown_required = rank_key("Z", {"r": "unknown"}, ["r"])
    tie_b = rank_key("B", {"r": "matched"}, ["r"])
    tie_a = rank_key("A2", {"r": "matched"}, ["r"])
    order = [
        key.canonical_id for key in sort_by_rank([violated_but_many_prefs, unknown_required, tie_b, tie_a])
    ]
    assert order == ["A2", "B", "Z", "A"]  # класс важнее предпочтений; равные — по каноническому ID


# --------------------------------------------------------------------- доступность


@pytest.mark.req("AVAIL-01", "DATA-MODE-02", "T-UNIT-REPLAY-01")
def test_availability_semantics_is_strict() -> None:
    assert judge_availability(availability()).status == "unknown"  # Mock
    now = datetime.now(tz=UTC)
    live_listing = availability(
        data_mode="live",
        evidence_level="search_listing",
        verification_status="verified",
        unknown_reason="not_provided_by_source",
    )
    assert judge_availability(live_listing).status == "unknown"
    confirmed = availability(
        data_mode="live",
        availability_status="available",
        evidence_level="offer_details",
        verification_status="verified",
        checked_at=now,
        unknown_reason=None,
    )
    assert judge_availability(confirmed).status == "available"
    refused = availability(
        data_mode="live",
        availability_status="unavailable",
        evidence_level="offer_details",
        verification_status="verified",
        checked_at=now,
        unknown_reason=None,
    )
    assert judge_availability(refused).status == "unknown"  # не доказан полный охват запроса
    assert judge_availability(refused, search_completeness="complete").status == "unavailable"
    replay = availability(data_mode="replay", evidence_level="historical", unknown_reason="stale")
    assert judge_availability(replay).status == "unknown"


@pytest.mark.req("T-HOTEL-SEARCH-01", "T-HOTEL-SEARCH-02")
def test_unknown_hotel_stays_candidate_and_unavailable_is_rejected() -> None:
    now = datetime.now(tz=UTC)
    first = hotel_offer().evolve(
        offer_id="first",
        availability=availability(
            data_mode="live",
            availability_status="unavailable",
            evidence_level="provider_checked",
            verification_status="verified",
            checked_at=now,
            unknown_reason=None,
        ),
        source=hotel_offer().source.evolve(
            source_type="mcp",
            provider="tutu_mcp",
            data_mode="live",
            retrieved_at=now,
            verification_status="verified",
        ),
    )
    second = hotel_offer().evolve(offer_id="second")
    confirmed, unconfirmed, rejected = select_hotel_candidates(
        [first, second], search_completeness="complete"
    )
    assert [o.offer_id for o in rejected] == ["first"]
    assert [o.offer_id for o in unconfirmed] == ["second"]
    assert confirmed == []


# --------------------------------------------------------------------- стыки


def _arrival(hour: int, minute: int = 0, day: int = 6) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.mark.req("CONNECTION-02", "K-16")
def test_early_arrival_is_not_a_violation_and_unapproved_buffer_gives_unknown() -> None:
    check = arrival_checkin_check(
        check_id="c",
        from_component="outbound",
        to_component="hotel",
        arrival_at=_arrival(6),
        check_in_date=date(2026, 11, 6),
        check_in_from=time(14, 0),
        timezone_name="Europe/Moscow",
        buffer_minutes=15,
        buffer_status="proposed_not_approved",
    )
    assert check.status == "unknown" and check.unknown_reason == "buffer_not_approved"
    assert check.margin_minutes == 480
    assert check.timezones == ["Europe/Moscow"] and "arrival_at" in check.times_used


@pytest.mark.req("CONNECTION-01", "K-18")
def test_arrival_after_check_in_day_is_violated() -> None:
    check = arrival_checkin_check(
        check_id="c",
        from_component="outbound",
        to_component="hotel",
        arrival_at=_arrival(1, day=7),
        check_in_date=date(2026, 11, 6),
        check_in_from=time(14, 0),
        timezone_name="Europe/Moscow",
        buffer_minutes=None,
        buffer_status="none",
    )
    assert check.status == "violated" and check.unknown_reason is None


def test_arrival_timezone_conversion_uses_hotel_zone() -> None:
    from zoneinfo import ZoneInfo

    arrival = datetime(2026, 11, 6, 20, 30, tzinfo=ZoneInfo("Asia/Yekaterinburg"))  # 18:30 по Москве
    check = arrival_checkin_check(
        check_id="c",
        from_component="outbound",
        to_component="hotel",
        arrival_at=arrival,
        check_in_date=date(2026, 11, 6),
        check_in_from=time(14, 0),
        timezone_name="Europe/Moscow",
        buffer_minutes=None,
        buffer_status="none",
    )
    assert check.margin_minutes == -270
    assert check.times_used["arrival_local"].startswith("2026-11-06T18:30")


def test_approved_buffer_requires_confirmed_transfer_time_for_matched() -> None:
    kwargs = dict(
        check_id="c",
        from_component="outbound",
        to_component="hotel",
        arrival_at=_arrival(10),
        check_in_date=date(2026, 11, 6),
        check_in_from=time(14, 0),
        timezone_name="Europe/Moscow",
        buffer_minutes=15,
        buffer_status="approved",
    )
    assert arrival_checkin_check(**kwargs).status == "unknown"
    assert arrival_checkin_check(**kwargs, transfer_minutes=40).status == "matched"


def test_missing_times_give_unknown_and_return_on_other_day_is_violated() -> None:
    missing = arrival_checkin_check(
        check_id="c",
        from_component="outbound",
        to_component="hotel",
        arrival_at=None,
        check_in_date=date(2026, 11, 6),
        check_in_from=time(14, 0),
        timezone_name="Europe/Moscow",
        buffer_minutes=None,
        buffer_status="none",
    )
    assert missing.status == "unknown" and missing.unknown_reason == "data_missing"
    early = checkout_departure_check(
        check_id="r",
        from_component="hotel",
        to_component="return",
        check_out_date=date(2026, 11, 8),
        check_out_until=time(12, 0),
        departure_at=_arrival(9, day=7),
        timezone_name="Europe/Moscow",
        buffer_minutes=None,
        buffer_status="none",
    )
    assert early.status == "violated"


# --------------------------------------------------------------------- актуальность и пересборка


@pytest.mark.req("DEC-21")
def test_offer_staleness_uses_provisional_ttl() -> None:
    observed = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert offer_staleness(observed, None, observed + timedelta(minutes=29))[0] is False
    assert offer_staleness(observed, None, observed + timedelta(minutes=31))[0] is True
    assert offer_staleness(observed, observed + timedelta(hours=2), observed + timedelta(hours=1))[0] is False


@pytest.mark.req("REPLAN-01", "T-UNIT-REPLAN-01")
def test_replan_table_as_data() -> None:
    roles, reasons = invalidated_roles({"date_from": "2026-11-13"})
    assert set(roles) == {"outbound", "return", "hotel", "transfer_in", "transfer_out"} and reasons
    roles, _ = invalidated_roles({"pets": [{"kind": "dog", "count": 1}]})
    assert set(roles) == {"outbound", "return", "hotel"}
    roles, _ = invalidated_roles({"budget_amount": "50000"})
    assert roles == []
    with pytest.raises(KeyError):
        invalidated_roles({"unknown_field": 1})
    assert {rule.group for rule in REPLAN_TABLE} >= {"dates", "party", "budget", "pets", "destination"}
