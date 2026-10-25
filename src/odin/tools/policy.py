"""Политика инструментов: deny-by-default, allowlist «сервер + инструмент» (SECURITY-01, ARCH-03).

Запрет проверяется в коде до любого обращения к сети. Live-вызов возможен только к провайдеру с
``approval_status=approved`` в реестре (DEC-09) и только к инструментам его allowlist.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

LOGICAL_TOOLS = (
    "search_transport",
    "search_hotels",
    "get_offer_details",
    "get_transfer_options",
    "geocode",
    "get_climate",
    "get_weather",
    "get_reviews",
    "suggest_destinations",
)

# Логический инструмент → провайдер в режимах live/mixed/replay (план §2.5). Вдохновение — каталог.
LIVE_ROUTES: dict[str, str] = {
    "search_transport": "tutu_mcp",
    "search_hotels": "tutu_mcp",
    "get_offer_details": "tutu_mcp",
    "geocode": "open_meteo",
    "get_transfer_options": "osrm_demo",
    "suggest_destinations": "odin_mock",
}

# Логические инструменты, разрешённые в S1 (Mock-путь). Остальные — объявлены, но не реализованы.
MOCK_SUPPORTED_TOOLS = (
    "search_hotels",
    "search_transport",
    "get_transfer_options",
    "suggest_destinations",
    "get_offer_details",
)

# Инструменты Tutu, которые нельзя вызывать никогда или до отдельного решения (план §2.12).
HARD_DENY_TOOLS = frozenset({"register_checkout_passengers"})
PENDING_DECISION_TOOLS = {
    "create_checkout_link": "DEC-10",
    "get_rail_seatmap": "DEC-10",
    "get_avia_instructions": "DEC-10",
    "get_rail_instructions": "DEC-10",
    "get_bus_instructions": "DEC-10",
    "get_etrain_instructions": "DEC-10",
    "get_hotels_instructions": "DEC-10",
    "get_multitransport_instructions": "DEC-10",
    "search_bus": "P1",
    "search_multitransport": "P1",
    "search_etrain": "P1, DEC-18",
}


class ToolDeniedError(PermissionError):
    """Вызов запрещён политикой: сеть не затрагивается, причина возвращается явно."""

    def __init__(self, tool: str, provider: str, reason: str, decision: str | None = None) -> None:
        self.tool = tool
        self.provider = provider
        self.reason = reason
        self.decision = decision
        self.error_code = "tool_denied"
        super().__init__(f"{provider}.{tool}: {reason}" + (f" ({decision})" if decision else ""))


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    provider_id: str
    approval_status: Literal["proposed", "approved", "rejected", "blocked"]
    decision_ref: str | None = None
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    fetch_resource_uris: frozenset[str] = field(default_factory=frozenset)
    hard_deny: frozenset[str] = field(default_factory=frozenset)
    deny_pending: Mapping[str, str] = field(default_factory=dict)

    @property
    def live_allowed(self) -> bool:
        return self.approval_status == "approved"


MOCK_PROVIDER = "odin_mock"


def load_provider_policies(path: Path | None = None) -> dict[str, ProviderPolicy]:
    """Читает `config/provider_registry.yaml`. Утверждённых провайдеров в S1 нет."""
    registry_path = path or Path("config") / "provider_registry.yaml"
    if not registry_path.is_file():
        return {}
    data: dict[str, Any] = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    policies: dict[str, ProviderPolicy] = {}
    for card in data.get("providers", []):
        mcp_policy = card.get("mcp_policy") or card.get("tool_policy") or {}
        policies[card["id"]] = ProviderPolicy(
            provider_id=card["id"],
            approval_status=card.get("approval_status", "proposed"),
            decision_ref=card.get("decision_ref"),
            allowed_tools=frozenset(mcp_policy.get("allow_when_approved") or ()),
            fetch_resource_uris=frozenset(mcp_policy.get("fetch_resource_uris") or ()),
            hard_deny=frozenset(mcp_policy.get("hard_deny") or ()) | HARD_DENY_TOOLS,
            deny_pending=dict(mcp_policy.get("deny_pending_decision") or {}),
        )
    return policies


class ToolPolicy:
    """Единая точка проверки: логический инструмент → разрешённый провайдер в текущем режиме."""

    def __init__(
        self,
        *,
        data_mode: str,
        policies: Mapping[str, ProviderPolicy] | None = None,
    ) -> None:
        self.data_mode = data_mode
        self.policies = dict(policies or {})

    def check_logical(self, tool: str) -> str:
        """Проверяет логический инструмент и возвращает провайдера, который его обслуживает."""
        if tool not in LOGICAL_TOOLS:
            raise ToolDeniedError(tool, "registry", "неизвестный логический инструмент (deny-by-default)")
        if self.data_mode == "mock":
            if tool not in MOCK_SUPPORTED_TOOLS:
                raise ToolDeniedError(tool, MOCK_PROVIDER, "инструмент не реализован в Mock-пути S1")
            return MOCK_PROVIDER
        provider = LIVE_ROUTES.get(tool)
        if provider is None:
            raise ToolDeniedError(tool, "registry", "инструмент не реализован для Live (deny-by-default)")
        if provider == MOCK_PROVIDER:
            return MOCK_PROVIDER  # внутренний редакционный каталог (вдохновение), всегда с маркировкой
        policy = self.policies.get(provider)
        if policy is not None and policy.live_allowed:
            return provider
        if self.data_mode == "mixed" and tool in MOCK_SUPPORTED_TOOLS:
            return MOCK_PROVIDER  # DEC-23: неутверждённый провайдер в mixed → явный Mock по компоненту
        raise ToolDeniedError(
            tool,
            provider,
            f"провайдер не утверждён (approval_status={policy.approval_status if policy else 'нет карточки'})",
            policy.decision_ref if policy else "DEC-09",
        )

    def check_provider_tool(self, provider: str, tool: str) -> None:
        """Проверка конкретного вызова провайдера (MCP/REST) — до сети."""
        if tool in HARD_DENY_TOOLS:
            raise ToolDeniedError(
                tool, provider, "инструмент запрещён всегда (создаёт реальный заказ)", "UC-08"
            )
        policy = self.policies.get(provider)
        if policy is None:
            raise ToolDeniedError(tool, provider, "провайдер отсутствует в реестре (deny-by-default)")
        if tool in policy.hard_deny:
            raise ToolDeniedError(tool, provider, "инструмент в жёстком запрете карточки провайдера")
        if tool in policy.deny_pending:
            raise ToolDeniedError(tool, provider, "инструмент ждёт решения", str(policy.deny_pending[tool]))
        if not policy.live_allowed:
            raise ToolDeniedError(
                tool,
                provider,
                f"провайдер не утверждён (approval_status={policy.approval_status})",
                policy.decision_ref,
            )
        if tool not in policy.allowed_tools:
            raise ToolDeniedError(tool, provider, "инструмент не входит в allowlist провайдера")

    def check_resource_uri(self, provider: str, uri: str) -> None:
        policy = self.policies.get(provider)
        if policy is None or uri not in policy.fetch_resource_uris:
            raise ToolDeniedError("fetch_resource", provider, f"URI {uri!r} не разрешён", "ARCH-03")
