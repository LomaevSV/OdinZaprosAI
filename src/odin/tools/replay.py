"""Replay: локальные снимки ответов Live и строгий переход Live → Replay (DATA-MODE-02/03, план §2.8).

- Ключ: провайдер, инструмент, нормализованные значимые параметры. Другие даты, состав или фильтры
  не подставляются (T-NEG-REPLAY-02).
- Снимок используется только при сбое Live; результат помечается `data_mode=replay`, доступность
  отелей — всегда `unknown`, цена — «на дату получения, не текущая».
- Каталог `replay_store/` исключён из git до DEC-24; в репозиторий попадают только синтетические фикстуры.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Параметры, которые не влияют на смысл ответа и не входят в ключ.
NON_SIGNIFICANT = frozenset({"ttl_minutes", "request_id", "call_id", "trace", "utm"})


def normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Каноническая форма значимых параметров: регистр строк, порядок списков-множеств, без служебных полей."""

    def norm(value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(value.strip().lower().replace("ё", "е").split())
        if isinstance(value, dict):
            return {key: norm(item) for key, item in sorted(value.items()) if key not in NON_SIGNIFICANT}
        if isinstance(value, (list, tuple)):
            items = [norm(item) for item in value]
            return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        return value

    return {key: norm(value) for key, value in sorted(arguments.items()) if key not in NON_SIGNIFICANT}


def replay_key(provider: str, tool: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"provider": provider, "tool": tool, "arguments": normalize_arguments(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    key: str
    provider: str
    tool: str
    arguments: dict[str, Any]
    retrieved_at: datetime
    payload: dict[str, Any]


class ReplayStore:
    """Файловое хранилище снимков: один JSON на ключ. Запись — только по факту успешного Live-ответа."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def save(
        self, *, provider: str, tool: str, arguments: dict[str, Any], retrieved_at: datetime, payload: Any
    ) -> str:
        if retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at без часового пояса недопустим")
        key = replay_key(provider, tool, arguments)
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key,
            "provider": provider,
            "tool": tool,
            "arguments": normalize_arguments(arguments),
            "retrieved_at": retrieved_at.isoformat(),
            "payload": payload,
        }
        self._path(key).write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")
        return key

    def lookup(self, *, provider: str, tool: str, arguments: dict[str, Any]) -> ReplaySnapshot | None:
        """Только точное совпадение ключа и параметров; повреждённый снимок не используется."""
        key = replay_key(provider, tool, arguments)
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("key") != key or record.get("arguments") != normalize_arguments(arguments):
                return None
            retrieved_at = datetime.fromisoformat(record["retrieved_at"])
            if retrieved_at.tzinfo is None:
                return None
            return ReplaySnapshot(
                key=key,
                provider=record["provider"],
                tool=record["tool"],
                arguments=record["arguments"],
                retrieved_at=retrieved_at,
                payload=record["payload"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None


def replay_label(retrieved_at: datetime) -> str:
    return f"цена на {retrieved_at.date().isoformat()}, не текущая (Replay)"
