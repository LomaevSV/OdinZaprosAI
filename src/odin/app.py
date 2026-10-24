"""Сборка приложения: конфигурация, хранилище, реестр инструментов, LLM-шлюз, оркестратор (S1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from odin.config import Settings
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


def build_application(settings: Settings, *, registry_path: Path | None = None) -> Application:
    storage = Storage(settings.db_path)
    policy = ToolPolicy(data_mode=settings.data_mode, policies=load_provider_policies(registry_path))
    registry = ToolRegistry(policy=policy, handlers=MOCK_HANDLERS, limits=settings.limits)
    llm = LlmGateway(settings=settings.llm)
    orchestrator = Orchestrator(settings=settings, storage=storage, registry=registry, llm=llm)
    return Application(
        settings=settings, storage=storage, registry=registry, llm=llm, orchestrator=orchestrator
    )


__all__ = ["Application", "StructuredForm", "build_application", "datetime", "UTC"]
