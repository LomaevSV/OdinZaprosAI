"""T-E2E-MOCK-01..03, T-E2E-API-*: сквозной путь по JSON API на Mock (ACCEPT-01 п. 2, UC-07, UC-08).

Сообщение → план → корзина → подтверждение → демо-чек; повтор подтверждения; отказ.
Внешних вызовов нет (сеть заблокирована фикстурой conftest).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import trip_form

pytestmark = [pytest.mark.e2e_mock, pytest.mark.req("ACCEPT-01", "UC-07", "UC-08", "API-01")]

DEMO_NOTICE = "Демонстрационная покупка. Реального бронирования и списания средств нет"


def _start(client: Any, **form_overrides: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    session = client.post("/api/v1/sessions").json()
    turn = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={
            "request_id": "req-e2e-0001",
            "trip_revision": session["trip_revision"],
            "form": trip_form(**form_overrides),
        },
    )
    assert turn.status_code == 200, turn.text
    return session, turn.json()


def _cart(client: Any, turn: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/trips/{turn['trip_id']}/cart",
        json={"request_id": "req-e2e-0002", "trip_revision": turn["trip_revision"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _confirm(client: Any, cart: dict[str, Any], *, request_id: str = "req-e2e-0003", **overrides: Any):
    body = {
        "request_id": request_id,
        "trip_revision": cart["trip_revision"],
        "confirmation_id": "conf-e2e-00000001",
        "purchase_consent": True,
        "risk_acknowledgement": True,
    }
    body.update(overrides)
    return client.post(f"/api/v1/carts/{cart['cart']['cart_id']}/confirm", json=body)


@pytest.mark.req("T-E2E-MOCK-01")
def test_message_plan_cart_confirm_receipt(client: Any) -> None:
    session, turn = _start(client)
    state = turn["state"]
    assert turn["status"] == "planned"
    assert turn["input_mode"] == "structured_form"
    plan = state["plan"]
    roles = {component["role"] for component in plan["components"]}
    assert {"hotel", "outbound", "transfer_in"} <= roles
    assert plan["trip_data_mode"] == "mock"
    assert set(plan["data_mode_breakdown"].values()) == {"mock"}
    assert plan["completeness"] == "partial"  # трансфер без подтверждённой стоимости
    assert "ДЕМО" in turn["assistant_text"]
    assert state["search_termination"]["reason"] == "sources_exhausted"

    cart = _cart(client, turn)["cart"]
    assert cart["status"] == "awaiting_confirmation"
    assert cart["demo_notice"] == DEMO_NOTICE
    assert all(item["data_mode"] == "mock" for item in cart["items"])
    transfer_items = [item for item in cart["items"] if item["kind"] == "transfer"]
    assert transfer_items and all(item["informational"] for item in transfer_items)
    assert cart["significant_unknowns"], "UC-08: риски перечислены для отдельного согласия"
    for item in cart["items"]:
        assert item["source"]["provider"] and item["source"]["data_mode"] == "mock"

    response = _confirm(client, {"trip_revision": cart["trip_revision"], "cart": cart})
    assert response.status_code == 200, response.text
    payload = response.json()
    receipt = payload["receipt"]
    assert payload["idempotent_replay"] is False
    assert receipt["notice"] == DEMO_NOTICE
    assert payload["cart"]["status"] == "demo_purchased"
    assert receipt["purchase_consent_at"] is not None and receipt["risk_acknowledged_at"] is not None

    fetched = client.get(f"/api/v1/receipts/{receipt['receipt_id']}")
    assert fetched.status_code == 200 and fetched.json() == receipt
    trip = client.get(f"/api/v1/trips/{turn['trip_id']}").json()
    assert trip["status"] == "purchased_demo"

    session_view = client.get(f"/api/v1/sessions/{session['session_id']}").json()
    assert session_view["trip_revision"] == trip["revision"]  # сессия переживает несколько ходов


@pytest.mark.req("T-E2E-MOCK-02", "ARCH-05")
def test_double_confirmation_returns_same_receipt(client: Any, application: Any) -> None:
    _, turn = _start(client)
    cart_response = _cart(client, turn)
    first = _confirm(client, cart_response)
    second = _confirm(client, cart_response, request_id="req-e2e-0004")
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["receipt"] == first.json()["receipt"]
    assert second.json()["idempotent_replay"] is True
    purchases = [
        entry
        for entry in application.storage.list_audit(turn["trip_id"])
        if entry["event"] == "demo_purchase"
    ]
    assert len(purchases) == 1, "повтор не создаёт дубль покупки"


@pytest.mark.req("T-E2E-MOCK-03")
def test_decline_creates_nothing(client: Any, application: Any) -> None:
    _, turn = _start(client)
    cart_response = _cart(client, turn)
    cart_id = cart_response["cart"]["cart_id"]
    declined = client.post(
        f"/api/v1/carts/{cart_id}/decline",
        json={"request_id": "req-e2e-0005", "trip_revision": cart_response["trip_revision"]},
    )
    assert declined.status_code == 200, declined.text
    cart = declined.json()["cart"]
    assert cart["status"] != "demo_purchased"
    assert cart["demo_receipt_id"] is None and cart["confirmed_at"] is None
    assert cart["declined_at"] is not None
    events = [entry["event"] for entry in application.storage.list_audit(turn["trip_id"])]
    assert "demo_purchase" not in events
    trip = client.get(f"/api/v1/trips/{turn['trip_id']}").json()
    assert trip["status"] != "purchased_demo"
    # После отказа подтвердить ту же корзину по старой ревизии нельзя.
    stale = _confirm(client, cart_response, request_id="req-e2e-0006")
    assert stale.status_code == 409


@pytest.mark.negative
@pytest.mark.req("UC-08")
def test_confirmation_requires_both_consents(client: Any) -> None:
    _, turn = _start(client)
    cart_response = _cart(client, turn)
    no_purchase = _confirm(client, cart_response, purchase_consent=False)
    assert no_purchase.status_code == 400
    no_risk = _confirm(client, cart_response, request_id="req-e2e-0007", risk_acknowledgement=False)
    assert no_risk.status_code == 400
    assert "риск" in no_risk.json()["message"]
    cart = client.get(f"/api/v1/trips/{turn['trip_id']}/cart").json()["cart"]
    assert cart["status"] == "awaiting_confirmation" and cart["demo_receipt_id"] is None


@pytest.mark.negative
@pytest.mark.req("ARCH-05", "REPLAN-02")
def test_stale_revision_is_409_and_old_cart_cannot_be_confirmed(client: Any) -> None:
    session, turn = _start(client)
    stale = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={"request_id": "req-e2e-0010", "trip_revision": 1, "form": trip_form()},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "revision_conflict"

    cart_response = _cart(client, turn)
    replan = client.post(
        f"/api/v1/trips/{turn['trip_id']}/replan",
        json={
            "request_id": "req-e2e-0011",
            "trip_revision": cart_response["trip_revision"],
            "change_set": {"adults": 3},
        },
    )
    assert replan.status_code == 200, replan.text
    assert replan.json()["state"]["request"]["party"]["adults"] == 3
    old = _confirm(client, cart_response, request_id="req-e2e-0012")
    assert old.status_code == 409, "старая корзина после новой ревизии не подтверждается"


@pytest.mark.req("UC-04", "DEC-28")
def test_inspiration_without_destination_returns_three_marked_candidates(client: Any) -> None:
    _, turn = _start(client, destination=None, wishes_tags=["море"], date_from=None, date_to=None)
    candidates = turn["state"]["candidate_destinations"]
    assert len(candidates) == 3
    for candidate in candidates:
        assert candidate["source"]["data_mode"] == "mock"
        assert candidate["visa_status"] == "not_verified"
        assert candidate["estimated_cost"] is None
        assert candidate["cost_note"] == "стоимость требует дат"
    assert candidates[0]["matched_tags"] == ["море"]


@pytest.mark.req("UC-02", "DEC-08")
def test_missing_party_size_asks_blocking_question(client: Any) -> None:
    _, turn = _start(client, adults=None)
    assert turn["status"] == "awaiting_clarification"
    assert [question["field"] for question in turn["questions"]] == ["adults"]
    assert turn["state"]["plan"] is None


@pytest.mark.req("T-LLM-MODE-01", "DEC-08")
def test_free_text_without_llm_is_refused_honestly(client: Any) -> None:
    session = client.post("/api/v1/sessions").json()
    response = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={"request_id": "req-e2e-0020", "trip_revision": 1, "text": "Хочу в Казань на выходные вдвоём"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["input_mode"] == "llm_unavailable"
    assert "Свободный диалог недоступен" in payload["assistant_text"]
    assert payload["state"]["request"] is None
    assert payload["state"]["plan"] is None


@pytest.mark.req("T-E2E-API-01")
def test_health_and_contract_endpoints(client: Any) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok" and body["data_mode"] == "mock" and body["llm_mode"] == "off"
    assert body["providers"] and all(provider["live_enabled"] is False for provider in body["providers"])
    assert "key" not in str(body).lower() or "api_key" not in str(body)
    assert client.get("/api/v1/schemas/Price").json()["title"] == "Price"
    assert client.get("/api/v1/schemas/Unknown").status_code == 404
    assert client.get("/openapi.json").status_code == 200


@pytest.mark.req("T-E2E-API-02", "UI-01")
def test_events_stream_reports_progress(client: Any) -> None:
    session, _ = _start(client)
    response = client.get(f"/api/v1/sessions/{session['session_id']}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: turn_started" in response.text and "event: turn_finished" in response.text


@pytest.mark.req("T-E2E-API-03")
def test_unknown_entities_are_404(client: Any) -> None:
    assert client.get("/api/v1/sessions/s-missing").status_code == 404
    assert client.get("/api/v1/trips/t-missing").status_code == 404
    assert client.get("/api/v1/receipts/DEMO-R-missing").status_code == 404
    response = client.post(
        "/api/v1/carts/DEMO-C-missing/confirm",
        json={
            "request_id": "req-e2e-0030",
            "trip_revision": 1,
            "confirmation_id": "conf-missing-01",
            "purchase_consent": True,
            "risk_acknowledgement": True,
        },
    )
    assert response.status_code == 404


@pytest.mark.negative
@pytest.mark.req("T-NEG-VALID-01", "ARCH-07")
def test_invalid_payloads_are_rejected(client: Any) -> None:
    session = client.post("/api/v1/sessions").json()
    url = f"/api/v1/sessions/{session['session_id']}/messages"
    bad_short_id = client.post(url, json={"request_id": "x", "trip_revision": 1, "text": "a"})
    assert bad_short_id.status_code == 422
    bad_float = client.post(
        url,
        json={
            "request_id": "req-e2e-0040",
            "trip_revision": 1,
            "form": trip_form(budget_amount=1000.5, budget_currency="RUB"),
        },
    )
    assert bad_float.status_code == 422
    extra = client.post(
        url, json={"request_id": "req-e2e-0041", "trip_revision": 1, "text": "a", "admin": True}
    )
    assert extra.status_code == 422
    assert extra.json()["error_code"] == "validation_error"


@pytest.mark.req("UC-03")
def test_city_outside_demo_directory_is_not_invented(client: Any) -> None:
    _, turn = _start(client, destination="Урюпинск")
    state = turn["state"]
    assert state["hotel_offers"] == [] and state["transport_offers"] == []
    assert state["search_termination"]["reason"] == "provider_unavailable"
    assert state["search_termination"]["unchecked_sources"]
    assert state["plan"]["components"] == []
    assert "не означает, что вариантов нет" in turn["assistant_text"]
    assert "вариантов нет" not in turn["assistant_text"].replace("не означает, что вариантов нет", "")


@pytest.mark.negative
@pytest.mark.req("T-SEARCH-STOP-01", "ARCH-02", "SEARCH-STOP-01")
def test_call_limit_gives_limits_exhausted_with_unchecked_sources(tmp_path: Any) -> None:
    from odin.app import build_application
    from odin.config import Settings
    from odin.contracts.api import StructuredForm

    settings = Settings.from_env({"ODIN_DB_PATH": str(tmp_path / "x.sqlite3"), "ODIN_CALLS_PER_TURN": "1"})
    app = build_application(settings)
    session = app.orchestrator.create_session()
    result = app.orchestrator.handle_message(
        session_id=session["session_id"],
        request_id="req-limit-0001",
        trip_revision=1,
        form=StructuredForm.model_validate(trip_form()),
    )
    termination = result.state.search_termination
    assert termination is not None and termination.reason == "limits_exhausted"
    assert termination.can_continue is True
    assert [item.source for item in termination.unchecked_sources] == ["odin_mock:search_hotels"]
    assert "odin_mock:search_transport" in termination.checked_sources
    assert "не означает, что вариантов нет" in result.assistant_text
