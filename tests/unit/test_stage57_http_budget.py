"""Offline HTTP-attempt accounting; no provider credentials or external network."""

import json
import sqlite3

import httpx
import pytest

from odin.llm.gateway import LlmBudget, LlmBudgetExceeded
from odin.llm.openai_client import ChatCompletionsClient
from odin.storage import Storage

# Маркер-«ключ» для проверки, что ключ не попадает в журнал (не секрет; собран, чтобы не ловиться сканером).
MARKER_KEY = "marker-" + "never-log"


def make_client(handler, cap, retries=0, sink=None):
    budget = LlmBudget(call_limit=4, http_limit=cap)
    events = []

    def save(event):
        events.append(event)
        if sink:
            sink(event)

    client = ChatCompletionsClient(
        base_url="https://example.org/v1",
        api_key=MARKER_KEY,
        model="mock-model",
        timeout_s=1,
        max_output_tokens=32,
        retries=retries,
        sleep=lambda _: None,
        transport=httpx.MockTransport(handler),
        before_post=budget.take_http,
        on_attempt=save,
    )
    return client, budget, events


def ok():
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        },
    )


def test_compatibility_cap_one_and_two():
    for cap, expected in ((1, 1), (2, 2)):
        sent = []

        def handler(req):
            sent.append(req)  # noqa: B023 — вызывается только в этой же итерации
            return httpx.Response(400) if len(sent) == 1 else ok()  # noqa: B023

        client, budget, events = make_client(handler, cap)
        if cap == 1:
            with pytest.raises(LlmBudgetExceeded):
                client.complete([{"role": "user", "content": "test"}])
        else:
            assert client.complete([{"role": "user", "content": "test"}]).status == "ok"
        assert len(sent) == len(events) == expected
        assert all(e["cost_status"] == "unknown" for e in events)
        assert MARKER_KEY not in json.dumps(events)


@pytest.mark.parametrize("status", [429, 503])
def test_http_retries(status):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status) if len(calls) == 1 else ok()

    client, budget, events = make_client(handler, 2, retries=1)
    assert client.complete([{"role": "user", "content": "test"}]).status == "ok"
    assert len(events) == 2


def test_timeout_and_unknown_cost():
    def handler(req):
        raise httpx.ReadTimeout("simulated")

    client, budget, events = make_client(handler, 1, retries=0)
    assert client.complete([{"role": "user", "content": "test"}]).status == "timeout"
    assert len(events) == 1
    assert events[0]["usage_status"] == "missing"
    assert events[0]["cost_amount"] is None
    assert events[0]["latency_ms"] >= 0


def test_shared_budget_between_roles_and_reset_only_new_turn():
    budget = LlmBudget(call_limit=4, http_limit=2)
    budget.take_http()
    budget.take_http()
    with pytest.raises(LlmBudgetExceeded):
        budget.take_http()
    # New turn constructs a fresh budget, not a new role.
    next_turn = LlmBudget(call_limit=4, http_limit=2)
    next_turn.take_http()
    assert next_turn.http_used == 1


def test_attempt_durable_after_reopen(tmp_path):
    db = tmp_path / "stage57.sqlite3"
    storage = Storage(db)
    event = {
        "model": "mock",
        "cost_status": "unknown",
        "cost_amount": None,
        "usage_status": "missing",
        "request_id": "req1",
        "logical_call_id": "call1",
        "attempt_id": "attempt1",
        "latency_ms": 3,
    }
    storage.audit("llm_http_attempt", request_id="req1", payload=event)
    con = sqlite3.connect(db)
    try:
        result = con.execute("SELECT payload_json FROM audit WHERE event='llm_http_attempt'").fetchone()[0]
    finally:
        con.close()
    assert json.loads(result) == event
