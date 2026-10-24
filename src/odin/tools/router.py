"""Маршрутизатор режимов Live → Replay (план §2.8, DATA-MODE-01..03, PROVIDER-02).

Live-обработчик вызывается только для утверждённого провайдера (проверка — в `ToolPolicy` до сети).
Успешный Live-ответ записывается в локальный Replay (DEC-24). При сбое Live используется только снимок
со строгим совпадением ключа; иначе — честный `unavailable`. Mock здесь никогда не подставляется (DEC-23).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from odin.tools.replay import ReplayStore, replay_label

LiveHandler = Callable[[dict[str, Any]], dict[str, Any]]
FAILED_STATUSES = frozenset({"error", "unavailable", "timeout"})


class ModeRouter:
    def __init__(
        self,
        *,
        provider: str,
        tool: str,
        live: LiveHandler,
        store: ReplayStore | None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self.provider = provider
        self.tool = tool
        self.live = live
        self.store = store
        self.clock = clock

    def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        failure: str
        try:
            response = self.live(dict(arguments))
        except Exception as exc:  # сбой транспорта/адаптера — повод проверить Replay, но не выдумывать данные
            response = None
            failure = f"live_exception:{type(exc).__name__}"
        else:
            if response.get("status") not in FAILED_STATUSES:
                if self.store is not None and response.get("status") in ("ok", "partial"):
                    self.store.save(
                        provider=self.provider,
                        tool=self.tool,
                        arguments=arguments,
                        retrieved_at=self.clock(),
                        payload=response.get("data"),
                    )
                return {**response, "data_mode": "live"}
            failure = str(response.get("error_code") or response.get("status"))

        snapshot = None
        if self.store is not None:
            snapshot = self.store.lookup(provider=self.provider, tool=self.tool, arguments=arguments)
        if snapshot is None:
            return {
                "status": "unavailable",
                "data": None,
                "sources": [],
                "error_code": f"live_failed_no_replay:{failure}",
                "warnings": ["Live недоступен, совместимого Replay нет: результат неизвестен (DATA-MODE-03)"],
                "data_mode": None,
            }
        return {
            "status": "partial",
            "data": snapshot.payload,
            "sources": [],
            "missing": ["текущие цены и наличие"],
            "warnings": [replay_label(snapshot.retrieved_at), f"Live недоступен: {failure}"],
            "data_mode": "replay",
            "replay_retrieved_at": snapshot.retrieved_at.isoformat(),
        }
