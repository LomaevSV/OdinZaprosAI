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

ChangeGroup = Literal["dates", "destination", "origin", "party", "budget", "pets", "transport", "preferences"]

ALL_ROLES: tuple[ComponentRole, ...] = ("outbound", "return", "hotel", "transfer_in", "transfer_out")

GROUP_TITLES: dict[str, str] = {
    "dates": "даты",
    "destination": "направление",
    "origin": "город отправления",
    "party": "состав группы",
    "budget": "бюджет",
    "pets": "животное",
    "transport": "вид транспорта",
    "preferences": "предпочтения",
}


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
        invalidates=("outbound", "return", "transfer_in", "transfer_out"),
        reason="город отправления: транспорт, точки прибытия и трансфер, стоимость (ТЗ REPLAN-01)",
    ),
    ReplanRule(
        group="party",
        fields=("adults", "children_ages"),
        invalidates=("outbound", "return", "hotel", "transfer_in", "transfer_out"),
        reason="состав группы: размещение, билеты, цены, трансфер, корзина (ТЗ REPLAN-01)",
    ),
    ReplanRule(
        group="pets",
        fields=("pets",),
        invalidates=("outbound", "return", "hotel", "transfer_in", "transfer_out"),
        reason="животное: правила отеля и транспорта, трансфер, совместимость, корзина (ТЗ REPLAN-01)",
    ),
    ReplanRule(
        group="budget",
        fields=("budget_amount", "budget_currency"),
        invalidates=(),
        reason="бюджет меняет только оценку условий, а не сами офферы",
    ),
    ReplanRule(
        group="transport",
        fields=("transport_modes",),
        invalidates=("outbound", "return", "transfer_in", "transfer_out"),
        reason="вид транспорта — параметр поиска: транспорт, точки прибытия и трансфер перепроверяются",
    ),
    ReplanRule(
        group="preferences",
        fields=("breakfast", "stars_min", "free_cancellation", "room_type", "wishes_tags"),
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


# Смена обязательности меняет только оценку условий, не офферы.
_REQUIRED_FLAG_GROUP: dict[str, ChangeGroup] = {
    "budget_required": "budget",
    "pets_required": "pets",
    "transport_modes_required": "transport",
    "breakfast_required": "preferences",
    "stars_required": "preferences",
    "free_cancellation_required": "preferences",
}


def split_change_set(change_set: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """Делит изменения на поля таблицы REPLAN-01 и флаги обязательности."""
    table = {key: value for key, value in change_set.items() if key not in _REQUIRED_FLAG_GROUP}
    flags = {key: value for key, value in change_set.items() if key in _REQUIRED_FLAG_GROUP}
    return table, flags


def changed_groups(change_set: Mapping[str, object]) -> list[str]:
    """Названия изменённых групп для пользователя (без повторов, в порядке полей)."""
    titles: list[str] = []
    for key in change_set:
        group = _REQUIRED_FLAG_GROUP.get(key) or classify_change(key).group
        title = GROUP_TITLES[group]
        if title not in titles:
            titles.append(title)
    return titles
