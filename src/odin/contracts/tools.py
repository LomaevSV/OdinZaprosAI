"""Контракты инструментов и агентов (ТЗ §7: ToolRequest, ToolResult, AgentResult; план §2.23)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from odin.contracts.common import SourceReference, VersionedContract

ToolStatus = Literal["ok", "empty", "partial", "unavailable", "error", "timeout"]

LogicalToolName = Literal[
    "search_transport",
    "search_hotels",
    "get_offer_details",
    "get_transfer_options",
    "geocode",
    "get_climate",
    "get_weather",
    "get_reviews",
    "suggest_destinations",
]


class ToolRequest(VersionedContract):
    """Запрос логического инструмента. Выполняет только оркестратор через Tool Registry (ARCH-01)."""

    request_id: str = Field(min_length=1, max_length=64)
    call_id: str = Field(min_length=1, max_length=64)
    tool_name: LogicalToolName
    arguments: dict[str, Any]
    deadline: AwareDatetime
    policy_scope: Literal["read_only"]


class ToolResult(VersionedContract):
    """Результат выполнения инструмента. ``status`` — единственный нормативный список статусов
    выполнения (CONTRACT-01); не путать с доступностью и ConstraintEvaluation.status.

    Отмена по дедлайну: ``status=timeout`` + ``error_code=deadline_cancelled`` (DEC-30).
    """

    call_id: str
    tool_name: str
    provider: str
    status: ToolStatus
    data: Any
    sources: list[SourceReference] = Field(default_factory=list)
    retrieved_at: AwareDatetime | None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None
    latency_ms: int = Field(ge=0)
    missing: list[str] = Field(default_factory=list, description="Для partial — что именно отсутствует")

    @model_validator(mode="after")
    def _rules(self) -> ToolResult:
        if self.status == "partial" and not self.missing:
            raise ValueError("partial требует указания отсутствующих данных (CONTRACT-01)")
        if self.status in ("error", "timeout", "unavailable") and not self.error_code:
            raise ValueError("для error/timeout/unavailable обязателен error_code")
        if self.status in ("ok", "partial") and not self.sources:
            raise ValueError("успешный результат без источника недопустим (DATA-MODE-02)")
        return self


class AgentResult(VersionedContract):
    """Результат LLM-роли или доменного модуля (конверт доменов — DEC-30).

    ``requested_tools`` — только просьба; решение принимает политика оркестратора.
    """

    agent: str
    status: Literal["ok", "partial", "unavailable", "error"]
    structured_output: Any
    requested_tools: list[ToolRequest] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


StopReason = Literal[
    "sufficient_results",
    "sources_exhausted",
    "limits_exhausted",
    "deadline",
    "provider_unavailable",
    "user_stopped",
    "blocked_by_decision",
]


class UncheckedSource(VersionedContract):
    source: str
    reason: str


class SearchTermination(VersionedContract):
    """Причина и охват остановки поиска (SEARCH-STOP-01, план §2.23).

    ``sufficient_results`` выключен до решения DEC-22 и отклоняется валидатором.
    Остановка по лимиту или ошибке не доказывает отсутствия вариантов.
    """

    reason: StopReason
    checked_sources: list[str]
    unchecked_sources: list[UncheckedSource]
    candidates_seen: int = Field(ge=0)
    confirmed_matches: int = Field(ge=0)
    unknown_candidates: int = Field(ge=0)
    constraints_unresolved: list[str] = Field(default_factory=list)
    budget_used: dict[str, int] = Field(default_factory=dict)
    can_continue: bool

    @model_validator(mode="after")
    def _rules(self) -> SearchTermination:
        if self.reason == "sufficient_results":
            raise ValueError("sufficient_results выключен до решения DEC-22 (план §2.23)")
        return self
