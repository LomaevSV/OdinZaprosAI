"""Обход аннотаций моделей контрактов для структурных проверок."""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Iterator
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

import odin.contracts.api as api_module
from odin.contracts import EXTRA_MODELS, NORMATIVE_MODELS

NONE_TYPE = type(None)


def iter_annotation_types(annotation: Any) -> Iterator[Any]:
    """Все типы внутри аннотации: Union, Optional, list, dict, Annotated."""
    yield annotation
    for arg in get_args(annotation):
        yield from iter_annotation_types(arg)


def is_model(tp: Any) -> bool:
    return inspect.isclass(tp) and issubclass(tp, BaseModel)


def model_closure(roots: Iterable[type[BaseModel]]) -> list[type[BaseModel]]:
    """Корневые модели и все вложенные модели, достижимые через поля."""
    seen: dict[type[BaseModel], None] = {}
    stack = list(roots)
    while stack:
        model = stack.pop(0)
        if model in seen:
            continue
        seen[model] = None
        for field in model.model_fields.values():
            stack.extend(tp for tp in iter_annotation_types(field.annotation) if is_model(tp))
    return list(seen)


def api_models() -> list[type[BaseModel]]:
    return [
        obj for obj in vars(api_module).values() if is_model(obj) and obj.__module__ == api_module.__name__
    ]


def normative_closure() -> list[type[BaseModel]]:
    return model_closure(NORMATIVE_MODELS.values())


def all_contract_models() -> list[type[BaseModel]]:
    return model_closure([*NORMATIVE_MODELS.values(), *EXTRA_MODELS.values(), *api_models()])


def iter_fields(models: Iterable[type[BaseModel]]) -> Iterator[tuple[type[BaseModel], str, FieldInfo]]:
    for model in models:
        for name, field in model.model_fields.items():
            yield model, name, field


def allows_none(field: FieldInfo) -> bool:
    return field.annotation is None or NONE_TYPE in get_args(field.annotation)


def single_literal_value(field: FieldInfo) -> tuple[bool, Any]:
    """(True, значение), если тип поля — Literal ровно с одним значением (константа контракта)."""
    if get_origin(field.annotation) is Literal and len(get_args(field.annotation)) == 1:
        return True, get_args(field.annotation)[0]
    return False, None
