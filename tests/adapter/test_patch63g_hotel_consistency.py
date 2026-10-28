"""PATCH-63G: согласованность гостиничных предложений Tutu (без сети, без LLM, на фикстурах tests/live_fixtures).

- 63G-01: неподдерживаемый состав гостей — явная ошибка до обращения к поставщику (без «обрезания» до 6);
- 63G-02: offer_id различает гостиницу, даты, состав, номер и тариф; не зависит от позиции тарифа в ответе;
- 63G-03: явное расхождение состава в ответе get_offer_details — отказ; неуказанный состав не подтверждён.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from odin.app import build_application
from odin.config import Settings
from odin.tools.live_handlers import build_live_toolset
from odin.tools.places import ResolvedPlace
from odin.tools.policy import ToolPolicy, load_provider_policies
from odin.tools.tutu import (
    MAX_HOTEL_ADULTS,
    UnsupportedGuests,
    details_arguments,
    hotel_arguments,
    normalize_hotel_details,
    normalize_hotels,
)
from tests.conftest import TRIP_END, TRIP_START, trip_form
from tests.live_fixtures import FakeRest, FakeTutu, details_payload, hotels_payload

pytestmark = [pytest.mark.adapter, pytest.mark.req("PATCH-63G", "T-ADP-TUTU-03", "T-ADP-TUTU-04")]

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
KAZAN = ResolvedPlace("Казань", "RU", "Europe/Moscow", 55.79, 49.12)
CHECK_IN, CHECK_OUT = date(2026, 11, 24), date(2026, 11, 26)


def _search(
    check_out: date = CHECK_OUT, adults: int = 2, children: list[int] | None = None, payload: Any = None
) -> tuple[list[Any], list[Any], list[str]]:
    return normalize_hotels(
        payload if payload is not None else hotels_payload(CHECK_IN, check_out),
        city=KAZAN,
        check_in=CHECK_IN,
        check_out=check_out,
        adults=adults,
        children_ages=list(children or []),
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )


def _details(
    payload: dict[str, Any], check_out: date = CHECK_OUT, adults: int = 2, children: list[int] | None = None
) -> tuple[Any, list[Any], list[str], str]:
    hotels, _, _ = _search()
    return normalize_hotel_details(
        payload,
        hotel=hotels[0],
        check_in=CHECK_IN,
        check_out=check_out,
        adults=adults,
        children_ages=list(children or []),
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )


def _detail_ids(check_out: date = CHECK_OUT, adults: int = 2, children: list[int] | None = None) -> set[str]:
    _, offers, _, _ = _details(
        details_payload("900000", CHECK_IN, check_out, adults, children), check_out, adults, children
    )
    return {offer.offer_id for offer in offers}


def _search_ids(check_out: date = CHECK_OUT, adults: int = 2, children: list[int] | None = None) -> set[str]:
    return {offer.offer_id for offer in _search(check_out, adults, children)[1]}


# ---------------------------------------------------------------- 63G-01: семь взрослых


def test_seven_adults_is_an_explicit_error_not_six() -> None:
    assert MAX_HOTEL_ADULTS == 6
    assert hotel_arguments("Казань", "2026-11-24", "2026-11-26", 6, [])["adults"] == 6, (
        "6 передаётся как есть"
    )
    for build in (
        lambda: hotel_arguments("Казань", "2026-11-24", "2026-11-26", 7, []),
        lambda: details_arguments("900000", "2026-11-24", "2026-11-26", 7, []),
    ):
        with pytest.raises(UnsupportedGuests, match="7 взрослых"):
            build()
    with pytest.raises(UnsupportedGuests):
        hotel_arguments("Казань", "2026-11-24", "2026-11-26", 2, [18])


def _toolset(fake: FakeTutu) -> Any:
    return build_live_toolset(
        policy=ToolPolicy(data_mode="live", policies=load_provider_policies(None)),
        data_mode="live",
        replay_dir=None,
        timeout_s=5,
        retries=0,
        mcp_transport=fake.transport(),
        http_transport=FakeRest().transport(),
    )


def test_seven_adults_never_reach_the_provider() -> None:
    fake = FakeTutu()
    toolset = _toolset(fake)
    stay = {"check_in": CHECK_IN.isoformat(), "check_out": CHECK_OUT.isoformat(), "adults": 7}
    search = toolset.search_hotels({"city": "Казань", **stay, "children_ages": []})
    assert search["status"] == "error" and search["error_code"] == "guests_not_supported_by_source"
    assert "7 взрослых" in search["warnings"][0] and "недоступен" in search["warnings"][0]
    hotels, _, _ = _search()
    details = toolset.get_offer_details({"hotels": [hotels[0].model_dump(mode="json")], **stay})
    assert details["status"] == "error" and details["error_code"] == "guests_not_supported_by_source"
    assert fake.methods == [] and fake.calls == [], "до поставщика запрос не доходит (даже initialize)"


# ---------------------------------------------------------------- 63G-02: идентичность предложения


def test_offer_id_changes_with_check_out_adults_and_children_ages() -> None:
    base_search, base_details = _search_ids(), _detail_ids()
    assert len(base_search) == 3 and len(base_details) == 2
    for changed in (
        {"check_out": CHECK_OUT + timedelta(days=1)},  # тот же заезд, другой выезд
        {"adults": 3},
        {"children": [5]},
    ):
        assert not _search_ids(**changed) & base_search, changed
        assert not _detail_ids(**changed) & base_details, changed
    assert not _detail_ids(children=[5]) & _detail_ids(children=[6]), "возраст ребёнка — часть идентичности"
    assert _detail_ids(children=[5, 8]) == _detail_ids(children=[8, 5]), "порядок возрастов не важен"
    assert _search_ids() == base_search and _detail_ids() == base_details, "детерминированно"
    assert all(offer_id.startswith("TUTU-HD-900000-2026-11-24-") for offer_id in base_details)


def _tariffs(offers: list[Any]) -> dict[str, tuple[str, bool | None, str | None]]:
    return {o.offer_id: (str(o.price_total.amount), o.terms.breakfast_included, o.room_type) for o in offers}


def test_rate_permutation_keeps_identity_and_different_rates_differ() -> None:
    payload = details_payload("900000", CHECK_IN, CHECK_OUT, 2)
    _, offers, _, _ = _details(payload)
    shuffled = copy.deepcopy(payload)
    shuffled["rooms"].reverse()
    for room in shuffled["rooms"]:
        room["rates"].reverse()
    _, permuted, _, _ = _details(shuffled)
    assert _tariffs(permuted) == _tariffs(offers), "перестановка тарифов не меняет соответствие id → тариф"
    assert len({o.offer_id for o in offers}) == len(offers) == 2, "разные тарифы одной гостиницы различаются"


def test_supplier_rate_id_is_used_and_duplicates_collapse() -> None:
    payload = details_payload("900000", CHECK_IN, CHECK_OUT, 2)
    room = payload["rooms"][0]
    cheap = room["rates"][0]
    room["rates"] = [
        {**cheap, "rate_id": "R-1"},
        {**cheap, "rate_id": "R-2", "price": {"amount": 8500.0, "currency": "RUB"}},
    ]
    _, offers, _, _ = _details(payload)
    first = next(o for o in offers if str(o.price_total.amount) == "8400.00")
    repriced = copy.deepcopy(payload)
    repriced["rooms"][0]["rates"][0]["price"]["amount"] = 8300.0
    _, again, _, _ = _details(repriced)
    assert first.offer_id in {o.offer_id for o in again}, (
        "устойчивый id тарифа поставщика сохраняет идентичность"
    )
    without_ids = details_payload("900000", CHECK_IN, CHECK_OUT, 2)
    without_ids["rooms"][0]["rates"] = [copy.deepcopy(cheap), copy.deepcopy(cheap)]
    _, single, _, _ = _details(without_ids)
    assert len(single) == 1, "одинаковые тарифы без id поставщика — одно предложение, а не два с разными id"


# ---------------------------------------------------------------- 63G-03: состав гостей в ответе


def test_guest_mismatch_in_details_rejects_and_missing_is_not_confirmed() -> None:
    _, offers, notes, status = _details(details_payload("900000", CHECK_IN, CHECK_OUT, 2, [5]), children=[5])
    assert status == "available" and offers and notes == []
    assert all(o.availability.availability_status == "available" for o in offers)
    assert all(o.occupancy.children_ages == [5] and o.price_total.guests == 3 for o in offers)

    for payload, children in (
        (details_payload("900000", CHECK_IN, CHECK_OUT, 3), []),  # другое число взрослых
        (details_payload("900000", CHECK_IN, CHECK_OUT, 2, [7]), [5]),  # другой возраст ребёнка
        (details_payload("900000", CHECK_IN, CHECK_OUT, 2), [5]),  # ребёнка в ответе нет
        (details_payload("900000", CHECK_IN, CHECK_OUT, 2, [5]), []),  # лишний ребёнок
    ):
        _, rejected, notes, status = _details(payload, children=children)
        assert rejected == [] and status == "unknown" and "состав" in notes[0]

    absent = details_payload("900000", CHECK_IN, CHECK_OUT, 2)
    del absent["stay"]["guests"]["children_ages"]
    _, kept, notes, status = _details(absent, children=[5])
    assert kept and status == "unknown", "неуказанный возраст детей не подтверждён, но и не расхождение"
    assert all(o.availability.availability_status == "unknown" for o in kept)
    assert any("не указан" in note for note in notes)
    assert all(any("не подтверждено" in item for item in o.limitations) for o in kept)
    no_guests = details_payload("900000", CHECK_IN, CHECK_OUT, 2)
    del no_guests["stay"]["guests"]
    _, kept, _, status = _details(no_guests)
    assert kept and status == "unknown" and all(o.availability.availability_status == "unknown" for o in kept)


def test_rate_level_guest_conflict_rejects_only_that_rate() -> None:
    payload = details_payload("900000", CHECK_IN, CHECK_OUT, 2)
    payload["rooms"][0]["rates"][0]["occupancy"] = {"adults": 1}
    payload["rooms"][0]["rates"][1]["max_adults"] = 2
    _, offers, notes, _ = _details(payload)
    assert [str(o.price_total.amount) for o in offers] == ["10400.00"], "тариф на 1 взрослого не принят"
    assert any("другого состава" in note for note in notes)
    small = details_payload("900000", CHECK_IN, CHECK_OUT, 3)
    small["rooms"][0]["max_guests"] = 2
    _, none, notes, status = _details(small, adults=3)
    assert none == [] and status == "unknown"


def test_search_guest_mismatch_is_rejected() -> None:
    payload = hotels_payload(CHECK_IN, CHECK_OUT)
    payload["stay"]["guests"] = {"adults": 3}
    hotels, offers, warnings = _search(payload=payload)
    assert hotels == [] and offers == [] and "состав" in warnings[0]
    payload = hotels_payload(CHECK_IN, CHECK_OUT)
    payload["hotels"][0]["best_offer"]["guests"] = {"adults": 1}
    _, offers, warnings = _search(payload=payload)
    assert {o.hotel_id for o in offers} == {"TUTU-H-900001", "TUTU-H-900009"} and warnings


# ---------------------------------------------------------------- сквозной путь: пересборка и старая цена


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[Any, FakeTutu]]:
    from fastapi.testclient import TestClient

    from odin.api import create_app

    tutu = FakeTutu()
    settings = Settings.from_env(
        {
            "ODIN_DATA_MODE": "live",
            "ODIN_DB_PATH": str(tmp_path / "odin.sqlite3"),
            "ODIN_REPLAY_DIR": str(tmp_path / "replay"),
            "ODIN_TOOL_RETRIES": "0",
        }
    )
    application = build_application(
        settings, mcp_transport=tutu.transport(), http_transport=FakeRest().transport()
    )
    with TestClient(create_app(settings, application=application)) as client:
        yield client, tutu


def _plan(client: Any, **overrides: Any) -> dict[str, Any]:
    session = client.post("/api/v1/sessions").json()
    turn = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={
            "request_id": "req-63g-0001",
            "trip_revision": session["trip_revision"],
            "form": trip_form(**overrides),
        },
    )
    assert turn.status_code == 200, turn.text
    return turn.json()


def _hotel(state: dict[str, Any]) -> dict[str, Any]:
    component = next(c for c in state["plan"]["components"] if c["role"] == "hotel")
    return next(o for o in state["hotel_offers"] if o["offer_id"] == component["offer_id"])


def test_seven_adults_through_api_gives_no_hotel_priced_for_six(live: tuple[Any, FakeTutu]) -> None:
    client, tutu = live
    turn = _plan(client, transport_modes=["rail"], adults=7)
    state = turn["state"]
    assert state["hotel_offers"] == [] and not any(
        c["role"] == "hotel" for c in (state["plan"] or {}).get("components", [])
    )
    assert not [name for name, _ in tutu.calls if name in ("search_hotels", "get_offer_details")]
    assert "7 взрослых" in turn["assistant_text"]
    assert any(
        "guests_not_supported_by_source" in item["reason"]
        for item in state["search_termination"]["unchecked_sources"]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"date_to": (TRIP_END + timedelta(days=1)).isoformat()},
        {"adults": 3},
        {"children_ages": [6]},
    ],
    ids=["check_out", "adults", "child_age"],
)
def test_replan_after_dates_or_guests_never_reuses_old_quote(
    live: tuple[Any, FakeTutu], change: dict[str, Any]
) -> None:
    client, _tutu = live
    first = _plan(client, transport_modes=["rail"], children_ages=[5])
    old_state = first["state"]
    old_hotel = _hotel(old_state)
    old_ids = {o["offer_id"] for o in old_state["hotel_offers"]}
    cart = client.post(
        f"/api/v1/trips/{first['trip_id']}/cart",
        json={"request_id": "req-63g-cart", "trip_revision": first["trip_revision"]},
    )
    assert cart.status_code == 201, cart.text
    revision = cart.json()["trip_revision"]

    replanned = client.post(
        f"/api/v1/trips/{first['trip_id']}/replan",
        json={"request_id": "req-63g-replan", "trip_revision": revision, "change_set": change},
    )
    assert replanned.status_code == 200, replanned.text
    body = replanned.json()
    state = body["state"]
    request = state["request"]
    new_hotel = _hotel(state)
    assert not {o["offer_id"] for o in state["hotel_offers"]} & old_ids, "ни одной котировки прежних условий"
    assert new_hotel["offer_id"] != old_hotel["offer_id"]
    assert (new_hotel["check_in"], new_hotel["check_out"]) == (
        request["date_range"]["start"],
        request["date_range"]["end"],
    )
    assert new_hotel["occupancy"]["adults"] == request["party"]["adults"]
    assert new_hotel["occupancy"]["children_ages"] == request["party"]["children_ages"]
    assert new_hotel["price_total"]["guests"] == request["party"]["adults"] + len(
        request["party"]["children_ages"]
    )
    assert state["cart"] is None

    stale = client.post(
        f"/api/v1/trips/{first['trip_id']}/selections",
        json={
            "request_id": "req-63g-select",
            "trip_revision": body["trip_revision"],
            "selections": {"hotel": old_hotel["offer_id"]},
        },
    )
    assert stale.status_code >= 400 and "неизвестные варианты" in stale.text, (
        "старая котировка в новой ревизии не принимается"
    )
    confirm = client.post(
        f"/api/v1/carts/{cart.json()['cart']['cart_id']}/confirm",
        json={
            "request_id": "req-63g-confirm",
            "trip_revision": revision,
            "confirmation_id": "conf-63g-0001",
            "purchase_consent": True,
            "risk_acknowledgement": True,
        },
    )
    assert confirm.status_code == 409, "корзина со старой ценой не подтверждается"


def test_same_conditions_give_same_hotel_identity(live: tuple[Any, FakeTutu]) -> None:
    client, _tutu = live
    first = _plan(client, transport_modes=["rail"])
    second = _plan(client, transport_modes=["rail"])
    assert {o["offer_id"] for o in first["state"]["hotel_offers"]} == {
        o["offer_id"] for o in second["state"]["hotel_offers"]
    }
    assert TRIP_START.isoformat() in _hotel(first["state"])["offer_id"]
