"""LLM Gateway: режимы `off`, `replay`, `live` (ARCH-06, DEC-07, DEC-08, план §2.22).

S1 работает в `off` (DEC-08). `replay` воспроизводит только реально записанные ответы по строгому ключу;
без записи честно возвращается `unavailable`, а не выдуманная интерпретация. `live` в S1 не включается.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from odin.config import LlmSettings
from odin.contracts import AgentResult
from odin.logging_setup import get_logger

logger = get_logger("llm")

LlmMode = Literal["off", "replay", "live"]


class InterpretationReplayKey(BaseModel):
    """Строгий ключ воспроизведения (план §2.8): другой текст, даты или состав не подставляются."""

    text: str
    reference_date: str
    schema_version: str
    role: str


def replay_key(*, text: str, reference_date: str, schema_version: str, role: str) -> str:
    canonical = json.dumps(
        InterpretationReplayKey(
            text=text, reference_date=reference_date, schema_version=schema_version, role=role
        ).model_dump(),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class LlmBudget:
    calls_used: int = 0
    call_limit: int = 4

    def take(self) -> None:
        if self.calls_used >= self.call_limit:
            raise LlmBudgetExceeded(f"бюджет вызовов LLM за ход исчерпан ({self.call_limit})")
        self.calls_used += 1


class LlmBudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class LlmGateway:
    settings: LlmSettings
    budget: LlmBudget = field(default_factory=LlmBudget)

    @property
    def mode(self) -> LlmMode:
        return self.settings.mode  # type: ignore[return-value]

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "key": self.settings.describe_key(),
            "live_enabled": self.settings.live_enabled,
            "free_dialog": self.mode == "live" and self.settings.live_enabled,
        }

    def start_turn(self) -> None:
        self.budget = LlmBudget(call_limit=self.budget.call_limit)

    def complete_structured(
        self,
        *,
        role: str,
        text: str,
        reference_date: str,
        schema_version: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Один структурированный вызов роли. Результат проверяется по Pydantic, повтора нет."""
        if self.mode == "off":
            return AgentResult(
                agent=role,
                status="unavailable",
                structured_output=None,
                uncertainties=[
                    "LLM отключён (ODIN_LLM_MODE=off): доступен только структурированный ввод (DEC-08)"
                ],
            )
        if self.mode == "live" and not self.settings.live_enabled:
            return AgentResult(
                agent=role,
                status="unavailable",
                structured_output=None,
                uncertainties=["LLM live требует ключ и решение DEC-07: свободный диалог недоступен"],
            )
        if self.mode == "live":
            # В S1 Live-вызовы не выполняются: ключа нет, DEC-07 не принят.
            return AgentResult(
                agent=role,
                status="unavailable",
                structured_output=None,
                uncertainties=["Live-вызов LLM в S1 не выполняется: DEC-07 не принят"],
            )
        return self._replay(
            role=role,
            text=text,
            reference_date=reference_date,
            schema_version=schema_version,
            payload=payload,
        )

    def _replay(
        self,
        *,
        role: str,
        text: str,
        reference_date: str,
        schema_version: str,
        payload: dict[str, Any] | None,
    ) -> AgentResult:
        key = replay_key(text=text, reference_date=reference_date, schema_version=schema_version, role=role)
        directory = self.settings.replay_dir
        if directory is None or not Path(directory).is_dir():
            return AgentResult(
                agent=role,
                status="unavailable",
                structured_output=None,
                uncertainties=["Replay-записей нет: свободный диалог недоступен (DEC-08)"],
            )
        path = Path(directory) / f"{key}.json"
        if not path.is_file():
            return AgentResult(
                agent=role,
                status="unavailable",
                structured_output=None,
                uncertainties=["нет совместимой Replay-записи для этого запроса (план §2.8)"],
            )
        try:
            self.budget.take()
            recorded = json.loads(path.read_text(encoding="utf-8"))
            if recorded.get("key") != key:
                raise ValueError("ключ записи не совпадает со строгим ключом запроса")
            structured = recorded.get("structured_output")
            if not isinstance(structured, dict):
                raise ValueError("в записи нет structured_output")
            return AgentResult(
                agent=role,
                status="ok",
                structured_output=structured,
                evidence_refs=[f"replay:{path.name}"],
                uncertainties=["ответ воспроизведён из записи, а не получен сейчас"],
            )
        except (OSError, ValueError, ValidationError) as exc:
            logger.warning("replay не пригоден: %s", type(exc).__name__)
            return AgentResult(
                agent=role,
                status="error",
                structured_output=None,
                uncertainties=[f"Replay-запись непригодна: {type(exc).__name__}"],
            )
