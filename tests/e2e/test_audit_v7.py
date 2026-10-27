"""Independent cart consistency tests, offline Mock only (snapshot 2026-10-25)."""
from __future__ import annotations

import itertools
from typing import Any

import pytest

from tests.conftest import trip_form

pytestmark = pytest.mark.e2e_mock
SEQ = itertools.count(1)
API = '/api/v1'


def rid() -> str:
    return f'v7-req-{next(SEQ):07d}'


def start(client: Any, **overrides: Any) -> dict[str, Any]:
    session = client.post(f'{API}/sessions').json()
    response = client.post(f"{API}/sessions/{session['session_id']}/messages", json={
        'request_id': rid(), 'trip_revision': session['trip_revision'],
        'form': trip_form(**overrides),
    })
    assert response.status_code == 200, response.text
    return response.json()


def cart(client: Any, turn: dict[str, Any]) -> Any:
    return client.post(f"{API}/trips/{turn['trip_id']}/cart", json={
        'request_id': rid(), 'trip_revision': turn['trip_revision'],
    })


def replan(client: Any, trip_id: str, revision: int, changes: dict[str, Any]) -> Any:
    return client.post(f'{API}/trips/{trip_id}/replan', json={
        'request_id': rid(), 'trip_revision': revision, 'change_set': changes,
    })


def confirm(client: Any, cart_response: dict[str, Any], *, confirmation: str | None = None,
            risk: bool = True, consent: bool = True) -> Any:
    c = cart_response['cart']
    return client.post(f"{API}/carts/{c['cart_id']}/confirm", json={
        'request_id': rid(), 'trip_revision': cart_response['trip_revision'],
        'confirmation_id': confirmation or f'v7-confirm-{next(SEQ):07d}',
        'purchase_consent': consent, 'risk_acknowledgement': risk,
    })


def decline(client: Any, cart_response: dict[str, Any], *, revision: int | None = None) -> Any:
    return client.post(f"{API}/carts/{cart_response['cart']['cart_id']}/decline", json={
        'request_id': rid(), 'trip_revision': revision if revision is not None else cart_response['trip_revision'],
    })


def current(client: Any, trip_id: str) -> dict[str, Any]:
    response = client.get(f'{API}/trips/{trip_id}')
    assert response.status_code == 200
    return response.json()


def test_cart_items_reference_current_plan_and_offers(client: Any) -> None:
    turn = start(client)
    result = cart(client, turn)
    assert result.status_code == 201, result.text
    c = result.json()['cart']
    state = current(client, turn['trip_id'])
    components = {x['component_id']: x for x in state['plan']['components']}
    offers = {x['offer_id']: x for key in ('hotel_offers', 'transport_offers', 'transfer_options')
              for x in state[key]}
    assert c['trip_revision'] == state['revision']
    assert len(c['items']) == len(components)
    for item in c['items']:
        component = components[item['component_id']]
        assert item['role'] == component['role']
        assert item['kind'] == component['kind']
        offer = offers[component['offer_id']]
        expected = offer['price_total'] if item['kind'] == 'hotel' else offer['price']
        assert item['price'] == expected


def test_cart_retains_partial_price_and_unknown_availability(client: Any) -> None:
    turn = start(client)
    response = cart(client, turn)
    assert response.status_code == 201
    c = response.json()['cart']
    assert c['completeness'] == turn['state']['plan']['completeness']
    assert c['total_price'] == turn['state']['plan']['total_price']
    assert all(i['availability_status'] == 'unknown' for i in c['items'])
    assert c['significant_unknowns']
    assert 'Реального бронирования и списания средств нет' in c['demo_notice']


def test_cart_does_not_claim_full_price_when_partial(client: Any) -> None:
    c = cart(client, start(client)).json()['cart']
    assert c['completeness'] == 'partial'
    assert any('стоимость неполная' in risk for risk in c['significant_unknowns'])


@pytest.mark.parametrize('action', ['confirm', 'decline'])
def test_stale_cart_action_rejected_after_replan(client: Any, action: str) -> None:
    turn = start(client)
    old = cart(client, turn).json()
    updated = replan(client, turn['trip_id'], old['trip_revision'], {'budget_amount': '200000', 'budget_currency': 'RUB'})
    assert updated.status_code == 200, updated.text
    revision = updated.json()['trip_revision']
    before = current(client, turn['trip_id'])
    if action == 'confirm':
        response = confirm(client, {**old, 'trip_revision': revision})
    else:
        response = decline(client, old, revision=revision)
    assert response.status_code == 409, response.text
    after = current(client, turn['trip_id'])
    assert after == before


def test_old_cart_cannot_replace_new_cart_on_decline(client: Any) -> None:
    turn = start(client)
    old = cart(client, turn).json()
    changed = replan(client, turn['trip_id'], old['trip_revision'], {'adults': 3})
    assert changed.status_code == 200, changed.text
    new = cart(client, changed.json()).json()
    before = current(client, turn['trip_id'])
    response = decline(client, old, revision=new['trip_revision'])
    assert response.status_code == 409, response.text
    assert current(client, turn['trip_id']) == before


def test_reopen_cart_invalidates_old_revision(client: Any) -> None:
    turn = start(client)
    first = cart(client, turn).json()
    second_response = cart(client, {**turn, 'trip_revision': first['trip_revision']})
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    assert second['cart']['cart_id'] != first['cart']['cart_id']
    assert confirm(client, first).status_code == 409
    assert second['cart']['trip_revision'] == current(client, turn['trip_id'])['revision']


