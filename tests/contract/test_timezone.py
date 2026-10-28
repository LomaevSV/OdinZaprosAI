"""T-CON-TZ-01: дата-время без часового пояса отклоняется (CONTRACT-01, INTERPRET-02, CONNECTION-02).

Календарные даты (``date``) и местное время заезда/выезда (``time``, пояс задаёт
``HotelLocation.timezone``) — не моменты времени и в эту проверку не входят.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import AwareDatetime, TypeAdapter, ValidationError

from odin.contracts import NORMATIVE_MODELS
from tests.introspection import all_contract_models, iter_annotation_types, iter_fields
from tests.samples import NAIVE, NORMATIVE_SAMPLES, NOW

pytestmark = [
    pytest.mark.contract,
    pytest.mark.negative,
    pytest.mark.req("CONTRACT-01", "INTERPRET-02", "T-CON-TZ-01"),
]

NAIVE_JSON_VALUES = ["2026-10-24T12:00:00", "2026-10-24T12:00", "2026-10-24 12:00:00", "2026-10-24"]


def _datetime_fields() -> list[tuple[str, str, object]]:
    found = []
    for model, name, field in iter_fields(all_contract_models()):
        if any(tp is AwareDatetime for tp in iter_annotation_types(field.annotation)):
            found.append((model.__name__, name, field.annotation))
    return found


DATETIME_FIELDS = _datetime_fields()


def _top_level_datetime_fields() -> list[tuple[str, str]]:
    return [
        (model_name, field_name)
        for model_name, field_name, _ in DATETIME_FIELDS
        if model_name in NORMATIVE_MODELS
    ]


def test_no_contract_field_uses_plain_datetime() -> None:
    offenders = [
        f"{model.__name__}.{name}"
        for model, name, field in iter_fields(all_contract_models())
        if any(tp is datetime for tp in iter_annotation_types(field.annotation))
    ]
    assert offenders == [], "поля datetime без требования часового пояса"
    assert len(DATETIME_FIELDS) >= 20  # проверка не пустая: моменты времени действительно найдены


@pytest.mark.parametrize(("model_name", "field_name", "annotation"), DATETIME_FIELDS)
def test_each_datetime_field_rejects_naive_and_accepts_aware(
    model_name: str, field_name: str, annotation: object
) -> None:
    adapter = TypeAdapter(annotation)
    with pytest.raises(ValidationError) as python_err:
        adapter.validate_python(NAIVE)
    assert {e["type"] for e in python_err.value.errors()} >= {"timezone_aware"}
    for raw in NAIVE_JSON_VALUES:
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(raw))
    assert adapter.validate_python(NOW) == NOW
    assert adapter.validate_json('"2026-10-24T15:00:00+03:00"') == NOW


@pytest.mark.parametrize(("model_name", "field_name"), _top_level_datetime_fields())
def test_full_contract_rejects_naive_datetime(model_name: str, field_name: str) -> None:
    model = NORMATIVE_MODELS[model_name]
    payload = json.loads(NORMATIVE_SAMPLES[model_name]().model_dump_json())

    payload[field_name] = "2026-10-24T12:00:00"
    with pytest.raises(ValidationError) as err:
        model.model_validate_json(json.dumps(payload))
    assert any(e["loc"] == (field_name,) and e["type"] == "timezone_aware" for e in err.value.errors())

    python_payload = NORMATIVE_SAMPLES[model_name]().model_dump()
    python_payload[field_name] = NAIVE
    with pytest.raises(ValidationError):
        model.model_validate(python_payload)


def test_offset_is_preserved_through_json() -> None:
    offer = NORMATIVE_SAMPLES["TravelOffer"]()
    payload = json.loads(offer.model_dump_json())
    assert payload["departure_at"] == "2026-11-05T22:00:00+03:00"
    restored = type(offer).model_validate_json(offer.model_dump_json())
    assert restored.departure_at is not None
    assert restored.departure_at.utcoffset() == offer.departure_at.utcoffset()


def test_assignment_of_naive_datetime_is_rejected() -> None:
    request = NORMATIVE_SAMPLES["ToolRequest"]()
    with pytest.raises(ValidationError):
        request.deadline = NAIVE
