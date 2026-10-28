"""OBS-CONTEXT-03: этапные записи привязаны к request_id своего запроса (cart/replan/message).

Аудит 57.4: `_observed_request_id` жил в полях экземпляра и не сбрасывался — span корзины получал
request_id прошлого хода. Здесь последовательные HTTP-запросы с разными request_id; Mock, без сети и LLM.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import trip_form

API = "/api/v1"


def _spans(application: Any) -> list[tuple[str, str]]:
    with application.storage._connection() as connection:
        rows = connection.execute(
            "SELECT request_id, payload_json FROM audit WHERE event='pipeline_span' ORDER BY audit_id"
        ).fetchall()
    return [(row["request_id"], json.loads(row["payload_json"])["stage"]) for row in rows]


def _stages_for(spans: list[tuple[str, str]], request_id: str) -> set[str]:
    return {stage for rid, stage in spans if rid == request_id}


def test_cart_and_replan_spans_carry_their_own_request_id(client: Any, application: Any) -> None:
    session = client.post(f"{API}/sessions").json()
    turn = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-ctx-msg-1", "trip_revision": session["trip_revision"], "form": trip_form()},
    ).json()
    trip_id = turn["trip_id"]

    cart = client.post(
        f"{API}/trips/{trip_id}/cart",
        json={"request_id": "req-ctx-cart-1", "trip_revision": turn["trip_revision"]},
    )
    assert cart.status_code == 201, cart.text
    cart_body = cart.json()
    declined = client.post(
        f"{API}/carts/{cart_body['cart']['cart_id']}/decline",
        json={"request_id": "req-ctx-decline-1", "trip_revision": cart_body["trip_revision"]},
    )
    assert declined.status_code == 200, declined.text
    current = client.get(f"{API}/trips/{trip_id}").json()["revision"]
    replan = client.post(
        f"{API}/trips/{trip_id}/replan",
        json={
            "request_id": "req-ctx-replan-1",
            "trip_revision": current,
            "change_set": {"adults": 3},
        },
    )
    assert replan.status_code == 200, replan.text

    spans = _spans(application)
    assert "open_cart" in _stages_for(spans, "req-ctx-cart-1")
    assert "decline_cart" in _stages_for(spans, "req-ctx-decline-1")
    assert "_apply_replan" in _stages_for(spans, "req-ctx-replan-1")
    # Ни одна операция корзины/пересборки не приписана прошлому ходу.
    assert _stages_for(spans, "req-ctx-msg-1").isdisjoint({"open_cart", "decline_cart", "_apply_replan"})
    assert all(rid for rid, _ in spans), "span без request_id"


def test_cart_span_in_fresh_instance_is_not_dropped(settings: Any) -> None:
    """Новый экземпляр приложения (как после рестарта): span корзины не теряется и не пустой."""
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application

    first = build_application(settings)
    with TestClient(create_app(application=first)) as client_a:
        session = client_a.post(f"{API}/sessions").json()
        turn = client_a.post(
            f"{API}/sessions/{session['session_id']}/messages",
            json={
                "request_id": "req-fresh-msg",
                "trip_revision": session["trip_revision"],
                "form": trip_form(),
            },
        ).json()

    second = build_application(settings)
    with TestClient(create_app(application=second)) as client_b:
        cart = client_b.post(
            f"{API}/trips/{turn['trip_id']}/cart",
            json={"request_id": "req-fresh-cart", "trip_revision": turn["trip_revision"]},
        )
        assert cart.status_code == 201, cart.text
    assert "open_cart" in _stages_for(_spans(second), "req-fresh-cart")


@pytest.mark.parametrize("broken", ["pipeline_span"])
def test_broken_span_sink_does_not_fail_http_turn(client: Any, application: Any, broken: str) -> None:
    """HTTP-ход и корзина успешны, даже если запись этапных span в SQLite падает."""
    original = application.storage.audit

    def audit(event: str, **kwargs: Any) -> int:
        if event == broken:
            raise RuntimeError("sqlite disk full")
        return original(event, **kwargs)

    application.storage.audit = audit
    session = client.post(f"{API}/sessions").json()
    turn = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-broken-1", "trip_revision": session["trip_revision"], "form": trip_form()},
    )
    assert turn.status_code == 200, turn.text
    body = turn.json()
    cart = client.post(
        f"{API}/trips/{body['trip_id']}/cart",
        json={"request_id": "req-broken-2", "trip_revision": body["trip_revision"]},
    )
    assert cart.status_code == 201, cart.text
