"""Конфигурация приложения из окружения (SECURITY-01: секреты только из env).

Все числовые параметры — `provisional` (приложение A плана v0.5.2): это обратимые ресурсные
ограничения прототипа, а не утверждённые лимиты расходов. Приоритетные решения: DEC-20 (буферы),
DEC-21 (TTL), DEC-22 (лимиты и причины остановки), DEC-07 (LLM).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DataMode = Literal["mock", "replay", "live"]
LlmMode = Literal["off", "replay", "live"]

DEFAULT_DB_PATH = Path("var") / "odin.sqlite3"


class ConfigError(RuntimeError):
    """Недопустимая конфигурация: приложение не стартует (лучше отказ, чем тихая подмена режима)."""


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}: ожидалось логическое значение, получено {raw!r}")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось целое число, получено {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name}: значение {value} меньше минимума {minimum}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось число, получено {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name}: значение {value} меньше минимума {minimum}")
    return value


def _mode(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    raw = (env.get(name) or default).strip().lower()
    if raw not in allowed:
        raise ConfigError(f"{name}: допустимые значения {allowed}, получено {raw!r}")
    return raw


@dataclass(frozen=True, slots=True)
class Limits:
    """Ресурсные лимиты хода (приложение A, provisional — DEC-22)."""

    turn_deadline_s: float = 60.0
    tool_timeout_s: float = 20.0
    tool_retries: int = 1
    tool_backoff_min_s: float = 0.5
    tool_backoff_max_s: float = 2.0
    circuit_breaker_threshold: int = 3
    circuit_breaker_cooldown_s: float = 60.0
    calls_per_turn: int = 20
    llm_calls_per_turn: int = 4
    waves: int = 2
    top_k: int = 3
    offer_ttl_minutes: int = 30
    max_concurrency: Mapping[str, int] = field(
        default_factory=lambda: {"odin_mock": 4, "osrm_demo": 2, "nominatim": 1, "open_meteo": 2}
    )
    buffers_minutes: Mapping[str, int] = field(
        default_factory=lambda: {
            "after_flight": 40,
            "after_train": 15,
            "before_flight": 120,
            "before_train": 30,
        }
    )


@dataclass(frozen=True, slots=True)
class LlmSettings:
    mode: LlmMode = "off"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    replay_dir: Path | None = None
    max_output_tokens: int = 4000
    timeout_s: float = 20.0

    @property
    def live_enabled(self) -> bool:
        return self.mode == "live" and bool(self.api_key) and bool(self.base_url)

    def describe_key(self) -> str:
        """Только факт наличия ключа — сам ключ не логируется и не отдаётся в API (SECURITY-01)."""
        return "present" if self.api_key else "absent"


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "О!дин запрос"
    version: str = "0.1.0"
    data_mode: DataMode = "mock"
    llm: LlmSettings = field(default_factory=LlmSettings)
    limits: Limits = field(default_factory=Limits)
    db_path: Path = DEFAULT_DB_PATH
    port: int = 8080
    log_level: str = "INFO"
    cors_origin: str | None = None
    api_prefix: str = "/api/v1"

    @property
    def mock_only(self) -> bool:
        return self.data_mode == "mock"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        data_mode = _mode(source, "ODIN_DATA_MODE", "mock", ("mock", "replay", "live"))
        if data_mode != "mock":
            raise ConfigError(
                "S1: доступен только Mock-путь (DEC-23); Live и Replay появятся в S2 после DEC-09/DEC-24"
            )
        llm_mode = _mode(source, "ODIN_LLM_MODE", "off", ("off", "replay", "live"))
        llm = LlmSettings(
            mode=llm_mode,  # type: ignore[arg-type]
            base_url=(source.get("ODIN_LLM_BASE_URL") or None),
            model=(source.get("ODIN_LLM_MODEL") or None),
            api_key=(source.get("ODIN_LLM_API_KEY") or None),
            replay_dir=Path(source["ODIN_LLM_REPLAY_DIR"]) if source.get("ODIN_LLM_REPLAY_DIR") else None,
            max_output_tokens=_int(source, "ODIN_LLM_MAX_OUTPUT_TOKENS", 4000, minimum=1),
            timeout_s=_float(source, "ODIN_LLM_TIMEOUT_S", 20.0, minimum=0.1),
        )
        if llm_mode == "live" and not llm.live_enabled:
            raise ConfigError(
                "ODIN_LLM_MODE=live требует ODIN_LLM_API_KEY и ODIN_LLM_BASE_URL (DEC-07); "
                "без ключа доступен только ODIN_LLM_MODE=off (DEC-08)"
            )
        limits = Limits(
            turn_deadline_s=_float(source, "ODIN_TURN_DEADLINE_S", 60.0, minimum=0.1),
            tool_timeout_s=_float(source, "ODIN_TOOL_TIMEOUT_S", 20.0, minimum=0.1),
            tool_retries=_int(source, "ODIN_TOOL_RETRIES", 1),
            calls_per_turn=_int(source, "ODIN_CALLS_PER_TURN", 20, minimum=1),
            llm_calls_per_turn=_int(source, "ODIN_LLM_CALLS_PER_TURN", 4, minimum=0),
            waves=_int(source, "ODIN_WAVES", 2, minimum=1),
            top_k=_int(source, "ODIN_TOP_K", 3, minimum=1),
            offer_ttl_minutes=_int(source, "ODIN_OFFER_TTL_MINUTES", 30, minimum=1),
        )
        return cls(
            data_mode=data_mode,  # type: ignore[arg-type]
            llm=llm,
            limits=limits,
            db_path=Path(source.get("ODIN_DB_PATH") or DEFAULT_DB_PATH),
            port=_int(source, "ODIN_PORT", 8080, minimum=1),
            log_level=(source.get("ODIN_LOG_LEVEL") or "INFO").upper(),
            cors_origin=(source.get("ODIN_CORS_ORIGIN") or None),
        )


# Имена переменных окружения (используются в .env.example и тестами на секреты).
ENV_VARS = (
    "ODIN_DATA_MODE",
    "ODIN_LLM_MODE",
    "ODIN_LLM_BASE_URL",
    "ODIN_LLM_MODEL",
    "ODIN_LLM_API_KEY",
    "ODIN_LLM_REPLAY_DIR",
    "ODIN_LLM_MAX_OUTPUT_TOKENS",
    "ODIN_LLM_TIMEOUT_S",
    "ODIN_TURN_DEADLINE_S",
    "ODIN_TOOL_TIMEOUT_S",
    "ODIN_TOOL_RETRIES",
    "ODIN_CALLS_PER_TURN",
    "ODIN_LLM_CALLS_PER_TURN",
    "ODIN_WAVES",
    "ODIN_TOP_K",
    "ODIN_OFFER_TTL_MINUTES",
    "ODIN_DB_PATH",
    "ODIN_PORT",
    "ODIN_LOG_LEVEL",
    "ODIN_CORS_ORIGIN",
)
