"""Политика уточнений UC-02 и допущений UC-03 (план §4 S4, DEC-25).

Правила:
- три класса: ``blocking_now`` (без этого нельзя безопасно сделать следующий шаг),
  ``assumable_only_if_authorized`` (допущение возможно только с разрешения — DEC-25 открыт,
  разрешения нет, поэтому вопрос задаётся), ``optional`` (не спрашивается);
- город отправления, направление, конкретные даты и бюджет не предполагаются никогда (UC-03);
- уже полученная информация не запрашивается повторно: ответ на уточнение дополняет прежние данные;
- ``unknown`` (не сообщено) и ``not_applicable`` (вопрос не относится к ситуации) различаются.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from odin.contracts.api import StructuredForm

BlockingClass = Literal["blocking_now", "assumable_only_if_authorized", "optional"]
FieldStatus = Literal["known", "unknown", "not_applicable", "invalid"]

# DEC-25 (открыт): допущения не разрешены. Значение меняется только решением руководителя.
ASSUMPTIONS_AUTHORIZED = False

# Поля, которые нельзя предполагать ни при каких условиях (UC-03).
NEVER_ASSUMED = frozenset({"origin", "destination", "date_from", "date_to", "budget_amount"})


@dataclass(frozen=True, slots=True)
class ClarificationDecision:
    field: str
    status: FieldStatus
    blocking_class: BlockingClass
    ask: bool
    question: str | None
    reason: str


def _decision(
    field: str,
    status: FieldStatus,
    blocking_class: BlockingClass,
    reason: str,
    question: str | None = None,
) -> ClarificationDecision:
    ask = question is not None and status in ("unknown", "invalid")
    if blocking_class == "optional":
        ask = False
    if blocking_class == "assumable_only_if_authorized" and ASSUMPTIONS_AUTHORIZED:
        ask = False
    return ClarificationDecision(field, status, blocking_class, ask, question if ask else None, reason)


def decide(form: StructuredForm) -> list[ClarificationDecision]:
    """Решение по каждому значимому полю. Вопросы — только у ``ask=True``."""
    inspiration = form.destination is None and bool(form.wishes_tags)
    decisions: list[ClarificationDecision] = []

    if form.origin:
        decisions.append(_decision("origin", "known", "blocking_now", "указан пользователем"))
    else:
        decisions.append(
            _decision(
                "origin",
                "unknown",
                "blocking_now",
                "город отправления не предполагается (UC-03)",
                "Откуда едем? Город отправления обязателен.",
            )
        )

    if form.destination:
        decisions.append(_decision("destination", "known", "blocking_now", "указано пользователем"))
    elif inspiration:
        decisions.append(
            _decision(
                "destination",
                "not_applicable",
                "blocking_now",
                "направление подбирается по пожеланиям (UC-04), а не запрашивается",
            )
        )
    else:
        decisions.append(
            _decision(
                "destination",
                "unknown",
                "blocking_now",
                "направление не предполагается (UC-03)",
                "Куда едем? Назовите направление или пожелания.",
            )
        )

    # Даты блокируют поиск с ценой; для вдохновения без направления они не обязательны.
    dates_class: BlockingClass = "optional" if inspiration else "blocking_now"
    if form.date_from is not None and form.date_to is not None:
        if form.date_to <= form.date_from:
            decisions.append(
                _decision(
                    "date_to",
                    "invalid",
                    "blocking_now",
                    "дата возвращения не позже даты отъезда",
                    "Дата возвращения должна быть позже даты отъезда.",
                )
            )
        else:
            decisions.append(_decision("date_from", "known", dates_class, "даты указаны пользователем"))
    elif form.date_from is not None or form.date_to is not None:
        decisions.append(
            _decision(
                "date_from",
                "unknown",
                "blocking_now",
                "указана только одна дата; вторая не предполагается (UC-03)",
                "Нужны обе даты поездки: отъезд и возвращение.",
            )
        )
    else:
        decisions.append(
            _decision(
                "date_from",
                "unknown",
                dates_class,
                "конкретные даты не предполагаются (UC-03)"
                + ("; для вдохновения стоимость требует дат" if inspiration else ""),
                "На какие даты планируется поездка (отъезд и возвращение)?",
            )
        )

    if form.adults is not None:
        decisions.append(
            _decision("adults", "known", "assumable_only_if_authorized", "указано пользователем")
        )
    else:
        decisions.append(
            _decision(
                "adults",
                "unknown",
                "assumable_only_if_authorized",
                "допущение о составе группы не разрешено (DEC-25): вопрос",
                "Сколько взрослых путешествует?",
            )
        )

    budget_status: FieldStatus = "known" if form.budget_amount is not None else "unknown"
    decisions.append(
        _decision("budget_amount", budget_status, "optional", "бюджет не предполагается и не обязателен")
    )
    pets_status: FieldStatus = "known" if form.pets else "unknown"
    decisions.append(_decision("pets", pets_status, "optional", "сообщается пользователем при необходимости"))
    return decisions


def questions(decisions: list[ClarificationDecision]) -> list[ClarificationDecision]:
    return [decision for decision in decisions if decision.ask]


_FLAG_SUFFIX = "_required"


def merge_answer(previous: StructuredForm, answer: StructuredForm) -> StructuredForm:
    """Ответ на уточнение дополняет прежние данные: полученное не теряется и не спрашивается снова.

    Непустое значение ответа заменяет прежнее; пустое (``None``, ``[]``) прежнее не стирает;
    флаг обязательности не снимается молча (обязательное условие не теряется).
    """
    old = previous.model_dump()
    new = answer.model_dump()
    merged: dict[str, Any] = dict(old)
    for key, value in new.items():
        if key.endswith(_FLAG_SUFFIX):
            merged[key] = bool(old.get(key)) or bool(value)
        elif value is None or value == []:
            continue
        else:
            merged[key] = value
    return StructuredForm.model_validate(merged)


def detect_changes(old: StructuredForm, new: StructuredForm) -> dict[str, Any]:
    """Детектор изменений: поля, отличающиеся между прежним и новым запросом (REPLAN-01)."""
    before = old.model_dump()
    after = new.model_dump()
    return {key: after[key] for key in after if after[key] != before.get(key)}
