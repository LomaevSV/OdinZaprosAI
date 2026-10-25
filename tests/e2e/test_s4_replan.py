"""S4: T-E2E-MOCK-10..14 — пересборка, корзина и актуальность на Mock по JSON API.

- 10: смена дат (K-22) — условия сохранены, зависимые компоненты перепроверены, корзина переоценена;
- 11: уменьшение бюджета (K-23) — без нового поиска, явный выбор сохранён, бюджет переоценён;
- 12: «едем с собакой» (K-24) и +1 взрослый (K-25) — правила отеля/транспорта перепроверены;
- 13: устаревшая корзина — старая ревизия → 409, `stale` → перепроверка или отдельное согласие;
- 14: неудачная пересборка сохраняет последнюю согласованную ревизию; устаревший оффер не переносится.
Дополнительно: диалог без повторных вопросов (UC-02), перехват вызовов — внешний checkout не вызывается.
Сеть заблокирована фикстурой conftest; Live (T-E2E-LIVE-02) — BLOCKED (DEC-04/07/09).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from odin.core.replan import ALL_ROLES
from tests.conftest import TRIP_END, TRIP_START, trip_form

pytestmark = [pytest.mark.e2e_mock, pytest.mark.req("UC-09", "REPLAN-01", "REPLAN-02", "ARCH-05")]

API = "/api/v1"
_ids = itertools.count(1)


def _rid() -> str:
    return f"req-s4-{next(_ids):05d}"


def _start(client: Any, **overrides: Any) -> dict[str, Any]:
    session = client.post(f"{API}/sessions").json()
    response = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={
            "request_id": _rid(),
            "trip_revision": session["trip_revision"],
            "form": trip_form(**overrides),
        },
    )
    assert response.status_code == 200, response.text
    turn = response.json()
    assert turn["status"] == "planned", turn["assistant_text"]
    return turn


def _message(client: Any, turn: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"{API}/sessions/{turn['session_id']}/messages",
        json={"request_id": _rid(), "trip_revision": turn["trip_revision"], "form": form},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _replan(client: Any, trip_id: str, revision: int, change_set: dict[str, Any]) -> Any:
    return client.post(
        f"{API}/trips/{trip_id}/replan",
        json={"request_id": _rid(), "trip_revision": revision, "change_set": change_set},
    )


def _select(client: Any, trip_id: str, revision: int, selections: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        f"{API}/trips/{trip_id}/selections",
        json={"request_id": _rid(), "trip_revision": revision, "selections": selections},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cart(client: Any, trip_id: str, revision: int) -> dict[str, Any]:
    response = client.post(
        f"{API}/trips/{trip_id}/cart", json={"request_id": _rid(), "trip_revision": revision}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _confirm(client: Any, cart: dict[str, Any], *, risk: bool = True, confirmation: str | None = None) -> Any:
    return client.post(
        f"{API}/carts/{cart['cart']['cart_id']}/confirm",
        json={
            "request_id": _rid(),
            "trip_revision": cart["trip_revision"],
            "confirmation_id": confirmation or f"conf-{_rid()}",
            "purchase_consent": True,
            "risk_acknowledgement": risk,
        },
    )


def _replan_events(application: Any, trip_id: str) -> list[dict[str, Any]]:
    return [
        entry["payload"] for entry in application.storage.list_audit(trip_id) if entry["event"] == "replan"
    ]


def _evaluation(state: dict[str, Any], constraint_id: str) -> str:
    statuses = [
        e["status"] for e in state["plan"]["constraint_evaluations"] if e["constraint_id"] == constraint_id
    ]
    assert statuses, f"условие {constraint_id} не оценено"
    return statuses[0]


def _non_top_hotel(state: dict[str, Any]) -> str:
    current = state["selected_components"]["hotel"]
    return next(offer["offer_id"] for offer in state["hotel_offers"] if offer["offer_id"] != current)


def _shift_clock(application: Any, monkeypatch: pytest.MonkeyPatch, minutes: int) -> None:
    """Сдвиг времени: и для проверки актуальности, и для «наблюдения» новых офферов Mock."""
    shifted = lambda: datetime.now(tz=UTC) + timedelta(minutes=minutes)  # noqa: E731
    application.orchestrator.clock = shifted
    monkeypatch.setattr("odin.tools.mock_adapters._now", shifted)


def _spy(application: Any) -> list[str]:
    """Перехват вызовов инструментов: список имён в порядке вызова."""
    calls: list[str] = []
    original = application.registry.invoke

    def spy(request: Any) -> Any:
        calls.append(request.tool_name)
        return original(request)

    application.registry.invoke = spy
    return calls


# --------------------------------------------------------------------- T-E2E-MOCK-10


@pytest.mark.req("T-E2E-MOCK-10", "K-22", "TEST-05.9")
def test_replan_dates_preserves_conditions_and_reprices_cart(client: Any, application: Any) -> None:
    turn = _start(client, breakfast=True, budget_amount="300000", budget_currency="RUB", budget_required=True)
    trip_id = turn["trip_id"]
    chosen = _non_top_hotel(turn["state"])
    selected = _select(client, trip_id, turn["trip_revision"], {"hotel": chosen})
    cart_one = _cart(client, trip_id, selected["revision"])

    new_start, new_end = TRIP_START + timedelta(days=7), TRIP_END + timedelta(days=8)
    response = _replan(
        client,
        trip_id,
        cart_one["trip_revision"],
        {"date_from": new_start.isoformat(), "date_to": new_end.isoformat()},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    state = result["state"]
    assert result["status"] == "planned" and state["cart"] is None
    request = state["request"]
    assert request["date_range"]["start"] == new_start.isoformat()
    assert request["date_range"]["end"] == new_end.isoformat()
    # Неизменённые условия сохранены (K-22): бюджет обязательный, завтрак, состав, маршрут.
    constraints = {c["constraint_id"]: c for c in request["constraints"]}
    assert constraints["c-budget_total"]["requiredness"] == "required"
    assert constraints["c-hotel_breakfast"]["value"] is True
    assert request["party"]["adults"] == 2
    assert request["origin"]["name"] == "Москва" and request["destination"]["name"] == "Казань"
    # Все зависимые компоненты перепроверены новым поиском по новым датам.
    assert all(offer["check_in"] == new_start.isoformat() for offer in state["hotel_offers"])
    outbound = [o for o in state["transport_offers"] if o["direction"] == "outbound"]
    assert outbound and all(o["departure_at"].startswith(new_start.isoformat()) for o in outbound)
    event = _replan_events(application, trip_id)[-1]
    assert set(event["invalidated_components"]) == set(ALL_ROLES)
    assert event["reused_components"] == [] and event["validation_result"] == "ok"
    assert "Пересборка: изменено — даты" in result["assistant_text"]

    # Старая корзина потеряла право подтверждения; новая — переоценена (ночей больше).
    assert _confirm(client, cart_one).status_code == 409
    cart_two = _cart(client, trip_id, result["trip_revision"])
    assert cart_two["cart"]["cart_id"] != cart_one["cart"]["cart_id"]
    hotel_one = next(i for i in cart_one["cart"]["items"] if i["role"] == "hotel")
    hotel_two = next(i for i in cart_two["cart"]["items"] if i["role"] == "hotel")
    assert Decimal(hotel_two["price"]["amount"]) > Decimal(hotel_one["price"]["amount"])


# --------------------------------------------------------------------- T-E2E-MOCK-11


@pytest.mark.req("T-E2E-MOCK-11", "K-23")
def test_budget_reduction_reevaluates_without_new_search(client: Any, application: Any) -> None:
    turn = _start(client, budget_amount="300000", budget_currency="RUB", budget_required=True)
    trip_id = turn["trip_id"]
    assert _evaluation(turn["state"], "c-budget_total") != "violated"
    chosen = _non_top_hotel(turn["state"])
    selected = _select(client, trip_id, turn["trip_revision"], {"hotel": chosen})
    calls = _spy(application)

    response = _replan(
        client, trip_id, selected["revision"], {"budget_amount": "5000", "budget_currency": "RUB"}
    )
    assert response.status_code == 200, response.text
    state = response.json()["state"]
    assert calls == [], "бюджет не требует нового поиска при свежих офферах"
    # Явный выбор сохранён; бюджет переоценён по итогу; обязательность не потеряна.
    assert state["selected_components"]["hotel"] == chosen
    assert _evaluation(state, "c-budget_total") == "violated"
    assert state["plan"]["fully_agreed"] is False
    constraint = next(c for c in state["request"]["constraints"] if c["constraint_id"] == "c-budget_total")
    assert constraint["requiredness"] == "required" and constraint["value"] == "5000"
    event = _replan_events(application, trip_id)[-1]
    assert event["invalidated_components"] == []
    assert "hotel" in event["reused_components"]


# --------------------------------------------------------------------- T-E2E-MOCK-12


@pytest.mark.req("T-E2E-MOCK-12", "K-24")
def test_dog_rechecks_hotel_and_transport_rules(client: Any, application: Any) -> None:
    turn = _start(client, breakfast=True)
    trip_id = turn["trip_id"]
    response = _replan(
        client, trip_id, turn["trip_revision"], {"pets": [{"kind": "dog", "count": 1}], "pets_required": True}
    )
    assert response.status_code == 200, response.text
    state = response.json()["state"]
    event = _replan_events(application, trip_id)[-1]
    assert set(event["invalidated_components"]) == set(ALL_ROLES)
    pets = state["request"]["party"]["pets"]
    assert len(pets) == 1 and pets[0]["kind"] == "dog" and pets[0]["count"] == 1
    # Правило животного оценено у отеля и транспорта; обязательное не превращается в «выполнено» без данных.
    pet_evaluations = {
        e["component_id"]: e["status"]
        for e in state["plan"]["constraint_evaluations"]
        if e["constraint_id"] == "c-pets"
    }
    assert pet_evaluations, "условие о животном оценено"
    hotel_id = state["selected_components"]["hotel"]
    assert pet_evaluations.get(hotel_id) == "matched", "выбран отель, где животные разрешены (RANK-01)"
    assert any(status == "unknown" for status in pet_evaluations.values()), (
        "транспорт: правила не подтверждены"
    )
    # Прежнее условие сохранено.
    assert any(c["constraint_id"] == "c-hotel_breakfast" for c in state["request"]["constraints"])


@pytest.mark.req("T-E2E-MOCK-12", "K-25")
def test_one_more_adult_reprices_and_keeps_conditions(client: Any, application: Any) -> None:
    turn = _start(client, breakfast=True, transport_modes=["rail"])
    trip_id = turn["trip_id"]
    before = Decimal(turn["state"]["plan"]["total_price"]["amount"])
    response = _replan(client, trip_id, turn["trip_revision"], {"adults": 3})
    assert response.status_code == 200, response.text
    state = response.json()["state"]
    assert state["request"]["party"]["adults"] == 3
    assert Decimal(state["plan"]["total_price"]["amount"]) > before
    fields = {c["field"] for c in state["request"]["constraints"]}
    assert {"hotel_breakfast", "transport_mode"} <= fields
    assert set(_replan_events(application, trip_id)[-1]["invalidated_components"]) == set(ALL_ROLES)


# --------------------------------------------------------------------- T-E2E-MOCK-13


@pytest.mark.req("T-E2E-MOCK-13", "DEC-21", "T-UNIT-TTL-02")
def test_stale_cart_requires_recheck_or_separate_consent(
    client: Any, application: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _start(client)
    trip_id = turn["trip_id"]
    cart = _cart(client, trip_id, turn["trip_revision"])
    assert not any("устарело" in risk for risk in cart["cart"]["significant_unknowns"])

    # Прошло больше provisional TTL (30 мин): подтверждение без перепроверки отклоняется.
    application.orchestrator.clock = lambda: datetime.now(tz=UTC) + timedelta(minutes=31)
    rejected = _confirm(client, cart)
    assert rejected.status_code == 409 and rejected.json()["error_code"] == "cart_stale"
    events = [e["event"] for e in application.storage.list_audit(trip_id)]
    assert "demo_purchase" not in events and "demo_purchase_rejected_stale" in events

    # Путь «согласие»: новая корзина перечисляет устаревание как риск; без согласия с риском — отказ.
    consent_cart = _cart(client, trip_id, cart["trip_revision"])
    stale_risks = [r for r in consent_cart["cart"]["significant_unknowns"] if "устарело" in r]
    assert stale_risks
    assert _confirm(client, consent_cart, risk=False).status_code == 400
    accepted = _confirm(client, consent_cart, risk=True)
    assert accepted.status_code == 200, accepted.text
    receipt = accepted.json()["receipt"]
    assert set(stale_risks) <= set(receipt["significant_unknowns"])
    assert receipt["risk_acknowledged_at"] is not None


@pytest.mark.req("T-E2E-MOCK-13", "DEC-21")
def test_stale_cart_recheck_by_new_search(
    client: Any, application: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _start(client)
    trip_id = turn["trip_id"]
    cart = _cart(client, trip_id, turn["trip_revision"])
    _shift_clock(application, monkeypatch, 31)
    assert _confirm(client, cart).status_code == 409
    # Путь «перепроверка»: тот же запрос ещё раз — новый поиск, свежие офферы, корзина без риска устаревания.
    trip = client.get(f"{API}/trips/{trip_id}").json()
    rechecked = _message(client, {**turn, "trip_revision": trip["revision"]}, trip_form())
    fresh_cart = _cart(client, trip_id, rechecked["trip_revision"])
    assert not any("устарело" in risk for risk in fresh_cart["cart"]["significant_unknowns"])
    assert _confirm(client, fresh_cart).status_code == 200


@pytest.mark.req("T-E2E-MOCK-13", "REPLAN-02")
def test_old_revision_is_409_everywhere(client: Any) -> None:
    turn = _start(client)
    trip_id = turn["trip_id"]
    cart = _cart(client, trip_id, turn["trip_revision"])
    stale_revision = turn["trip_revision"]
    assert _replan(client, trip_id, stale_revision, {"adults": 3}).status_code == 409
    assert (
        client.post(
            f"{API}/trips/{trip_id}/cart", json={"request_id": _rid(), "trip_revision": stale_revision}
        ).status_code
        == 409
    )
    replanned = _replan(client, trip_id, cart["trip_revision"], {"adults": 3})
    assert replanned.status_code == 200
    assert _confirm(client, cart).status_code == 409, "прежняя корзина не подтверждается после пересборки"


# --------------------------------------------------------------------- T-E2E-MOCK-14


@pytest.mark.req("T-E2E-MOCK-14", "UC-09", "ARCH-05")
def test_failed_replan_keeps_last_agreed_revision(client: Any, application: Any) -> None:
    turn = _start(client, breakfast=True)
    trip_id = turn["trip_id"]
    plan_id = turn["state"]["plan"]["plan_id"]
    head_before = application.storage.get_head(trip_id)
    assert head_before["last_agreed_revision"] == turn["trip_revision"]

    # Город вне демо-справочника: поиск не находит компонентов — план не собирается.
    failed = _replan(client, trip_id, turn["trip_revision"], {"destination": "Урюпинск"})
    assert failed.status_code == 200, failed.text
    body = failed.json()
    assert body["trip_revision"] == turn["trip_revision"]
    assert body["state"]["plan"]["plan_id"] == plan_id
    assert body["state"]["request"]["destination"]["name"] == "Казань"
    assert "Пересборка не выполнена" in body["assistant_text"]
    assert "не означает, что вариантов нет" in body["assistant_text"]
    head_after = application.storage.get_head(trip_id)
    assert head_after == head_before, "неудачная пересборка не меняет ревизии"
    event = _replan_events(application, trip_id)[-1]
    assert event["validation_result"] == "failed" and event["new_revision"] is None

    # Некорректные даты — вопрос, а план и ревизия прежние.
    invalid = _replan(
        client, trip_id, turn["trip_revision"], {"date_to": (TRIP_START - timedelta(days=1)).isoformat()}
    )
    assert invalid.status_code == 200
    payload = invalid.json()
    assert payload["questions"] and payload["trip_revision"] == turn["trip_revision"]
    assert payload["state"]["plan"]["plan_id"] == plan_id
    # Последний согласованный план пригоден для корзины.
    assert _cart(client, trip_id, turn["trip_revision"])["cart"]["items"]


@pytest.mark.req("T-E2E-MOCK-14", "UC-09", "DEC-21")
def test_stale_selected_offer_is_not_carried_as_active(
    client: Any, application: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _start(client)
    trip_id = turn["trip_id"]
    chosen = _non_top_hotel(turn["state"])
    selected = _select(client, trip_id, turn["trip_revision"], {"hotel": chosen})
    calls = _spy(application)

    _shift_clock(application, monkeypatch, 31)
    response = _replan(
        client, trip_id, selected["revision"], {"budget_amount": "300000", "budget_currency": "RUB"}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert "search_hotels" in calls, "устаревшие офферы перепроверены новым поиском"
    assert "устарели" in result["assistant_text"]
    event = _replan_events(application, trip_id)[-1]
    assert "hotel" not in event["reused_components"], "устаревший оффер не переиспользуется без перепроверки"
    state = result["state"]
    hotel = next(o for o in state["hotel_offers"] if o["offer_id"] == state["selected_components"]["hotel"])
    observed = datetime.fromisoformat(hotel["validity"]["observed_at"])
    assert observed > datetime.fromisoformat(turn["state"]["hotel_offers"][0]["validity"]["observed_at"])
    assert state["selected_components"]["hotel"] == chosen, "выбор сохранён только после перепроверки"
    assert "Перепроверены и сохранены выбранные вами — гостиница" in result["assistant_text"]


# --------------------------------------------------------------------- диалог и перехват вызовов


@pytest.mark.req("UC-02", "K-05")
def test_clarification_answer_is_merged_without_repeated_questions(client: Any) -> None:
    session = client.post(f"{API}/sessions").json()
    first = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={
            "request_id": _rid(),
            "trip_revision": session["trip_revision"],
            "form": trip_form(adults=None),
        },
    ).json()
    assert [q["field"] for q in first["questions"]] == ["adults"]
    assert first["questions"][0]["blocking_class"] == "assumable_only_if_authorized"
    second = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": _rid(), "trip_revision": first["trip_revision"], "form": {"adults": 2}},
    ).json()
    assert second["questions"] == [] and second["status"] == "planned"
    request = second["state"]["request"]
    assert request["origin"]["name"] == "Москва" and request["destination"]["name"] == "Казань"
    assert "Допущений в расчёте нет" in second["assistant_text"]


@pytest.mark.req("UC-09", "REPLAN-01")
def test_changed_form_message_is_detected_as_replan(client: Any, application: Any) -> None:
    turn = _start(client)
    changed = _message(client, turn, trip_form(adults=3))
    assert changed["status"] == "planned"
    event = _replan_events(application, turn["trip_id"])[-1]
    assert event["change_set"] == {"adults": "3"}
    assert set(event["invalidated_components"]) == set(ALL_ROLES)


@pytest.mark.req("UC-08", "ACCEPT-01", "T-E2E-MOCK-01")
def test_external_checkout_is_never_called(client: Any, application: Any) -> None:
    calls = _spy(application)
    turn = _start(client)
    trip_id = turn["trip_id"]
    replanned = _replan(client, trip_id, turn["trip_revision"], {"adults": 3}).json()
    selected = _select(
        client, trip_id, replanned["trip_revision"], {"hotel": _non_top_hotel(replanned["state"])}
    )
    cart = _cart(client, trip_id, selected["revision"])
    assert _confirm(client, cart).status_code == 200
    assert calls, "перехватчик видит вызовы"
    read_only = {"search_transport", "search_hotels", "get_transfer_options", "get_offer_details"}
    assert set(calls) <= read_only
    assert not any(word in tool for tool in calls for word in ("checkout", "book", "pay", "register"))
    purchase = next(e for e in application.storage.list_audit(trip_id) if e["event"] == "demo_purchase")
    assert purchase["payload"]["external_calls"] == 0
