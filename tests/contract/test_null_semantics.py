"""T-CON-NULL-01: неизвестное значение не заменяется значением по умолчанию (CONTRACT-01, CONSTRAINT-01, UC-03).

Правила проверки:
1. У полей нормативных контрактов (и всех вложенных моделей) нет «правдоподобных» значений
   по умолчанию: допускаются только ``None``, пустые коллекции и константы контракта
   (``Literal`` с единственным значением, например ``schema_version``).
2. Пропущенное обязательное nullable-поле — ошибка валидации, а не подстановка.
3. Явный ``null`` сохраняется при сериализации и обратном разборе; поля из вывода не выбрасываются.
4. Неизвестное не превращается в известное: ``unknown`` требует причины, ``complete`` без суммы невозможен.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from odin.contracts import NORMATIVE_MODELS, Party, Price
from tests.introspection import allows_none, iter_fields, normative_closure, single_literal_value
from tests.samples import NORMATIVE_SAMPLES, availability, constraint_evaluation, price

pytestmark = [pytest.mark.contract, pytest.mark.req("CONTRACT-01", "CONSTRAINT-01", "UC-03", "T-CON-NULL-01")]

OPTIONAL_FIELDS = [(m, n, f) for m, n, f in iter_fields(normative_closure()) if not f.is_required()]
REQUIRED_NULLABLE = [
    (m.__name__, n)
    for m, n, f in iter_fields(NORMATIVE_MODELS.values())
    if f.is_required() and allows_none(f)
]


@pytest.mark.parametrize(
    ("model", "name", "field"),
    OPTIONAL_FIELDS,
    ids=[f"{m.__name__}.{n}" for m, n, _ in OPTIONAL_FIELDS],
)
def test_defaults_never_invent_values(model: type, name: str, field) -> None:
    default = field.get_default(call_default_factory=True)
    if field.default_factory is not None:
        assert type(default) in (list, dict) and not default, f"{model.__name__}.{name}: непустая фабрика"
        return
    if default is None:
        return
    is_constant, constant = single_literal_value(field)
    assert is_constant and default == constant, (
        f"{model.__name__}.{name}: значение по умолчанию {default!r} подменяет неизвестное"
    )


def test_structural_checks_are_not_vacuous() -> None:
    assert len(normative_closure()) > len(NORMATIVE_MODELS)  # вложенные модели тоже проверены
    assert len(OPTIONAL_FIELDS) >= 19  # как минимум schema_version у каждого нормативного типа
    assert len(REQUIRED_NULLABLE) >= 40


@pytest.mark.parametrize(("model_name", "field_name"), REQUIRED_NULLABLE)
def test_missing_nullable_field_is_error_not_default(model_name: str, field_name: str) -> None:
    payload = json.loads(NORMATIVE_SAMPLES[model_name]().model_dump_json())
    del payload[field_name]
    with pytest.raises(ValidationError) as err:
        NORMATIVE_MODELS[model_name].model_validate_json(json.dumps(payload))
    assert any(e["loc"] == (field_name,) and e["type"] == "missing" for e in err.value.errors())


@pytest.mark.parametrize("name", sorted(NORMATIVE_MODELS))
def test_explicit_nulls_survive_round_trip(name: str) -> None:
    sample = NORMATIVE_SAMPLES[name]()
    raw = sample.model_dump_json()
    payload = json.loads(raw)

    assert set(payload) == set(type(sample).model_fields)  # ни одно поле не выброшено из вывода
    for field_name in type(sample).model_fields:
        if getattr(sample, field_name) is None:
            assert payload[field_name] is None
    restored = type(sample).model_validate_json(raw)
    assert restored == sample
    assert restored.model_dump() == sample.model_dump()


def test_samples_actually_contain_nulls() -> None:
    nulls = sum(
        1
        for name in NORMATIVE_MODELS
        for value in NORMATIVE_SAMPLES[name]().model_dump().values()
        if value is None
    )
    assert nulls >= 20  # проверка сохранения null не пустая


def test_unknown_party_size_stays_unknown() -> None:
    with pytest.raises(ValidationError) as err:
        Party.model_validate({})
    assert any(e["loc"] == ("adults",) and e["type"] == "missing" for e in err.value.errors())

    party = Party.model_validate_json('{"adults": null}')
    assert party.adults is None and party.rooms is None
    assert json.loads(party.model_dump_json())["adults"] is None


def test_unknown_price_stays_unknown() -> None:
    unknown = price(
        amount=None, currency=None, total_completeness="unknown", unknown_reason="not_provided_by_source"
    )
    restored = Price.model_validate_json(unknown.model_dump_json())
    assert restored.amount is None and restored.currency is None
    assert restored.total_completeness == "unknown"
    assert json.loads(unknown.model_dump_json())["amount"] is None


@pytest.mark.negative
def test_unknown_price_cannot_claim_completeness_or_omit_reason() -> None:
    with pytest.raises(ValidationError):
        price(amount=None, currency=None, total_completeness="complete", unknown_reason=None)
    with pytest.raises(ValidationError):
        price(amount=None, currency=None, total_completeness="unknown", unknown_reason=None)
    with pytest.raises(ValidationError):
        price(amount=Decimal("100.00"), currency=None)


@pytest.mark.negative
def test_unknown_availability_requires_reason_and_mock_is_never_available() -> None:
    with pytest.raises(ValidationError):
        availability(unknown_reason=None)
    with pytest.raises(ValidationError):
        availability(availability_status="available", unknown_reason=None)
    assert availability().availability_status == "unknown"


@pytest.mark.negative
def test_constraint_evaluation_unknown_is_not_matched() -> None:
    evaluation = constraint_evaluation()
    assert evaluation.status == "unknown" and evaluation.unknown_reason is not None
    with pytest.raises(ValidationError):
        type(evaluation).model_validate({**evaluation.model_dump(), "unknown_reason": None})
    with pytest.raises(ValidationError):
        type(evaluation).model_validate({**evaluation.model_dump(), "status": "matched"})
