"""Independent state-consistency regression tests; no network."""
from __future__ import annotations

import itertools
from typing import Any

import pytest

from tests.conftest import trip_form

pytestmark = pytest.mark.e2e_mock
_counter = itertools.count()
def rid() -> str:
    return f"audit-v4-{next(_counter)}"

def start(client: Any) -> dict:
    session = client.post('/api/v1/sessions').json()
    r = client.post(f"/api/v1/sessions/{session['session_id']}/messages", json={"request_id": rid(), "trip_revision": session['trip_revision'], "form": trip_form()})
    assert r.status_code == 200, r.text
    return r.json()

def cart(client: Any, trip: str, revision: int) -> dict:
    r=client.post(f'/api/v1/trips/{trip}/cart',json={'request_id':rid(),'trip_revision':revision})
    assert r.status_code==201,r.text
    return r.json()

def test_old_cart_decline_cannot_replace_new_cart(client: Any) -> None:
    turn=start(client)
    trip=turn['trip_id']
    first=cart(client,trip,turn['trip_revision'])
    rep=client.post(f'/api/v1/trips/{trip}/replan',json={'request_id':rid(),'trip_revision':first['trip_revision'],'change_set':{'adults':3}})
    assert rep.status_code==200,rep.text
    second=cart(client,trip,rep.json()['trip_revision'])
    response=client.post(f"/api/v1/carts/{first['cart']['cart_id']}/decline",json={'request_id':rid(),'trip_revision':second['trip_revision']})
    assert response.status_code==409, response.text
    current=client.get(f'/api/v1/trips/{trip}')
    assert current.status_code==200
    assert current.json()['cart']['cart_id']==second['cart']['cart_id']

@pytest.mark.parametrize('change', [
    {'budget_amount':'5000','budget_currency':'RUB'}, {'budget_amount':'100000','budget_currency':'RUB'},
    {'adults':3}, {'adults':4}, {'children_ages':[3,14]},
    {'pets':[{'kind':'dog','count':1}]},
    {'breakfast':True}, {'stars_min':4},
    {'transport_modes':['rail']}, {'transport_modes':['flight']},
])
def test_replan_preserves_destination_and_origin(client: Any, change: dict) -> None:
    initial=start(client)
    response=client.post(f"/api/v1/trips/{initial['trip_id']}/replan",json={'request_id':rid(),'trip_revision':initial['trip_revision'],'change_set':change})
    assert response.status_code==200,response.text
    state=response.json()['state']
    assert state['request']['origin']['name']=='Москва'
    assert state['request']['destination']['name']=='Казань'

@pytest.mark.parametrize('change', [
    {'budget_amount':'5000','budget_currency':'RUB'}, {'budget_amount':'100000','budget_currency':'RUB'},
    {'breakfast':True}, {'stars_min':4},
])
def test_non_search_changes_do_not_call_tools(client: Any, application: Any, change: dict) -> None:
    initial=start(client)
    calls=[]
    original=application.registry.invoke
    def spy(req: Any) -> Any:
        calls.append(req.tool_name)
        return original(req)
    application.registry.invoke=spy
    response=client.post(f"/api/v1/trips/{initial['trip_id']}/replan",json={'request_id':rid(),'trip_revision':initial['trip_revision'],'change_set':change})
    assert response.status_code==200,response.text
    assert calls==[]

@pytest.mark.parametrize('change', [
    {'adults':3}, {'adults':4},
    {'children_ages':[3,14]},
    {'pets':[{'kind':'dog','count':1}]},
    {'transport_modes':['rail']}, {'transport_modes':['flight']},
])
def test_dependent_changes_trigger_search(client: Any, application: Any, change: dict) -> None:
    initial=start(client)
    calls=[]
    original=application.registry.invoke
    def spy(req: Any) -> Any:
        calls.append(req.tool_name)
        return original(req)
    application.registry.invoke=spy
    response=client.post(f"/api/v1/trips/{initial['trip_id']}/replan",json={'request_id':rid(),'trip_revision':initial['trip_revision'],'change_set':change})
    assert response.status_code==200,response.text
    assert 'search_transport' in calls and 'search_hotels' in calls

@pytest.mark.parametrize('invalid', [
    {'adults':-1}, {'adults':'many'},
    {'date_from':'not-a-date'}, {'budget_amount':'NaN'},
])
def test_invalid_replan_does_not_change_head(client: Any, invalid: dict) -> None:
    initial=start(client)
    response=client.post(f"/api/v1/trips/{initial['trip_id']}/replan",json={'request_id':rid(),'trip_revision':initial['trip_revision'],'change_set':invalid})
    assert response.status_code in (400,422),response.text
    current=client.get(f"/api/v1/trips/{initial['trip_id']}")
    assert current.status_code==200
    assert current.json()['revision']==initial['trip_revision']

@pytest.mark.parametrize('change', [
    {'adults':3}, {'children_ages':[3,14]},
    {'transport_modes':['rail']}, {'pets':[{'kind':'dog','count':1}]},
    {'budget_amount':'5000','budget_currency':'RUB'},
])
def test_old_cart_decline_after_replan_without_new_cart_is_rejected(client: Any, change: dict) -> None:
    initial=start(client)
    first=cart(client,initial['trip_id'],initial['trip_revision'])
    rep=client.post(f"/api/v1/trips/{initial['trip_id']}/replan",json={'request_id':rid(),'trip_revision':first['trip_revision'],'change_set':change})
    assert rep.status_code==200,rep.text
    before=client.get(f"/api/v1/trips/{initial['trip_id']}").json()
    response=client.post(f"/api/v1/carts/{first['cart']['cart_id']}/decline",json={'request_id':rid(),'trip_revision':rep.json()['trip_revision']})
    assert response.status_code==409,response.text
    after=client.get(f"/api/v1/trips/{initial['trip_id']}").json()
    assert after['revision']==before['revision']
    assert after['cart'] is None
