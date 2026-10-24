"""S4: политика уточнений UC-02, допущения UC-03 (DEC-25), детектор изменений и таблица REPLAN-01."""

from __future__ import annotations

from datetime import date

import pytest

from odin.contracts.api import StructuredForm
from odin.contracts.trip import Assumption
from odin.core import clarify
from odin.core.replan import (
    ALL_ROLES,
    REPLAN_TABLE,
    changed_groups,
    invalidated_roles,
    split_change_set,
)
from odin.llm.composer import ComposerFacts, compose_answer, safe_answer
from odin.llm.interpreter import interpret_structured_form

FULL = {
    "origin": "Москва",
    "destination": "Казань",
    "date_from": date(2026, 11, 20),
    "date_to": date(2026, 11, 22),
    "adults": 2,
}


def _by_field(form: StructuredForm) -> dict[str, clarify.ClarificationDecision]:
    return {decision.field: decision for decision in clarify.decide(form)}


# --------------------------------------------------------------------- UC-02: классы вопросов


@pytest.mark.req("UC-02", "T-UNIT-CLARIFY-01")
def test_three_blocking_classes_and_only_blocking_fields_are_asked() -> None:
    decisions = _by_field(StructuredForm())
    assert decisions["origin"].blocking_class == "blocking_now" and decisions["origin"].ask
    assert decisions["destination"].blocking_class == "blocking_now" and decisions["destination"].ask
    assert decisions["date_from"].blocking_class == "blocking_now" and decisions["date_from"].ask
    assert decisions["adults"].blocking_class == "assumable_only_if_authorized"
    assert decisions["adults"].ask, "DEC-25 не разрешает допущений: состав группы спрашивается"
    for optional in ("budget_amount", "pets"):
        assert decisions[optional].blocking_class == "optional" and not decisions[optional].ask
    # Один компактный раунд: все блокирующие вопросы сразу, без необязательных.
    asked = [decision.field for decision in clarify.questions(list(decisions.values()))]
    assert asked == ["origin", "destination", "date_from", "adults"]


@pytest.mark.req("UC-02", "T-UNIT-CLARIFY-01")
def test_complete_form_asks_nothing() -> None:
    assert clarify.questions(clarify.decide(StructuredForm(**FULL))) == []


@pytest.mark.req("UC-02", "T-UNIT-CLARIFY-02")
def test_unknown_and_not_applicable_are_distinct() -> None:
    inspiration = _by_field(StructuredForm(origin="Москва", adults=2, wishes_tags=["море"]))
    assert inspiration["destination"].status == "not_applicable"
    assert not inspiration["destination"].ask
    # Для вдохновения даты не блокируют (стоимость требует дат), но остаются неизвестными.
    assert inspiration["date_from"].status == "unknown"
    assert inspiration["date_from"].blocking_class == "optional" and not inspiration["date_from"].ask

    missing = _by_field(StructuredForm(origin="Москва", adults=2))
    assert missing["destination"].status == "unknown" and missing["destination"].ask

    invalid = _by_field(StructuredForm(**{**FULL, "date_to": date(2026, 11, 19)}))
    assert invalid["date_to"].status == "invalid" and invalid["date_to"].ask
    one_date = _by_field(StructuredForm(**{**FULL, "date_to": None}))
    assert one_date["date_from"].ask, "вторая дата не предполагается"


@pytest.mark.req("UC-02", "T-UNIT-CLARIFY-03")
def test_answer_supplements_previous_data_without_repeating_questions() -> None:
    first = StructuredForm(
        origin="Москва",
        destination="Казань",
        date_from=FULL["date_from"],
        date_to=FULL["date_to"],
        budget_amount="30000",
        budget_currency="RUB",
        budget_required=True,
    )
    assert [d.field for d in clarify.questions(clarify.decide(first))] == ["adults"]
    merged = clarify.merge_answer(first, StructuredForm(adults=2))
    assert clarify.questions(clarify.decide(merged)) == []
    assert merged.origin == "Москва" and merged.destination == "Казань"
    assert merged.budget_required is True, "обязательное условие не теряется при ответе"
    # Новое непустое значение заменяет прежнее.
    assert clarify.merge_answer(first, StructuredForm(origin="Казань")).origin == "Казань"


