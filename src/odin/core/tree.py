"""Дерево условий, покрытие и таблица компромиссов (INTERPRET-01, CONSTRAINT-01, COVERAGE-01, план §2.21).

Группы ссылаются на условия и на другие группы по ID; дерево вычисляется снизу вверх без рекурсивного
доверия к входу: циклы и висячие ссылки — `unknown` с причиной, а не исключение и не `matched`.
Неактивная ветвь FALLBACK получает `not_applicable` с причиной (CONSTRAINT-01).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from odin.contracts import Constraint, ConstraintEvaluation, ConstraintGroup
from odin.contracts.common import EvaluationStatus
from odin.core.constraints import aggregate_statuses, evaluate_group


@dataclass(frozen=True, slots=True)
class TreeResult:
    """Итог дерева: статус каждой группы с пояснением и статусы ветвей FALLBACK."""

    group_status: dict[str, EvaluationStatus]
    notes: dict[str, str]
    inactive_branches: dict[str, str] = field(default_factory=dict)

    def root_statuses(self, roots: Iterable[str]) -> dict[str, EvaluationStatus]:
        return {root: self.group_status[root] for root in roots if root in self.group_status}


def evaluate_tree(
    groups: Sequence[ConstraintGroup], evaluations: Iterable[ConstraintEvaluation]
) -> TreeResult:
    statuses: dict[str, EvaluationStatus] = dict(aggregate_statuses(evaluations))
    by_id = {group.group_id: group for group in groups}
    notes: dict[str, str] = {}
    inactive: dict[str, str] = {}
    visiting: set[str] = set()
    cycle_members: set[str] = set()
    stack: list[str] = []

    def visit(group_id: str) -> EvaluationStatus:
        if group_id in statuses and group_id in notes:
            return statuses[group_id]
        if group_id in visiting:
            cycle_members.update(stack[stack.index(group_id) :])
            notes[group_id] = "цикл в дереве условий: оценка невозможна"
            statuses[group_id] = "unknown"
            return "unknown"
        visiting.add(group_id)
        stack.append(group_id)
        group = by_id[group_id]
        for child in group.children:
            if child in by_id:
                visit(child)
        status, note = evaluate_group(group, statuses)
        if group.operator == "FALLBACK":
            primary, backup = group.children
            activated = group.activation is not None and group.activation.status == "activated"
            inactive[backup if not activated else primary] = (
                "резервная ветвь не активирована: условие активации не доказано"
                if not activated
                else "основная ветвь отклонена: сработал FALLBACK"
            )
        visiting.discard(group_id)
        stack.pop()
        if group_id in cycle_members:
            status, note = "unknown", "цикл в дереве условий: оценка невозможна"
        statuses[group_id] = status
        notes[group_id] = note
        return status

    for group_id in by_id:
        visit(group_id)
    return TreeResult(
        group_status={gid: statuses[gid] for gid in by_id}, notes=notes, inactive_branches=inactive
    )


def roots_of(groups: Sequence[ConstraintGroup]) -> list[str]:
    referenced = {child for group in groups for child in group.children}
    return [group.group_id for group in groups if group.group_id not in referenced]


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """COVERAGE-01: структурная проверка — у каждого применимого условия есть оценка."""

    missing: tuple[str, ...]
    violated_required: tuple[str, ...]
    unknown_required: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


def coverage(
    constraints: Sequence[Constraint], evaluations: Iterable[ConstraintEvaluation]
) -> CoverageReport:
    statuses = aggregate_statuses(evaluations)
    missing = tuple(c.constraint_id for c in constraints if c.constraint_id not in statuses)
    required = [c.constraint_id for c in constraints if c.requiredness == "required"]
    return CoverageReport(
        missing=missing,
        violated_required=tuple(cid for cid in required if statuses.get(cid) == "violated"),
        unknown_required=tuple(cid for cid in required if statuses.get(cid) == "unknown"),
    )


@dataclass(frozen=True, slots=True)
class Tradeoff:
    candidate_id: str
    better: tuple[str, ...]
    worse: tuple[str, ...]
    unknown: tuple[str, ...]

    def text(self) -> str:
        parts = []
        if self.better:
            parts.append("подтверждено: " + ", ".join(self.better))
        if self.worse:
            parts.append("нарушено: " + ", ".join(self.worse))
        if self.unknown:
            parts.append("не подтверждено: " + ", ".join(self.unknown))
        return f"{self.candidate_id}: " + ("; ".join(parts) if parts else "оцениваемых условий нет")


def tradeoffs(
    candidates: dict[str, Iterable[ConstraintEvaluation]], constraint_ids: Sequence[str]
) -> list[Tradeoff]:
    """Таблица компромиссов, которую считает код: только статусы условий, без весов и без цены."""
    result: list[Tradeoff] = []
    for candidate_id in sorted(candidates):
        statuses = aggregate_statuses(candidates[candidate_id])
        result.append(
            Tradeoff(
                candidate_id=candidate_id,
                better=tuple(cid for cid in constraint_ids if statuses.get(cid) == "matched"),
                worse=tuple(cid for cid in constraint_ids if statuses.get(cid) == "violated"),
                unknown=tuple(cid for cid in constraint_ids if statuses.get(cid) == "unknown"),
            )
        )
    return result
