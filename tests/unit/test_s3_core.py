"""S3: TEST-05 (9 пунктов), календарь INTERPRET-02, HOTEL-01, дерево условий, COVERAGE-01, часовые пояса и DST.

Каждый пункт TEST-05 помечен маркером `req("TEST-05.N")`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from odin.contracts import Constraint, ConstraintEvaluation, ConstraintGroup, Fee
from odin.core.calendar import resolve_date_expression
from odin.core.constraints import fallback_activation
from odin.core.money import sum_prices
from odin.core.rooms import RoomOption, build_room_set, joint_availability
from odin.core.tree import coverage, evaluate_tree, roots_of, tradeoffs
from odin.core.trip import arrival_checkin_check, judge_availability
from odin.tools.replay import ReplayStore
from odin.tools.router import ModeRouter
from tests.samples import availability, price

pytestmark = pytest.mark.unit

CHECK_IN, CHECK_OUT = date(2026, 11, 20), date(2026, 11, 23)  # 3 ночи


def nightly(
    amount: str | None, *, availability_status: str = "available", capacity: int | None = 2
) -> RoomOption:
    value = None if amount is None else Decimal(amount)
    return RoomOption(
        room_id=f"room-{amount}",
        capacity=capacity,
        price=price(
            amount=value,
            currency="RUB" if value is not None else None,
            price_scope="per_night",
            period=None,
            known_fees=[],
            mandatory_fees_status="not_applicable",
            total_completeness="partial",
            unknown_reason="price_incomplete",
        ),
        availability=availability_status,
    )


# --------------------------------------------------------------------- TEST-05


@pytest.mark.req("TEST-05.1", "T-UNIT-PRICE-27000", "PRICE-01", "HOTEL-01")
def test_two_rooms_5000_and_4000_for_three_nights_is_27000() -> None:
    room_set = build_room_set(
        [nightly("5000.00"), nightly("4000.00")],
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        guests=4,
        jointly_confirmed=True,
        fees_status="not_applicable",
    )
    assert room_set.price.amount == Decimal("27000.00")
    assert room_set.price.currency == "RUB" and room_set.price.price_scope == "stay_total"
    assert room_set.price.rooms == 2 and room_set.price.total_completeness == "complete"
    assert room_set.availability == "available" and room_set.capacity_status == "matched"


@pytest.mark.req("TEST-05.2", "HOTEL-01")
def test_one_room_unavailable_makes_set_unavailable() -> None:
    room_set = build_room_set(
        [nightly("5000.00"), nightly("4000.00", availability_status="unavailable")],
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        guests=4,
        jointly_confirmed=False,
    )
    assert room_set.availability == "unavailable"


@pytest.mark.req("TEST-05.3", "HOTEL-01")
def test_independent_but_not_joint_availability_is_unknown() -> None:
    status, reason = joint_availability(["available", "available"], jointly_confirmed=False)
    assert status == "unknown" and "совместная" in reason
    assert joint_availability(["available", "unknown"], jointly_confirmed=True)[0] == "unknown"


@pytest.mark.req("TEST-05.4", "PRICE-01")
def test_complete_and_incomplete_totals() -> None:
    complete = build_room_set(
        [nightly("5000.00")],
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        guests=2,
        jointly_confirmed=True,
        fees_status="included",
    )
    assert complete.price.total_completeness == "complete"
    unknown_fee = build_room_set(
        [nightly("5000.00")],
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        guests=2,
        jointly_confirmed=True,
        mandatory_fees=[Fee(name="туристический налог", amount=None, currency="RUB", included=None)],
        fees_status="excluded_known",
    )
    assert unknown_fee.price.total_completeness == "partial" and unknown_fee.price.amount == Decimal(
        "15000.00"
    )
    known_fee = build_room_set(
        [nightly("5000.00")],
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        guests=2,
        jointly_confirmed=True,
        mandatory_fees=[Fee(name="сбор", amount=Decimal("300.00"), currency="RUB", included=False)],
        fees_status="excluded_known",
    )
    assert known_fee.price.amount == Decimal("15300.00") and known_fee.price.total_completeness == "complete"
    missing_room = build_room_set(
        [nightly("5000.00"), nightly(None)],
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        guests=4,
        jointly_confirmed=False,
        fees_status="included",
    )
    assert missing_room.price.total_completeness == "partial" and missing_room.price.amount == Decimal(
        "15000.00"
    )


@pytest.mark.req("TEST-05.5", "DATA-MODE-02")
def test_replay_availability_is_unknown() -> None:
    replay = availability(data_mode="replay", evidence_level="historical", unknown_reason="stale")
    assert judge_availability(replay).status == "unknown"
    with pytest.raises(ValueError):
        availability(data_mode="replay", availability_status="available", unknown_reason=None)


@pytest.mark.req("TEST-05.6", "DATA-MODE-03")
def test_live_to_replay_only_with_matching_parameters(tmp_path: Path) -> None:
    args = {"city": "Казань", "check_in": "2026-11-20", "check_out": "2026-11-23", "adults": 2}
    store = ReplayStore(tmp_path)
    ModeRouter(provider="p", tool="search_hotels", live=lambda a: {"status": "ok", "data": [1]}, store=store)(
        args
    )

    def failing(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "timeout", "error_code": "timeout"}

    router = ModeRouter(provider="p", tool="search_hotels", live=failing, store=store)
    assert router(args)["data_mode"] == "replay"
    assert router({**args, "adults": 3})["status"] == "unavailable"


def _constraint(cid: str, field: str = "hotel_breakfast", requiredness: str = "required") -> Constraint:
    return Constraint(
        constraint_id=cid,
        field=field,  # type: ignore[arg-type]
        operator="eq" if field != "hotel_stars_min" else "gte",  # type: ignore[arg-type]
        value=True if field != "hotel_stars_min" else 4,
        requiredness=requiredness,  # type: ignore[arg-type]
        source_span=cid,
        source_kind="form_field",
        scope="hotel",
        confidence=None,
        ambiguity=None,
    )


def _evaluation(cid: str, status: str) -> ConstraintEvaluation:
    return ConstraintEvaluation(
        constraint_id=cid,
        component_id="h1",
        plan_id=None,
        status=status,  # type: ignore[arg-type]
        unknown_reason="not_provided_by_source" if status == "unknown" else None,
        reason="тест",
        deviation=None,
    )


def _group(gid: str, operator: str, children: list[str], activation: Any = None, raw: str | None = None):
    return ConstraintGroup(
        group_id=gid,
        operator=operator,
        children=children,
        scope="hotel",
        activation=activation,
        raw_text=raw,  # type: ignore[arg-type]
    )


@pytest.mark.req("TEST-05.7", "INTERPRET-01", "K-08", "K-09", "K-10")
def test_and_or_fallback_conflicts_in_nested_tree() -> None:
    # «(4★ ИЛИ апартаменты с кухней) И завтрак; если нет 4★ — 3★ с завтраком»
    evaluations = [
        _evaluation("c-4star", "unknown"),
        _evaluation("c-kitchen", "matched"),
        _evaluation("c-breakfast", "violated"),
        _evaluation("c-3star", "matched"),
    ]
    undetermined = fallback_activation(
        group_id="g-fb",
        trigger="нет 4★",
        primary_status="unknown",
        search_completeness="incomplete",
        evidence=[],
    )
    groups = [
        _group("g-or", "OR", ["c-4star", "c-kitchen"]),
        _group("g-and", "AND", ["g-or", "c-breakfast"]),
        _group("g-fb", "FALLBACK", ["c-4star", "c-3star"], activation=undetermined),
        _group("g-raw", "unparsed", [], raw="чтобы было уютно"),
    ]
    result = evaluate_tree(groups, evaluations)
    assert result.group_status["g-or"] == "matched"
    assert result.group_status["g-and"] == "violated"  # OR выполнен, но завтрак нарушен
    assert result.group_status["g-fb"] == "unknown"  # основная ветвь не подменена резервной
    assert result.inactive_branches["c-3star"].startswith("резервная ветвь не активирована")
    assert result.group_status["g-raw"] == "unknown" and "уютно" in result.notes["g-raw"]
    assert set(roots_of(groups)) == {"g-and", "g-fb", "g-raw"}


def test_tree_cycle_and_dangling_reference_are_unknown() -> None:
    groups = [_group("a", "AND", ["b", "c-x"]), _group("b", "AND", ["a", "c-x"])]
    result = evaluate_tree(groups, [_evaluation("c-x", "matched")])
    assert result.group_status["a"] == "unknown" or result.group_status["b"] == "unknown"
    dangling = evaluate_tree([_group("d", "OR", ["c-missing", "c-y"])], [_evaluation("c-y", "violated")])
    assert dangling.group_status["d"] == "unknown"


@pytest.mark.req("TEST-05.8", "FALLBACK-01", "SEARCH-STOP-01")
def test_timeout_never_proves_empty_search(tmp_path: Path) -> None:
    from odin.app import build_application
    from odin.config import Settings
    from odin.contracts.api import StructuredForm
    from tests.conftest import trip_form

    app = build_application(Settings.from_env({"ODIN_DB_PATH": str(tmp_path / "t.sqlite3")}))

    def timeout_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "timeout", "error_code": "timeout", "data": None, "sources": []}

    app.registry.handlers["search_hotels"] = timeout_handler
    session = app.orchestrator.create_session()
    result = app.orchestrator.handle_message(
        session_id=session["session_id"],
        request_id="req-timeout-01",
        trip_revision=1,
        form=StructuredForm.model_validate(trip_form()),
    )
    termination = result.state.search_termination
    assert termination is not None and termination.reason == "provider_unavailable"
    assert termination.reason != "sources_exhausted"
    assert any(item.source == "odin_mock:search_hotels" for item in termination.unchecked_sources)
    text = result.assistant_text.replace("Это не означает, что вариантов нет", "")
    assert "вариантов нет" not in text and "не найден" in " ".join(result.state.plan.unknowns)  # type: ignore[union-attr]


@pytest.mark.req("TEST-05.9", "UC-09", "REPLAN-02")
def test_cart_is_repriced_after_replan(client: Any) -> None:
    from tests.conftest import trip_form

    session = client.post("/api/v1/sessions").json()
    # Вид транспорта фиксирован: при равных классах RANK-01 порядок задаётся каноническим ID (DEC-26),
    # который зависит от даты, — без фиксации сравнивались бы разные виды транспорта.
    turn = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={
            "request_id": "req-reprice-01",
            "trip_revision": 1,
            "form": trip_form(transport_modes=["rail"]),
        },
    ).json()
    cart_one = client.post(
        f"/api/v1/trips/{turn['trip_id']}/cart",
        json={"request_id": "req-reprice-02", "trip_revision": turn["trip_revision"]},
    ).json()
    replanned = client.post(
        f"/api/v1/trips/{turn['trip_id']}/replan",
        json={
            "request_id": "req-reprice-03",
            "trip_revision": cart_one["trip_revision"],
            "change_set": {"adults": 3},
        },
    ).json()
    assert replanned["state"]["cart"] is None
    cart_two = client.post(
        f"/api/v1/trips/{turn['trip_id']}/cart",
        json={"request_id": "req-reprice-04", "trip_revision": replanned["trip_revision"]},
    ).json()
    assert cart_two["cart"]["cart_id"] != cart_one["cart"]["cart_id"]
    first_total = Decimal(cart_one["cart"]["total_price"]["amount"])
    second_total = Decimal(cart_two["cart"]["total_price"]["amount"])
    assert second_total > first_total  # три человека дороже двух: цена пересчитана, а не перенесена


# --------------------------------------------------------------------- календарь INTERPRET-02

REF = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)  # четверг; в Москве 12:00


@pytest.mark.req("INTERPRET-02", "K-11", "T-UNIT-CAL-01")
@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("20–22 ноября", date(2026, 11, 20), date(2026, 11, 22)),
        ("с 5 января", date(2027, 1, 5), None),
        ("завтра", date(2026, 10, 23), None),
        ("послезавтра", date(2026, 10, 24), None),
        ("через две недели", date(2026, 11, 5), None),
        ("через 3 дня", date(2026, 10, 25), None),
        ("на выходные", date(2026, 10, 24), date(2026, 10, 25)),
        ("в субботу", date(2026, 10, 24), None),
        ("2026-12-31", date(2026, 12, 31), None),
        ("15.11", date(2026, 11, 15), None),
    ],
)
def test_calendar_resolves_with_rule(phrase: str, start: date, end: date | None) -> None:
    expression, _ = resolve_date_expression(phrase, REF, "Europe/Moscow")
    assert expression.status == "resolved", expression.rule
    assert (expression.resolved_start, expression.resolved_end) == (start, end)
    assert expression.rule and expression.raw_text == phrase and expression.reference_datetime == REF


@pytest.mark.req("INTERPRET-02", "K-13", "T-UNIT-CAL-02")
def test_calendar_does_not_invent_dates() -> None:
    ambiguous, resolution = resolve_date_expression("в следующие выходные", REF, "Europe/Moscow")
    assert ambiguous.status == "ambiguous" and ambiguous.resolved_start is None
    assert len(resolution.candidates) == 2
    for phrase in ("когда-нибудь потом", "31 февраля", "30.13"):
        expression, _ = resolve_date_expression(phrase, REF, "Europe/Moscow")
        assert expression.status == "unresolved" and expression.resolved_start is None
    # DATE-003: «летом» — период (несколько толкований до выбора), конкретная дата не выдумывается.
    summer, _ = resolve_date_expression("когда-нибудь летом", REF, "Europe/Moscow")
    assert summer.status == "ambiguous" and summer.resolved_start is None and summer.resolved_end is None
    no_zone, _ = resolve_date_expression("завтра", REF, None)
    assert no_zone.status == "unresolved" and "DEC-34" in no_zone.rule


def test_calendar_uses_local_date_of_timezone() -> None:
    late_utc = datetime(2026, 10, 22, 22, 30, tzinfo=UTC)  # в Москве уже 23 октября
    moscow, _ = resolve_date_expression("завтра", late_utc, "Europe/Moscow")
    kaliningrad, _ = resolve_date_expression("завтра", late_utc, "Europe/Kaliningrad")
    assert moscow.resolved_start == date(2026, 10, 24)
    assert kaliningrad.resolved_start == date(2026, 10, 24)
    novosibirsk, _ = resolve_date_expression(
        "завтра", datetime(2026, 10, 22, 17, 30, tzinfo=UTC), "Asia/Novosibirsk"
    )
    assert novosibirsk.resolved_start == date(2026, 10, 24)


def test_next_weekend_on_friday_is_resolved() -> None:
    friday = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    expression, _ = resolve_date_expression("следующие выходные", friday, "Europe/Moscow")
    assert expression.status == "resolved" and expression.resolved_start == date(2026, 10, 31)


# --------------------------------------------------------------------- пояса, DST, стыки


@pytest.mark.req("T-UNIT-TZ-01", "CONNECTION-02")
def test_dst_transition_and_cross_zone_connection() -> None:
    """Переход на летнее время: Берлин, 29.03.2026 02:00 → 03:00. Минуты считаются по реальным моментам."""
    berlin = ZoneInfo("Europe/Berlin")
    arrival = datetime(2026, 3, 29, 1, 30, tzinfo=berlin)  # до перехода, UTC+1
    check = arrival_checkin_check(
        check_id="dst",
        from_component="outbound",
        to_component="hotel",
        arrival_at=arrival,
        check_in_date=date(2026, 3, 29),
        check_in_from=time(4, 0),  # после перехода, UTC+2
        timezone_name="Europe/Berlin",
        buffer_minutes=None,
        buffer_status="none",
    )
    assert check.margin_minutes == 90  # 1:30 → 4:00 по стене = 2,5 ч, но реально 1,5 ч
    eastern = datetime(2026, 11, 20, 23, 30, tzinfo=ZoneInfo("Asia/Yekaterinburg"))  # 21:30 МСК
    cross = arrival_checkin_check(
        check_id="x",
        from_component="outbound",
        to_component="hotel",
        arrival_at=eastern,
        check_in_date=date(2026, 11, 20),
        check_in_from=time(14, 0),
        timezone_name="Europe/Moscow",
        buffer_minutes=None,
        buffer_status="none",
    )
    assert cross.status == "unknown" and cross.margin_minutes == -450
    assert set(cross.times_used) >= {"arrival_at", "arrival_local", "check_in_from"}


# --------------------------------------------------------------------- покрытие и компромиссы


@pytest.mark.req("COVERAGE-01", "T-UNIT-COV-01")
def test_coverage_reports_missing_and_required_problems() -> None:
    constraints = [_constraint("c-a"), _constraint("c-b"), _constraint("c-c", requiredness="preferred")]
    report = coverage(constraints, [_evaluation("c-a", "violated"), _evaluation("c-c", "unknown")])
    assert report.missing == ("c-b",) and not report.complete
    assert report.violated_required == ("c-a",) and report.unknown_required == ()
    full = coverage(constraints, [_evaluation(c.constraint_id, "matched") for c in constraints])
    assert full.complete


@pytest.mark.req("UC-06", "RANK-01")
def test_tradeoffs_are_computed_from_statuses_only() -> None:
    rows = tradeoffs(
        {
            "h2": [_evaluation("c-a", "matched"), _evaluation("c-b", "unknown")],
            "h1": [_evaluation("c-a", "violated"), _evaluation("c-b", "matched")],
        },
        ["c-a", "c-b"],
    )
    assert [row.candidate_id for row in rows] == ["h1", "h2"]
    assert rows[0].worse == ("c-a",) and rows[0].better == ("c-b",)
    assert "не подтверждено: c-b" in rows[1].text()


@pytest.mark.req("RANK-01", "K-20")
@pytest.mark.parametrize("count", [0, 1, 5])
def test_zero_one_many_prices(count: int) -> None:
    prices = [price(amount=Decimal("1000.00"), price_scope="stay_total") for _ in range(count)]
    totals = sum_prices(prices)
    if count == 0:
        assert totals.completeness == "unknown" and totals.amount is None
    else:
        assert totals.amount == Decimal("1000.00") * count
