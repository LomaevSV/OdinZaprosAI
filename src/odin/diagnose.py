"""Офлайн-диагностика конфигурации: ``python -m odin.diagnose`` (без сети и без платных вызовов).

Показывает, в каком режиме запустится приложение и почему; ключ LLM не выводится (только «present/absent»).
Код возврата: 0 — конфигурация пригодна, 1 — ошибка конфигурации.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from odin.config import ConfigError, Settings, load_env_file


def report(env_file: str = ".env") -> tuple[int, dict[str, Any]]:
    present = Path(env_file).is_file()
    loaded = load_env_file(env_file) if present else []
    source = {"env_file": env_file if present else None, "env_vars_from_file": sorted(loaded)}
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        return 1, {"ok": False, **source, "error": str(exc)}
    llm = settings.llm
    calls = 0
    if llm.live_enabled:
        calls = 1 + (1 if llm.composer else 0)
    result: dict[str, Any] = {
        "ok": True,
        **source,
        "data_mode": settings.data_mode,
        "data_mode_auto": settings.data_mode_auto,
        "data_mode_reason": (
            "auto: LLM настроена → live"
            if settings.data_mode_auto and settings.data_mode == "live"
            else "auto: LLM не настроена → mock (ДЕМО)"
            if settings.data_mode_auto
            else "задан явно (ODIN_DATA_MODE)"
        ),
        "network_sources": settings.data_mode in ("live", "mixed"),
        "llm": {
            "mode": llm.mode,
            "free_dialog": llm.live_enabled or llm.mode == "replay",
            "host": (urlsplit(llm.base_url).hostname if llm.base_url else None),
            "model": llm.model,
            "key": llm.describe_key(),
            "composer": llm.composer,
            "json_mode": llm.json_mode,
            "retries": llm.retries,
            "timeout_s": llm.timeout_s,
            "max_output_tokens": llm.max_output_tokens,
            "paid_calls_per_turn_typical": calls,
            "paid_calls_per_turn_max": settings.limits.llm_calls_per_turn if llm.live_enabled else 0,
        },
        "port": settings.port,
        "db_path": str(settings.db_path),
    }
    return 0, result


def main() -> int:
    code, data = report()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
