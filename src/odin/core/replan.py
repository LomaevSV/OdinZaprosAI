"""Таблица пересборки REPLAN-01 как данные (план §2.4 п. 3, REPLAN-01).

Таблица воспроизводит зависимость «изменение → что инвалидируется» и применяется только после
явного ``change_set``. Пока корзина не подтверждена, пересборка безопасна; после подтверждения
ревизия не переиспользуется (REPLAN-02).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

ComponentRole = Literal["outbound", "return", "hotel", "transfer_in", "transfer_out"]

ChangeGroup = Literal["dates", "destination", "origin", "party", "budget", "pets", "preferences"]


@dataclass(frozen=True, slots=True)
class ReplanRule:
    group: ChangeGroup
    fields: tuple[str, ...]
    invalidates: tuple[ComponentRole, ...]
    reason: str


REPLAN_TABLE: tuple[ReplanRule, ...] = (
    ReplanRule(
        group="dates",
        fields=("date_from", "date_to", "return_trip"),
        invalidates=("outbound", "return", "hotel", "transfer_in", "transfer_out"),
        reason="даты влияют на все компоненты и их цены",
    ),
    ReplanRule(
        group="destination",
        fields=("destination",),
        invalidates=("outbound", "return", "hotel", "transfer_in", "transfer_out"),
        reason="смена направления меняет весь маршрут",
    ),
    ReplanRule(
        group="origin",
        fields=("origin",),
        invalidates=("outbound", "return"),
        reason="меняется точка отправления",
    ),
    ReplanRule(
        group="party",
        fields=("adults", "children_ages"),
        invalidates=("outbound", "return", "hotel"),
        reason="состав группы влияет на вместимость и тарифы",
    ),
    ReplanRule(
        group="pets",
        fields=("pets",),
        invalidates=("outbound", "return", "hotel"),
        reason="правила животного перепроверяются у транспорта и отеля",
    ),
    ReplanRule(
        group="budget",
        fields=("budget_amount", "budget_currency"),
        invalidates=(),
        reason="бюджет меняет только оценку условий, а не сами офферы",
    ),
    ReplanRule(
        group="preferences",
        fields=("breakfast", "stars_min", "free_cancellation", "wishes_tags", "transport_modes"),
        invalidates=(),
        reason="предпочтения оцениваются на уже найденных офферах",
    ),
)

_RULES_BY_FIELD: dict[str, ReplanRule] = {field: rule for rule in REPLAN_TABLE for field in rule.fields}


def classify_change(field: str) -> ReplanRule:
    return _RULES_BY_FIELD[field]


def invalidated_roles(change_set: Mapping[str, object]) -> tuple[list[ComponentRole], list[str]]:
    """Возвращает инвалидируемые роли и пояснения. Неизвестное поле — ошибка, а не догадка."""
    unknown = [field for field in change_set if field not in _RULES_BY_FIELD]
    if unknown:
        raise KeyError(f"поля вне таблицы REPLAN-01: {', '.join(sorted(unknown))}")
    roles: list[ComponentRole] = []
    reasons: list[str] = []
    for field in change_set:
        rule = _RULES_BY_FIELD[field]
        reasons.append(f"{rule.group}: {rule.reason}")
        for role in rule.invalidates:
            if role not in roles:
                roles.append(role)
    return roles, reasons


def reusable_roles(
    all_roles: Iterable[ComponentRole], invalidated: Iterable[ComponentRole]
) -> list[ComponentRole]:
    dropped = set(invalidated)
    return [role for role in all_roles if role not in dropped]