def test_double_confirmation_is_idempotent(client: Any, application: Any) -> None:
    turn = start(client)
    c = cart(client, turn).json()
    confirmation = f'v7-repeat-{next(SEQ):07d}'
    first = confirm(client, c, confirmation=confirmation)
    second = confirm(client, c, confirmation=confirmation)
    assert first.status_code == second.status_code == 200
    assert second.json()['idempotent_replay'] is True
    assert second.json()['receipt'] == first.json()['receipt']
    events = [x for x in application.storage.list_audit(turn['trip_id']) if x['event'] == 'demo_purchase']
    assert len(events) == 1


@pytest.mark.parametrize('risk,consent', [(False, True), (True, False)])
def test_confirmation_requires_explicit_consents(client: Any, risk: bool, consent: bool) -> None:
    turn = start(client)
    c = cart(client, turn).json()
    before = current(client, turn['trip_id'])
    result = confirm(client, c, risk=risk, consent=consent)
    assert result.status_code == 400, result.text
    assert current(client, turn['trip_id']) == before


def test_receipt_never_claims_real_purchase(client: Any) -> None:
    turn = start(client)
    c = cart(client, turn).json()
    response = confirm(client, c)
    assert response.status_code == 200, response.text
    receipt = response.json()['receipt']
    assert 'Реального бронирования и списания средств нет' in receipt['notice']
    assert receipt['total_price'] == c['cart']['total_price']
    assert receipt['completeness'] == c['cart']['completeness']
    assert receipt['items'] == c['cart']['items']


def test_decline_preserves_no_receipt(client: Any) -> None:
    turn = start(client)
    c = cart(client, turn).json()
    response = decline(client, c)
    assert response.status_code == 200, response.text
    assert current(client, turn['trip_id'])['status'] != 'purchased_demo'


def test_stale_revision_cannot_open_new_cart(client: Any) -> None:
    turn = start(client)
    first = cart(client, turn)
    assert first.status_code == 201
    before = current(client, turn['trip_id'])
    response = cart(client, turn)
    assert response.status_code == 409
    assert current(client, turn['trip_id']) == before


def test_invalid_replan_preserves_existing_cart(client: Any) -> None:
    turn = start(client)
    c = cart(client, turn).json()
    before = current(client, turn['trip_id'])
    response = replan(client, turn['trip_id'], c['trip_revision'], {'adults': -100})
    assert response.status_code in (400, 422), response.text
    assert current(client, turn['trip_id']) == before


@pytest.mark.parametrize('overrides', [
    {'adults': 1},
    {'adults': 3},
    {'adults': 2, 'children_ages': [7]},
    {'adults': 2, 'children_ages': [3, 14]},
])
def test_cart_price_provenance_matches_guest_count(client: Any, overrides: dict[str, Any]) -> None:
    turn = start(client, **overrides)
    c = cart(client, turn)
    assert c.status_code == 201, c.text
    state = current(client, turn['trip_id'])
    adults = state['request']['party']['adults']
    children = state['request']['party']['children_ages']
    for item in c.json()['cart']['items']:
        params = item['source'].get('request_parameters') or {}
        if 'adults' in params:
            assert params['adults'] == adults
        if 'children_ages' in params:
            assert params['children_ages'] == children


@pytest.mark.parametrize('role', ['hotel', 'outbound', 'return'])
def test_cart_price_provenance_has_correct_dates(client: Any, role: str) -> None:
    turn = start(client)
    c = cart(client, turn)
    assert c.status_code == 201, c.text
    request = current(client, turn['trip_id'])['request']
    item = next(i for i in c.json()['cart']['items'] if i['role'] == role)
    params = item['source']['request_parameters']
    if role == 'hotel':
        assert params['check_in'] == request['date_range']['start']
        assert params['check_out'] == request['date_range']['end']
    else:
        date = request['date_range']['start' if role == 'outbound' else 'end']
        assert params['date'] == date


def test_cart_unknown_price_is_not_silently_zero(client: Any) -> None:
    c = cart(client, start(client)).json()['cart']
    transfers = [item for item in c['items'] if item['kind'] == 'transfer']
    assert transfers
    for item in transfers:
        assert item['price']['amount'] is None
        assert item['price']['total_completeness'] == 'unknown'
        assert item['informational'] is True
        assert 'не подтверждена' in item['note']


def test_receipt_preserves_unknown_availability_and_price(client: Any) -> None:
    c = cart(client, start(client)).json()
    receipt_response = confirm(client, c)
    assert receipt_response.status_code == 200, receipt_response.text
    receipt = receipt_response.json()['receipt']
    assert all(item['availability_status'] == 'unknown' for item in receipt['items'])
    assert receipt['completeness'] == 'partial'
    assert any(item['price']['amount'] is None for item in receipt['items'])


def test_replan_guest_count_keeps_new_cart_price_provenance(client: Any) -> None:
    turn = start(client)
    original = cart(client, turn).json()
    response = replan(client, turn['trip_id'], original['trip_revision'], {'adults': 3})
    assert response.status_code == 200, response.text
    new_cart = cart(client, response.json())
    assert new_cart.status_code == 201, new_cart.text
    for item in new_cart.json()['cart']['items']:
        params = item['source'].get('request_parameters') or {}
        if 'adults' in params:
            assert params['adults'] == 3
    assert confirm(client, original).status_code == 409
