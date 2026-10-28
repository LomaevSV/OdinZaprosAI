"""Сборка приложения: конфигурация, хранилище, реестр инструментов, LLM-шлюз, оркестратор."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from odin.config import LlmSettings, Settings
from odin.contracts.api import StructuredForm
from odin.llm.gateway import LlmGateway
from odin.orchestrator import Orchestrator
from odin.storage import Storage
from odin.tools.mock_adapters import MOCK_HANDLERS
from odin.tools.policy import ToolPolicy, load_provider_policies
from odin.tools.registry import ToolRegistry


@dataclass(slots=True)
class Application:
    settings: Settings
    storage: Storage
    registry: ToolRegistry
    llm: LlmGateway
    orchestrator: Orchestrator


def build_handlers(
    settings: Settings,
    policy: ToolPolicy,
    *,
    mcp_transport: httpx.BaseTransport | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Обработчики логических инструментов по режиму данных (DATA-MODE-01, DEC-23)."""
    if settings.data_mode == "mock":
        return dict(MOCK_HANDLERS)
    from odin.tools.live_handlers import build_live_toolset

    toolset = build_live_toolset(
        policy=policy,
        data_mode=settings.data_mode,
        replay_dir=settings.replay_dir,
        timeout_s=settings.limits.tool_timeout_s,
        retries=settings.limits.tool_retries,
        mcp_transport=mcp_transport,
        http_transport=http_transport,
    )
    return toolset.handlers()


def build_application(
    settings: Settings,
    *,
    registry_path: Path | None = None,
    llm_client_factory: Callable[[LlmSettings], Any] | None = None,
    mcp_transport: httpx.BaseTransport | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> Application:
    storage = Storage(settings.db_path)
    policy = ToolPolicy(data_mode=settings.data_mode, policies=load_provider_policies(registry_path))
    handlers = build_handlers(settings, policy, mcp_transport=mcp_transport, http_transport=http_transport)
    registry = ToolRegistry(policy=policy, handlers=handlers, limits=settings.limits)
    llm = (
        LlmGateway(settings=settings.llm, client_factory=llm_client_factory)
        if llm_client_factory is not None
        else LlmGateway(settings=settings.llm)
    )
    llm.set_call_limit(settings.limits.llm_calls_per_turn)
    orchestrator = Orchestrator(settings=settings, storage=storage, registry=registry, llm=llm)
    return Application(
        settings=settings, storage=storage, registry=registry, llm=llm, orchestrator=orchestrator
    )


__all__ = ["Application", "StructuredForm", "build_application", "datetime", "UTC"]