# --------------------------------------------------------------------- UC-03: допущения


@pytest.mark.req("UC-03", "DEC-25", "T-UNIT-ASSUME-01", "K-15")
def test_no_assumptions_are_made_and_party_is_asked() -> None:
    outcome = interpret_structured_form(
        StructuredForm(origin="Москва", destination="Казань"), session_id="s", request_id="r"
    )
    request = outcome.request
    assert request is not None
    assert request.assumptions == [], "DEC-25: допущения не делаются"
    assert request.party.adults is None
    assert request.date_range is None and request.budget is None
    assert {q.field for q in outcome.questions} == {"date_from", "adults"}
    assert request.ambiguities == outcome.questions, "открытые вопросы видны в запросе"
    for field_name in clarify.NEVER_ASSUMED:
        decision = next((d for d in clarify.decide(StructuredForm()) if d.field == field_name), None)
        assert decision is None or decision.blocking_class != "assumable_only_if_authorized"


@pytest.mark.req("UC-03", "DEC-25", "T-UNIT-ASSUME-01")
def test_assumptions_are_shown_next_to_the_sum() -> None:
    base = {
        "destination": "Казань",
        "origin": "Москва",
        "known_amount": "27000.00",
        "currency": "RUB",
        "completeness": "partial",
    }
    text = compose_answer(ComposerFacts(**base))
    lines = text.splitlines()
    total_index = next(i for i, line in enumerate(lines) if line.startswith("Итог:"))
    assert "Допущений в расчёте нет" in lines[total_index + 2]

    assumption = Assumption(
        assumption_id="as-1",
        field="nights",
        value=2,
        reason="ориентировочный расчёт",
        shown_with_price=True,
        authorized_by="user",
    )
    shown = f"{assumption.field}: {assumption.value} ({assumption.reason})"
    text, verified = safe_answer(ComposerFacts(**base, assumptions=(shown,)))
    lines = text.splitlines()
    total_index = next(i for i, line in enumerate(lines) if line.startswith("Итог:"))
    assert verified and lines[total_index + 2].startswith("Допущения расчёта: nights: 2")


# --------------------------------------------------------------------- REPLAN-01: таблица и детектор


@pytest.mark.req("REPLAN-01", "T-UNIT-REPLAN-02")
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"date_from": "2026-11-27"}, set(ALL_ROLES)),  # даты: транспорт, отель, трансфер
        ({"budget_amount": "5000"}, set()),  # бюджет: оценка и итог, без нового поиска
        ({"pets": [{"kind": "dog", "count": 1}]}, set(ALL_ROLES)),  # животное
        ({"adults": 3}, set(ALL_ROLES)),  # состав группы
        ({"destination": "Сочи"}, set(ALL_ROLES)),  # направление
        ({"origin": "Казань"}, {"outbound", "return", "transfer_in", "transfer_out"}),
        ({"transport_modes": ["flight"]}, {"outbound", "return", "transfer_in", "transfer_out"}),
        ({"breakfast": True}, set()),
    ],
)
def test_all_rows_of_tz_table_are_covered(change: dict[str, object], expected: set[str]) -> None:
    roles, reasons = invalidated_roles(change)
    assert set(roles) == expected and reasons


@pytest.mark.req("REPLAN-01", "T-UNIT-REPLAN-03")
def test_change_detector_and_required_flags() -> None:
    old = StructuredForm(**FULL, budget_amount="30000", budget_currency="RUB")
    new = StructuredForm(
        **{**FULL, "adults": 3}, budget_amount="30000", budget_currency="RUB", budget_required=True
    )
    changes = clarify.detect_changes(old, new)
    assert changes == {"adults": 3, "budget_required": True}
    table, flags = split_change_set(changes)
    assert table == {"adults": 3} and flags == {"budget_required": True}
    assert changed_groups(changes) == ["состав группы", "бюджет"]
    assert clarify.detect_changes(old, old) == {}
    assert {rule.group for rule in REPLAN_TABLE} >= {
        "dates",
        "budget",
        "pets",
        "party",
        "destination",
        "origin",
    }
