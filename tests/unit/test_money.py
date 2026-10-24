"""T-UNIT-MONEY-01: Decimal переживает сериализацию туда и обратно (PRICE-01, CONTRACT-02).

Деньги — Decimal; на границе JSON — строка; float и логические значения отклоняются.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from odin.contracts import NORMATIVE_MODELS, Price
from odin.contracts.api import StructuredForm
from odin.contracts.common import Deviation
from odin.contracts.trip import Budget
from tests.samples import NORMATIVE_SAMPLES, price, trip_state

pytestmark = [pytest.mark.unit, pytest.mark.req("PRICE-01", "CONTRACT-02", "T-UNIT-MONEY-01")]

ROUND_TRIP_AMOUNTS = ["0", "0.01", "0.10", "1", "12.30", "27000.00", "123456.7", "999999999999.99"]


@pytest.mark.parametrize("text", ROUND_TRIP_AMOUNTS)
def test_price_amount_round_trips_through_json_exactly(text: str) -> None:
    original = price(amount=Decimal(text))
    raw = original.model_dump_json()

    assert json.loads(raw)["amount"] == text  # строка, без потери знаков и без экспоненты
    restored = Price.model_validate_json(raw)
    assert restored == original
    assert isinstance(restored.amount, Decimal)
    assert str(restored.amount) == text  # сохраняется и масштаб: 0.10 не превращается в 0.1


@pytest.mark.parametrize("text", ROUND_TRIP_AMOUNTS)
def test_price_amount_round_trips_through_python_mode(text: str) -> None:
    original = price(amount=Decimal(text))
    dumped = original.model_dump()
    assert isinstance(dumped["amount"], Decimal)
    assert Price.model_validate(dumped) == original


def test_signed_money_round_trips_negative_deviation() -> None:
    original = Deviation(amount=Decimal("-1500.50"), unit="RUB", description="превышение бюджета")
    raw = original.model_dump_json()
    assert json.loads(raw)["amount"] == "-1500.50"
    assert Deviation.model_validate_json(raw) == original


def test_money_inside_nested_contracts_round_trips() -> None:
    original = trip_state()
    restored = type(original).model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.request is not None and restored.request.budget is not None
    assert restored.request.budget.amount == Decimal("30000.00")
    assert str(restored.hotel_offers[0].price_total.amount) == "27000.00"


def _money_values(node: Any, path: str = "$") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("amount", "budget_amount"):
                found.append((f"{path}.{key}", value))
            found.extend(_money_values(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_money_values(value, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("name", sorted(NORMATIVE_MODELS))
def test_money_is_never_a_json_number(name: str) -> None:
    payload = json.loads(NORMATIVE_SAMPLES[name]().model_dump_json())
    for path, value in _money_values(payload):
        assert value is None or isinstance(value, str), f"{name}: {path} = {value!r}"


def test_json_schema_declares_money_as_string() -> None:
    amount = Price.model_json_schema()["properties"]["amount"]
    variants = {variant.get("type") for variant in amount["anyOf"]}
    assert variants == {"string", "null"}


@pytest.mark.negative
@pytest.mark.parametrize("bad", [0.1, 27000.0, True, False])
def test_float_and_bool_are_rejected_in_python_input(bad: object) -> None:
    with pytest.raises(ValidationError):
        price(amount=bad)


@pytest.mark.negative
@pytest.mark.parametrize("bad_json", ["27000.5", "1e3", "true"])
def test_float_and_bool_are_rejected_in_json_input(bad_json: str) -> None:
    payload = json.loads(price().model_dump_json())
    raw = json.dumps(payload).replace('"amount": "27000.00"', f'"amount": {bad_json}')
    assert bad_json in raw
    with pytest.raises(ValidationError):
        Price.model_validate_json(raw)


@pytest.mark.negative
@pytest.mark.parametrize(
    "bad",
    ["1.005", "-1", "1000000000000.00", "NaN", "Infinity", "-Infinity", "abc", ""],
)
def test_invalid_money_strings_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        price(amount=bad)


@pytest.mark.negative
def test_budget_and_form_money_reject_float() -> None:
    with pytest.raises(ValidationError):
        Budget(amount=30000.0, currency="RUB", scope="trip_total")
    with pytest.raises(ValidationError):
        StructuredForm(budget_amount=30000.0, budget_currency="RUB")
    form = StructuredForm(budget_amount="30000.00", budget_currency="RUB")
    assert StructuredForm.model_validate_json(form.model_dump_json()) == form
