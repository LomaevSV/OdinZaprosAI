"""Оценщики условий, дерево AND/OR/FALLBACK и классы ранжирования (CONSTRAINT-01, RANK-01, FALLBACK-01).

Матрица §2.21 плана: ``matched`` только при доказанном выполнении, ``unknown`` при нехватке данных,
``not_applicable`` только по явной схеме применимости. ``unknown`` никогда не равен ``matched``.
Ранжирование — строгие классы RANK-01 без весов: цена и предпочтения не поднимают класс с нарушением.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from odin.contracts import Constraint, ConstraintEvaluation, ConstraintGroup, FallbackActivation
from odin.contracts.common import Deviation, EvaluationStatus
from odin.core.trip import evaluate_budget

# Область условия → роли компонентов, к которым оно применимо (явная схема применимости).
SCOPE_ROLES: dict[str, frozenset[str]] = {
    "trip": frozenset({"trip"}),
    "hotel": frozenset({"hotel"}),
    "transport": frozenset({"outbound", "return"}),
    "transfer": frozenset({"transfer_in", "transfer_out"}),
}


@dataclass(frozen=True, slots=True)
class OfferFacts:
    """Факты кандидата для проверки условий. Отсутствующее значение — ``None``, а не догадка."""

    component_id: str
    role: str
    total_amount: Decimal | None = None
    currency: str | None = None
    price_completeness: str = "unknown"
    transport_mode: str | None = None
    pets_allowed: bool | None = None
    breakfast_included: bool | None = None
    star_rating: int | None = None
    free_cancellation: bool | None = None
    evidence: tuple[str, ...] = ()


def _result(
    constraint: Constraint,
    facts: OfferFacts,
    status: EvaluationStatus,
    reason: str,
    *,
    unknown_reason: str = "not_provided_by_source",
    deviation: Deviation | None = None,
) -> ConstraintEvaluation:
    return ConstraintEvaluation(
        constraint_id=constraint.constraint_id,
        component_id=facts.component_id,
        plan_id=None,
        status=status,
        unknown_reason=unknown_reason if status == "unknown" else None,  # type: ignore[arg-type]
        evidence_refs=list(facts.evidence) if status != "not_applicable" else [],
        reason=reason,
        deviation=deviation,
    )


def _bool_check(
    constraint: Constraint, facts: OfferFacts, actual: bool | None, subject: str
) -> ConstraintEvaluation:
    if actual is None:
        return _result(constraint, facts, "unknown", f"{subject}: источник не подтвердил")
    if actual == bool(constraint.value):
        return _result(constraint, facts, "matched", f"{subject}: совпадает с условием")
    return _result(constraint, facts, "violated", f"{subject}: не совпадает с условием")


def _check_budget(constraint: Constraint, facts: OfferFacts) -> ConstraintEvaluation:
    try:
        limit = Decimal(str(constraint.value))
    except (InvalidOperation, ValueError):
        return _result(
            constraint, facts, "unknown", "значение бюджета не распознано", unknown_reason="data_missing"
        )
    currency = facts.currency or "RUB"
    status, deviation, reason = evaluate_budget(
        limit, currency, facts.total_amount, facts.currency, facts.price_completeness
    )
    unknown_reason = "price_incomplete" if facts.total_amount is not None else "not_provided_by_source"
    return _result(constraint, facts, status, reason, unknown_reason=unknown_reason, deviation=deviation)


_MODE_NAMES = {"rail": "поезд", "flight": "самолёт"}


def _check_transport_mode(constraint: Constraint, facts: OfferFacts) -> ConstraintEvaluation:
    wanted = [
        str(item) for item in (constraint.value if isinstance(constraint.value, list) else [constraint.value])
    ]
    if facts.transport_mode is None:
        return _result(constraint, facts, "unknown", "вид транспорта не указан источником")
    inside = facts.transport_mode in wanted
    name = _MODE_NAMES.get(facts.transport_mode, facts.transport_mode)
    if (constraint.operator == "in") == inside:
        return _result(constraint, facts, "matched", f"вид транспорта «{name}» допустим")
    return _result(constraint, facts, "violated", f"вид транспорта «{name}» не допустим условием")


def _check_pets(constraint: Constraint, facts: OfferFacts) -> ConstraintEvaluation:
    return _bool_check(constraint, facts, facts.pets_allowed, "размещение с животным")


def _check_breakfast(constraint: Constraint, facts: OfferFacts) -> ConstraintEvaluation:
    return _bool_check(constraint, facts, facts.breakfast_included, "завтрак")


def _check_free_cancellation(constraint: Constraint, facts: OfferFacts) -> ConstraintEvaluation:
    return _bool_check(constraint, facts, facts.free_cancellation, "бесплатная отмена")


def _check_stars(constraint: Constraint, facts: OfferFacts) -> ConstraintEvaluation:
    if facts.star_rating is None:
        return _result(constraint, facts, "unknown", "категория отеля не указана источником")
    if facts.star_rating >= int(constraint.value):
        return _result(constraint, facts, "matched", f"категория {facts.star_rating}★ не ниже требуемой")
    return _result(constraint, facts, "violated", f"категория {facts.star_rating}★ ниже требуемой")


_EVALUATORS: dict[str, Callable[[Constraint, OfferFacts], ConstraintEvaluation]] = {
    "budget_total": _check_budget,
    "transport_mode": _check_transport_mode,
    "pets": _check_pets,
    "hotel_breakfast": _check_breakfast,
    "hotel_stars_min": _check_stars,
    "hotel_free_cancellation": _check_free_cancellation,
}

# Область условия → роли компонентов. Бюджет поездки проверяется у каждого компонента (известная часть),
# условие о животном — у отеля и транспорта, режим транспорта — только у транспорта.
_TRIP_FIELD_ROLES: dict[str, frozenset[str]] = {
    "budget_total": frozenset({"trip", "hotel", "outbound", "return", "transfer_in", "transfer_out"}),
    "pets": frozenset({"hotel", "outbound", "return"}),
    "transport_mode": frozenset({"outbound", "return"}),
}


def applicable(constraint: Constraint, role: str) -> bool:
    """Явная схема применимости: условие к компоненту другой роли — ``not_applicable``."""
    roles = _TRIP_FIELD_ROLES.get(constraint.field) or SCOPE_ROLES[constraint.scope]
    return role in roles


def evaluate_constraint(constraint: Constraint, facts: OfferFacts) -> ConstraintEvaluation:
    if not applicable(constraint, facts.role):
        return _result(constraint, facts, "not_applicable", f"условие не относится к компоненту {facts.role}")
    return _EVALUATORS[constraint.field](constraint, facts)


def evaluate_constraints(constraints: Iterable[Constraint], facts: OfferFacts) -> list[ConstraintEvaluation]:
    return [evaluate_constraint(constraint, facts) for constraint in constraints]


def combine_and(statuses: Sequence[EvaluationStatus]) -> EvaluationStatus:
    applicable_statuses = [status for status in statuses if status != "not_applicable"]
    if not applicable_statuses:
        return "not_applicable"
    if "violated" in applicable_statuses:
        return "violated"
    if all(status == "matched" for status in applicable_statuses):
        return "matched"
    return "unknown"


def combine_or(statuses: Sequence[EvaluationStatus]) -> EvaluationStatus:
    applicable_statuses = [status for status in statuses if status != "not_applicable"]
    if not applicable_statuses:
        return "not_applicable"
    if "matched" in applicable_statuses:
        return "matched"
    if all(status == "violated" for status in applicable_statuses):
        return "violated"
    return "unknown"


def evaluate_group(
    group: ConstraintGroup, statuses: dict[str, EvaluationStatus]
) -> tuple[EvaluationStatus, str]:
    """Оценка группы по матрице §2.21. Возвращает статус и пояснение (выбранная ветвь, активация)."""
    missing = [child for child in group.children if child not in statuses]
    if group.operator == "unparsed":
        return "unknown", f"нераспознанный фрагмент сохранён, нужно уточнение: {group.raw_text}"
    if missing:
        return "unknown", f"нет оценки для: {', '.join(missing)}"
    values = [statuses[child] for child in group.children]
    if group.operator == "AND":
        return combine_and(values), "AND: violated при любом нарушении, matched только если все matched"
    if group.operator == "OR":
        for child, status in zip(group.children, values, strict=True):
            if status == "matched":
                return "matched", f"OR: подтверждена ветвь {child}"
        return combine_or(values), "OR: ни одна ветвь не подтверждена"

    # FALLBACK: резервная ветвь только после доказанной активации (FALLBACK-01).
    primary, backup = group.children
    activation = group.activation
    if activation is None or activation.status != "activated":
        note = "условие активации не доказано" if activation else "нет условия активации"
        return statuses[primary], f"FALLBACK не активирован ({note}): основная ветвь {primary} не подменяется"
    return statuses[
        backup
    ], f"FALLBACK активирован: основная ветвь {primary} отклонена, показана резервная {backup}"


def fallback_activation(
    *,
    group_id: str,
    trigger: str,
    primary_status: EvaluationStatus,
    search_completeness: str,
    evidence: Sequence[str],
) -> FallbackActivation:
    """``activated`` — только при доказанном нарушении основной ветви и завершённом поиске."""
    if primary_status == "matched":
        status = "not_activated"
    elif primary_status == "violated" and search_completeness == "complete" and evidence:
        status = "activated"
    else:
        status = "undetermined"
    return FallbackActivation(
        group_id=group_id,
        trigger=trigger,
        evidence=list(evidence),
        search_completeness=search_completeness,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------- ранжирование RANK-01


@dataclass(frozen=True, slots=True)
class RankingKey:
    """Ключ строгого порядка RANK-01/DEC-26: класс → подтверждённые предпочтения → полнота → ID."""

    rank_class: int
    confirmed_preferences: int
    unknown_count: int
    canonical_id: str

    def as_tuple(self) -> tuple[int, int, int, str]:
        return (self.rank_class, -self.confirmed_preferences, self.unknown_count, self.canonical_id)


def rank_class(statuses_by_constraint: dict[str, EvaluationStatus], required_ids: Iterable[str]) -> int:
    """1 — все обязательные ``matched``; 2 — обязательные ``unknown`` без нарушений; 3 — нарушение."""
    required = [statuses_by_constraint.get(cid, "unknown") for cid in required_ids]
    applicable_required = [status for status in required if status != "not_applicable"]
    if "violated" in applicable_required:
        return 3
    if all(status == "matched" for status in applicable_required):
        return 1
    return 2


def rank_key(
    canonical_id: str, statuses_by_constraint: dict[str, EvaluationStatus], required_ids: Iterable[str]
) -> RankingKey:
    required = set(required_ids)
    preferred_matched = sum(
        1 for cid, status in statuses_by_constraint.items() if cid not in required and status == "matched"
    )
    unknown = sum(1 for status in statuses_by_constraint.values() if status == "unknown")
    return RankingKey(
        rank_class=rank_class(statuses_by_constraint, required),
        confirmed_preferences=preferred_matched,
        unknown_count=unknown,
        canonical_id=canonical_id,
    )


def sort_by_rank(keys: Iterable[RankingKey]) -> list[RankingKey]:
    """Стабильный детерминированный порядок без весов и без цены."""
    return sorted(keys, key=lambda key: key.as_tuple())


def aggregate_statuses(evaluations: Iterable[ConstraintEvaluation]) -> dict[str, EvaluationStatus]:
    """Сводит оценки одного условия по нескольким компонентам: AND по применимым компонентам."""
    grouped: dict[str, list[EvaluationStatus]] = {}
    for evaluation in evaluations:
        grouped.setdefault(evaluation.constraint_id, []).append(evaluation.status)
    return {cid: combine_and(statuses) for cid, statuses in grouped.items()}
