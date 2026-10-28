"""UC-10: матрица неожиданных запросов на Mock (план §4 S4, K-30, T-NEG-TOOL-*).

Для каждого случая система даёт полезный ограниченный ответ или вопрос — не галлюцинацию
и не ложный успешный сценарий. Все числа ответа проходят проверку фактов (TEST-04).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from tests.conftest import TRIP_START, trip_form

pytestmark = [pytest.mark.e2e_mock, pytest.mark.req("UC-10", "K-30")]

API = "/api/v1"


def _send(client: Any, body: dict[str, Any]) -> dict[str, Any]:
    session = client.post(f"{API}/sessions").json()
    response = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-uc10-0001", "trip_revision": session["trip_revision"], **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _facts_verified(application: Any, trip_id: str) -> bool:
    finished = [e for e in application.storage.list_audit(trip_id) if e["event"] == "turn_finished"]
    return bool(finished) and all(e["payload"]["facts_verified"] for e in finished)


def _broken_hotels(_: dict[str, Any]) -> Any:
    raise RuntimeError("демо-сбой поставщика гостиниц")


CASES = {
    "unknown_city": {"form": trip_form(destination="Урюпинск")},
    "missing_integration_llm": {"text": "Хочу к морю на выходные"},
    "ambiguous_dates_one_date": {"form": trip_form(date_to=None)},
    "unresolved_dates_order": {"form": trip_form(date_to=(TRIP_START - timedelta(days=2)).isoformat())},
    "tool_error": {"form": trip_form()},
    "budget_exceeded": {"form": trip_form(budget_amount="1000", budget_currency="RUB", budget_required=True)},
}


@pytest.mark.req("T-NEG-TOOL-02")
@pytest.mark.parametrize("case", sorted(CASES))
def test_unexpected_request_gives_limited_answer_or_question(
    case: str, client: Any, application: Any
) -> None:
    if case == "tool_error":
        application.registry.handlers["search_hotels"] = _broken_hotels
    turn = _send(client, CASES[case])
    state = turn["state"]
    text = turn["assistant_text"]
    assert turn["status"] != "purchased_demo" and state["cart"] is None
    assert _facts_verified(application, turn["trip_id"]), "числа ответа взяты из таблицы фактов"

    if case == "unknown_city":
        destination = state["request"]["destination"]
        assert destination["lat"] is None and destination["timezone"] is None  # координаты не выдуманы
        assert state["plan"]["components"] == []
        assert state["search_termination"]["reason"] == "provider_unavailable"
        assert "не означает, что вариантов нет" in text
    elif case == "missing_integration_llm":
        assert turn["input_mode"] == "llm_unavailable" and state["plan"] is None
        assert "Свободный диалог недоступен" in text
    elif case == "ambiguous_dates_one_date":
        assert turn["status"] == "awaiting_clarification"
        assert [q["field"] for q in turn["questions"]] == ["date_from"]
        assert state["plan"] is None
    elif case == "unresolved_dates_order":
        assert turn["status"] == "awaiting_clarification"
        assert [q["field"] for q in turn["questions"]] == ["date_to"]
        assert state["request"]["date_range"] is None, "даты не исправляются за пользователя"
    elif case == "tool_error":
        plan = state["plan"]
        assert "hotel" not in {c["role"] for c in plan["components"]}
        assert "компонент «гостиница» не найден" in plan["unknowns"]
        assert plan["completeness"] != "complete" and plan["fully_agreed"] is False
        assert state["search_termination"]["reason"] == "provider_unavailable"
        assert "не означает, что вариантов нет" in text
    elif case == "budget_exceeded":
        statuses = [
            e["status"]
            for e in state["plan"]["constraint_evaluations"]
            if e["constraint_id"] == "c-budget_total"
        ]
        assert statuses == ["violated"]
        assert state["plan"]["fully_agreed"] is False
        assert "Нарушено:" in text
    else:  # pragma: no cover - защита от непокрытого случая матрицы
        pytest.fail(f"случай {case} не проверен")
