"""Live-адаптеры на синтетических фикстурах (T-ADP-TUTU-01..04, DEC-04/09/11/23): сеть не используется."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from odin.app import build_application
from odin.config import Settings
from odin.tools.mcp_client import McpClient, McpError
from odin.tools.places import ResolvedPlace, match_transit_point
from odin.tools.policy import ToolDeniedError, ToolPolicy, load_provider_policies
from odin.tools.rest_adapters import normalize_geocoding, normalize_transfers
from odin.tools.tutu import (
    ALLOWED_HOSTS,
    ENDPOINT,
    TutuGateway,
    avia_party,
    normalize_hotels,
    normalize_transport,
)
from tests.conftest import TRIP_END, TRIP_START, trip_form
from tests.live_fixtures import GEOCODING, FakeRest, FakeTutu, avia_payload, hotels_payload, rail_payload

pytestmark = [pytest.mark.adapter, pytest.mark.req("PROVIDER-01", "DATA-MODE-01", "DEC-09")]

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
MOSCOW = ResolvedPlace("Москва", "RU", "Europe/Moscow", 55.75, 37.62)
KAZAN = ResolvedPlace("Казань", "RU", "Europe/Moscow", 55.79, 49.12)


def _client(fake: FakeTutu) -> McpClient:
    return McpClient(endpoint=ENDPOINT, allowed_hosts=ALLOWED_HOSTS, timeout_s=5, transport=fake.transport())


# --------------------------------------------------------------------- MCP-клиент


@pytest.mark.req("DEC-04", "T-ADP-TUTU-01")
def test_mcp_client_json_and_sse_without_session() -> None:
    fake = FakeTutu(sse_tools=frozenset({"search_avia"}))
    client = _client(fake)
    info = client.initialize()
    assert info["name"] == "tutu-mcp-server" and client.negotiated_version == "2025-06-18"
    assert client.session_id is None and fake.methods[:2] == ["initialize", "notifications/initialized"]
    assert {tool["name"] for tool in client.list_tools()} >= {"search_rail", "search_hotels"}
    rail = client.call_tool(
        "search_rail",
        {"origin": "Москва", "destination": "Казань", "departure_date": "2026-11-24", "passengers": 2},
    )
    assert rail.payload()["meta"]["pricing"]["basis"] == "per_seat"
    avia = client.call_tool("search_avia", {"origin": "Москва", "departure_date": "2026-11-24"})
    assert avia.payload()["meta"]["pricing"]["basis"] == "party_total"  # ответ пришёл по SSE


@pytest.mark.negative
@pytest.mark.req("T-NEG-TOOL-01", "T-NEG-TOOL-04")
def test_mcp_client_errors_and_allowlist() -> None:
    for status, code in ((401, "auth_401"), (429, "rate_limited"), (503, "server_503")):
        with pytest.raises(McpError) as err:
            _client(FakeTutu(fail_status=status)).initialize()
        assert err.value.code == code
    with pytest.raises(McpError) as err:
        McpClient(endpoint="https://evil.test/mcp", allowed_hosts=ALLOWED_HOSTS, timeout_s=1)
    assert err.value.code == "host_not_allowed"


@pytest.mark.negative
@pytest.mark.req("T-NEG-SEC-01", "T-NEG-SCHEMA-01", "UC-08")
def test_tutu_gateway_denies_before_network_and_closes_changed_schema() -> None:
    fake = FakeTutu()
    gateway = TutuGateway(
        client=_client(fake), policy=ToolPolicy(data_mode="live", policies=load_provider_policies())
    )
    for tool in ("register_checkout_passengers", "create_checkout_link", "get_rail_seatmap"):
        with pytest.raises(ToolDeniedError):
            gateway.call(tool, {})
    assert fake.methods == []  # отказ до сети
    changed = [tool for tool in fake.tools if tool["name"] != "search_rail"]
    changed.append({"name": "search_rail", "inputSchema": {"type": "object", "properties": {"from": {}}}})
    gateway = TutuGateway(
        client=_client(FakeTutu(tools=changed)),
        policy=ToolPolicy(data_mode="live", policies=load_provider_policies()),
    )
    with pytest.raises(McpError) as err:
        gateway.call("search_rail", {})
    assert err.value.code == "schema_changed"


# --------------------------------------------------------------------- нормализация


@pytest.mark.req("T-ADP-TUTU-02", "PRICE-01", "AVAIL-01")
def test_rail_price_is_party_from_per_seat_and_availability_unknown() -> None:
    day = date(2026, 11, 24)
    offers, warnings = normalize_transport(
        rail_payload(day),
        kind="rail",
        direction="outbound",
        origin=MOSCOW,
        destination=KAZAN,
        adults=2,
        children_ages=[],
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    assert warnings == [] and len(offers) == 2
    first = offers[0]
    assert (
        first.price.amount == first.price.amount.quantize(first.price.amount)
        and str(first.price.amount) == "3001.00"
    )
    assert first.price.price_scope == "party_total" and first.price.total_completeness == "partial"
    assert first.price.mandatory_fees_status == "unknown"
    assert (
        first.availability.availability_status == "unknown"
        and first.availability.evidence_level == "search_listing"
    )
    assert (
        first.source.data_mode == "live"
        and first.source.provider == "tutu_mcp"
        and first.source.retrieved_at == NOW
    )
    assert first.terms["arrival_point_id"] == "kzn-rail" and first.arrival_place.lat is not None
    assert first.departure_at.utcoffset() == timedelta(hours=3)
    assert "checkout_hint" not in first.terms  # тексты провайдера — данные, а не команды
    with_child, _ = normalize_transport(
        rail_payload(day),
        kind="rail",
        direction="outbound",
        origin=MOSCOW,
        destination=KAZAN,
        adults=2,
        children_ages=[6],
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    assert any("детские тарифы" in item for item in with_child[0].limitations)


def test_avia_party_total_and_basis_mismatch_gives_unknown_amount() -> None:
    day = date(2026, 11, 24)
    offers, _ = normalize_transport(
        avia_payload(day),
        kind="flight",
        direction="outbound",
        origin=MOSCOW,
        destination=KAZAN,
        adults=2,
        children_ages=[],
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    assert str(offers[0].price.amount) == "9000.00" and offers[0].price.price_scope == "party_total"
    assert offers[0].terms["arrival_point_id"] == "KZN" and offers[0].terms["baggage_pieces"] == 0
    broken = avia_payload(day)
    broken["meta"]["pricing"]["basis"] = "per_person"
    offers, warnings = normalize_transport(
        broken,
        kind="flight",
        direction="outbound",
        origin=MOSCOW,
        destination=KAZAN,
        adults=2,
        children_ages=[],
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    assert offers[0].price.amount is None and offers[0].price.total_completeness == "unknown" and warnings
    assert avia_party(2, [14, 6, 1]) == (3, 1, 1)


@pytest.mark.req("T-ADP-TUTU-03", "DEC-16", "T-NEG-INJ-01")
def test_hotels_stay_total_partial_and_unsafe_urls_dropped() -> None:
    check_in, check_out = date(2026, 11, 24), date(2026, 11, 26)
    hotels, offers, _ = normalize_hotels(
        hotels_payload(check_in, check_out),
        city=KAZAN,
        check_in=check_in,
        check_out=check_out,
        adults=2,
        children_ages=[],
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    assert len(hotels) == 3 and len(offers) == 3
    priced = offers[0]
    assert priced.price_scope == "stay_total" and str(priced.price_total.amount) == "8799.00"
    assert priced.price_total.total_completeness == "partial" and priced.terms.breakfast_included is True
    assert priced.availability.availability_status == "unknown"
    assert hotels[0].geo == {"lat": 55.7892, "lon": 49.1527} and hotels[0].star_rating == 3
    assert hotels[2].source.url is None  # ссылка вне tutu.ru отброшена
    assert offers[2].price_total.amount is None
    other_dates, offers_other, warnings = normalize_hotels(
        hotels_payload(check_in, check_out),
        city=KAZAN,
        check_in=check_in,
        check_out=check_out + timedelta(days=1),
        adults=2,
        children_ages=[],
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    assert other_dates == [] and offers_other == [] and "не совпадают" in warnings[0]


def test_geocoding_validates_timezone_and_transfer_matrix() -> None:
    places = normalize_geocoding(GEOCODING["казань"], data_mode="live", retrieved_at=NOW, query="Казань")
    assert places[0].timezone == "Europe/Moscow" and places[0].source.provider == "open_meteo"
    assert normalize_geocoding(GEOCODING["плохой город"], data_mode="live", retrieved_at=NOW, query="x") == []
    from tests.live_fixtures import osrm_table

    points = [
        {
            "point_id": "kzn-rail",
            "label": "вокзал",
            "lat": 55.78,
            "lon": 49.09,
            "directions": ["transfer_in", "transfer_out"],
        }
    ]
    hotels = [{"hotel_id": "TUTU-H-1", "name": "Отель", "lat": 55.79, "lon": 49.15}]
    offers = normalize_transfers(
        osrm_table(2),
        points=points,
        hotels=hotels,
        city="Казань",
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    assert {offer.direction for offer in offers} == {"transfer_in", "transfer_out"}
    assert all(offer.duration_basis == "estimate_osrm" and offer.price.amount is None for offer in offers)
    assert offers[0].duration_minutes == 11 and offers[0].terms["distance_km"] == 5.0
    assert match_transit_point("Москва — Казанский вокзал (2000003)", "rail").key == "mow-kazansky"
    assert match_transit_point("Казань, KZN, терм. 1", "airport").key == "KZN"
    assert match_transit_point("Неизвестно — Станция (1)", "rail") is None


# --------------------------------------------------------------------- сквозной путь по режимам


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from odin.api import create_app

    clients: list[Any] = []

    def make(mode: str, tutu: FakeTutu, rest: FakeRest, name: str = "odin") -> Any:
        settings = Settings.from_env(
            {
                "ODIN_DATA_MODE": mode,
                "ODIN_DB_PATH": str(tmp_path / f"{name}.sqlite3"),
                "ODIN_REPLAY_DIR": str(tmp_path / "replay"),
                "ODIN_TOOL_RETRIES": "0",
            }
        )
        application = build_application(
            settings, mcp_transport=tutu.transport(), http_transport=rest.transport()
        )
        client = TestClient(create_app(settings, application=application))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def _plan(client: Any, **overrides: Any) -> dict[str, Any]:
    session = client.post("/api/v1/sessions").json()
    turn = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={
            "request_id": "req-live-0001",
            "trip_revision": session["trip_revision"],
            "form": trip_form(**overrides),
        },
    )
    assert turn.status_code == 200, turn.text
    return turn.json()


@pytest.mark.e2e_mock
@pytest.mark.req("T-E2E-LIVE-01", "DEC-24", "T-NEG-REPLAY-02")
def test_live_plan_then_offline_replay_reproduces_it(make_client: Any) -> None:
    tutu, rest = FakeTutu(), FakeRest()
    live = make_client("live", tutu, rest)
    turn = _plan(live, transport_modes=["rail"])
    plan = turn["state"]["plan"]
    assert turn["status"] == "planned", turn["assistant_text"]
    assert plan["trip_data_mode"] == "live" and set(plan["data_mode_breakdown"].values()) == {"live"}
    roles = {component["role"]: component for component in plan["components"]}
    assert {"outbound", "return", "hotel", "transfer_in", "transfer_out"} <= set(roles)
    assert roles["transfer_in"]["offer_id"].startswith("OSRM-transfer_in-kzn-rail-")
    assert not plan["plan_id"].startswith("DEMO") and "ДЕМО" not in turn["assistant_text"]
    assert turn["state"]["request"]["destination"]["timezone"] == "Europe/Moscow"
    assert turn["state"]["request"]["destination"]["source"]["provider"] == "open_meteo"
    called = [name for name, _ in tutu.calls]
    assert sorted(called) == ["get_offer_details"] * 3 + ["search_hotels", "search_rail", "search_rail"]
    rail_args = next(args for name, args in tutu.calls if name == "search_rail")
    assert rail_args["passengers"] == 2 and rail_args["departure_date"] in (
        TRIP_START.isoformat(),
        TRIP_END.isoformat(),
    )
    # Волна 2: детали лучших K — наличие по данным поставщика, тариф с завтраком отдельно, время заезда.
    state = turn["state"]
    detailed = [offer for offer in state["hotel_offers"] if offer["offer_id"].startswith("TUTU-HD-900000-")]
    assert len(detailed) == 2 and {offer["terms"]["breakfast_included"] for offer in detailed} == {
        True,
        False,
    }
    assert all(offer["availability"]["availability_status"] == "available" for offer in detailed)
    assert all(offer["availability"]["evidence_level"] == "offer_details" for offer in detailed)
    assert not any(offer["hotel_id"] == "TUTU-H-900001" for offer in state["hotel_offers"])  # нет номеров
    assert "нет номеров" in turn["assistant_text"]
    hotel = next(item for item in state["hotels"] if item["hotel_id"] == "TUTU-H-900000")
    assert hotel["check_in_from"] == "14:00:00"
    if roles["hotel"]["offer_id"].startswith("TUTU-HD-"):
        assert "наличие (hotel) не подтверждено источником" not in plan["unknowns"]
    health = live.get("/health").json()
    enabled = {item["provider_id"] for item in health["providers"] if item["live_enabled"]}
    assert enabled == {"tutu_mcp", "open_meteo", "osrm_demo"}

    offline = make_client(
        "replay", FakeTutu(fail_status=500), FakeRest(fail_geo=True, fail_osrm=True), name="replay"
    )
    replayed = _plan(offline, transport_modes=["rail"])
    replay_plan = replayed["state"]["plan"]
    assert replayed["status"] == "planned" and replay_plan["trip_data_mode"] == "replay"
    assert "Replay" in replayed["assistant_text"]
    other = _plan(offline, transport_modes=["rail"], adults=3)  # другой состав группы Replay не подставляет
    assert other["state"]["plan"] is None or not other["state"]["plan"]["components"]


@pytest.mark.e2e_mock
@pytest.mark.req("DEC-23", "T-NEG-LIVEFAIL-01")
def test_mixed_falls_back_to_marked_mock_and_live_stays_honest(make_client: Any) -> None:
    mixed = make_client("mixed", FakeTutu(fail_status=503), FakeRest(), name="mixed")
    turn = _plan(mixed, transport_modes=["rail"])
    plan = turn["state"]["plan"]
    assert turn["status"] == "planned" and plan["components"]
    assert {component["data_mode"] for component in plan["components"]} == {"mock"}
    assert "ДЕМО" in turn["assistant_text"]
    # Нет незаметной подмены: в ответе прямо сказано, что Live недоступен и показан ДЕМО-вариант.
    assert "Live-источник недоступен" in turn["assistant_text"] and "ДЕМО-вариант" in turn["assistant_text"]

    strict = make_client("live", FakeTutu(fail_status=503), FakeRest(), name="strict")
    turn = _plan(strict, transport_modes=["rail"])
    assert turn["state"]["plan"] is None or not turn["state"]["plan"]["components"]
    termination = turn["state"]["search_termination"]
    assert termination["reason"] == "provider_unavailable"
    assert any(item["source"].startswith("tutu_mcp") for item in termination["unchecked_sources"])
    assert "ДЕМО-" not in str(turn["state"]["transport_offers"])
    assert "недоступен" in turn["assistant_text"] and "ДЕМО-вариант" not in turn["assistant_text"]


@pytest.mark.req("UC-03")
def test_unknown_city_in_live_is_reported_not_invented(make_client: Any) -> None:
    client = make_client("live", FakeTutu(), FakeRest(), name="unknown")
    turn = _plan(client, destination="Нигдеград")
    termination = turn["state"]["search_termination"]
    assert termination["reason"] == "provider_unavailable"
    assert "геокодер" in termination["unchecked_sources"][0]["reason"]


@pytest.mark.req("AVAIL-01", "DEC-15", "T-ADP-TUTU-04")
def test_hotel_details_confirm_availability_only_live_and_for_requested_stay() -> None:
    from odin.tools.tutu import normalize_hotel_details
    from tests.live_fixtures import details_payload

    check_in, check_out = date(2026, 11, 24), date(2026, 11, 26)
    hotels, _, _ = normalize_hotels(
        hotels_payload(check_in, check_out),
        city=KAZAN,
        check_in=check_in,
        check_out=check_out,
        adults=2,
        children_ages=[],
        request_parameters={},
        data_mode="live",
        retrieved_at=NOW,
        ttl_minutes=30,
    )
    common = {
        "check_in": check_in,
        "check_out": check_out,
        "adults": 2,
        "children_ages": [],
        "request_parameters": {},
        "retrieved_at": NOW,
        "ttl_minutes": 30,
    }
    hotel, offers, notes, status = normalize_hotel_details(
        details_payload("900000", check_in, check_out, 2), hotel=hotels[0], data_mode="live", **common
    )
    assert status == "available" and notes == [] and len(offers) == 2  # тариф «за ночь» отброшен
    cheapest, breakfast = offers
    assert str(cheapest.price_total.amount) == "8400.00" and cheapest.terms.breakfast_included is False
    assert breakfast.terms.breakfast_included is True and breakfast.terms.free_cancellation_until is not None
    assert cheapest.availability.availability_status == "available"
    assert (
        cheapest.availability.evidence_level == "offer_details"
        and cheapest.price_total.total_completeness == "partial"
    )
    assert str(hotel.check_in_from) == "14:00:00" and hotel.location.address.startswith("Россия")
    _, none, notes, status = normalize_hotel_details(
        details_payload("900001", check_in, check_out, 2), hotel=hotels[1], data_mode="live", **common
    )
    assert none == [] and status == "unavailable" and "нет номеров" in notes[0]
    assert "забронируй" not in " ".join(notes)  # подсказка сервера — данные, а не команда
    _, replayed, _, _ = normalize_hotel_details(
        details_payload("900000", check_in, check_out, 2), hotel=hotels[0], data_mode="replay", **common
    )
    assert all(offer.availability.availability_status == "unknown" for offer in replayed)
    _, other, notes, status = normalize_hotel_details(
        details_payload("900000", check_in, check_out, 3), hotel=hotels[0], data_mode="live", **common
    )
    assert other == [] and status == "unknown" and "состав" in notes[0]
