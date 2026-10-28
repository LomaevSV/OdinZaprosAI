"""Сценарии корпуса без отдельного теста в срезах: K-05 (сквозной), K-12, K-29 (TEST-01, Mock)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import trip_form

pytestmark = [pytest.mark.e2e_mock]

API = "/api/v1"


def _send(client: Any, form: dict[str, Any]) -> dict[str, Any]:
    session = client.post(f"{API}/sessions").json()
    response = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-corpus-0001", "trip_revision": session["trip_revision"], "form": form},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.req("K-05", "UC-02")
def test_missing_origin_gives_one_blocking_question(client: Any) -> None:
    turn = _send(client, trip_form(origin=None))
    assert turn["status"] == "awaiting_clarification"
    assert [(q["field"], q["blocking_class"]) for q in turn["questions"]] == [("origin", "blocking_now")]
    assert turn["state"]["plan"] is None, (
        "без города отправления поиск не выполняется и город не предполагается"
    )


@pytest.mark.req("K-12", "HOTEL-01", "PRICE-01")
def test_child_six_years_is_carried_to_every_search(client: Any) -> None:
    turn = _send(client, trip_form(children_ages=[6]))
    state = turn["state"]
    assert state["request"]["party"]["children_ages"] == [6]
    assert state["hotel_offers"] and all(
        offer["occupancy"]["children_ages"] == [6] and offer["occupancy"]["adults"] == 2
        for offer in state["hotel_offers"]
    )
    assert all(offer["parameters"].get("children_ages") == [6] for offer in state["transport_offers"])
    assert "дети: 6" in turn["assistant_text"]


@pytest.mark.req("K-29", "DATA-MODE-01", "UI-01")
def test_mock_is_visible_in_every_card(client: Any) -> None:
    turn = _send(client, trip_form())
    state = turn["state"]
    offers = [*state["transport_offers"], *state["hotel_offers"], *state["transfer_options"]]
    assert offers and all(offer["source"]["data_mode"] == "mock" for offer in offers)
    assert all(hotel["source"]["data_mode"] == "mock" for hotel in state["hotels"])
    plan = state["plan"]
    assert plan["trip_data_mode"] == "mock" and set(plan["data_mode_breakdown"].values()) == {"mock"}
    assert all(component["data_mode"] == "mock" for component in plan["components"])
    assert "ДЕМО" in turn["assistant_text"]
    cart = client.post(
        f"{API}/trips/{turn['trip_id']}/cart",
        json={"request_id": "req-corpus-0002", "trip_revision": turn["trip_revision"]},
    ).json()["cart"]
    assert all(
        item["data_mode"] == "mock" and item["source"]["data_mode"] == "mock" for item in cart["items"]
    )
    assert any("ДЕМО" in risk for risk in cart["significant_unknowns"])
