"""LLM Gateway: режимы `off`, `replay`, `live` (ARCH-06, DEC-07, DEC-08, план §2.22).

- ``off`` — LLM нет: свободный диалог честно недоступен (DEC-08).
- ``replay`` — только реально записанные ответы по строгому ключу; без записи — ``unavailable``.
- ``live`` — OpenAI-совместимый Chat Completions. Включается только явной конфигурацией оператора
  (``ODIN_LLM_MODE=live`` + ключ, адрес, модель): это и есть разрешение на платные вызовы.

Ответ модели — недоверенные данные: JSON проверяется Pydantic-моделью роли, при ошибке — один повтор
с сообщением об ошибке формата (ARCH-06), затем безопасная деградация. Бюджет вызовов за ход
(``ODIN_LLM_CALLS_PER_TURN``) соблюдается. Тексты пользователя и ответы модели не журналируются —
только роль, статус, код ошибки, задержка и токены.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
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


def replay_key(*, text: str, reference_date: str, schema_version: str, role: str, context: Any = None) -> str:
    """Ключ записи. ``context`` (текущие условия поездки, версия промпта) входит в ключ, если задан."""
    data = InterpretationReplayKey(
        text=text, reference_date=reference_date, schema_version=schema_version, role=role
    ).model_dump()
    if context is not None:
        data["context"] = context
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True)
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


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """Запись журнала вызова LLM без текстов (SECURITY-01, план §2.2 «Хранилище»)."""

    role: str
    status: str
    error_code: str | None
    attempts: int
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    repaired: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "status": self.status,
            "error_code": self.error_code,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "repaired": self.repaired,
        }


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def extract_json_object(content: str) -> Any:
    """JSON из ответа модели: допускаются Markdown-ограждение и текст вокруг одного объекта."""
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _error_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:3]:
            location = ".".join(str(item) for item in error.get("loc", ()))
            parts.append(f"{location}: {error.get('msg')}")
        return "; ".join(parts)
    return type(exc).__name__


ClientFactory = Callable[[LlmSettings], Any]


def default_client_factory(settings: LlmSettings, transport: httpx.BaseTransport | None = None) -> Any:
    from odin.llm.openai_client import ChatCompletionsClient

    assert settings.base_url and settings.api_key and settings.model
    return ChatCompletionsClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        timeout_s=settings.timeout_s,
        max_output_tokens=settings.max_output_tokens,
        json_mode=settings.json_mode,
        temperature=settings.temperature,
        retries=settings.retries,
        transport=transport,
    )


@dataclass(slots=True)
class LlmGateway:
    settings: LlmSettings
    budget: LlmBudget = field(default_factory=LlmBudget)
    client_factory: ClientFactory = default_client_factory
    calls: list[LlmCallRecord] = field(default_factory=list)
    _client: Any = None

    @property
    def mode(self) -> LlmMode:
        return self.settings.mode  # type: ignore[return-value]

    @property
    def live(self) -> bool:
        return self.mode == "live" and self.settings.live_enabled

    @property
    def free_dialog(self) -> bool:
        """Свободный диалог доступен в live и в replay (только для записанных ответов)."""
        return self.live or self.mode == "replay"

    @property
    def composer_enabled(self) -> bool:
        return self.live and self.settings.composer

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "key": self.settings.describe_key(),
            "live_enabled": self.settings.live_enabled,
            "model": self.settings.model if self.settings.live_enabled else None,
            "free_dialog": self.live,
            "composer": self.composer_enabled,
        }

    def set_call_limit(self, limit: int) -> None:
        self.budget = LlmBudget(call_limit=limit)

    def start_turn(self) -> None:
        self.budget = LlmBudget(call_limit=self.budget.call_limit)
        self.calls = []

    def _client_instance(self) -> Any:
        if self._client is None:
            self._client = self.client_factory(self.settings)
        return self._client

    # ------------------------------------------------------------------ основной вызов

    def complete_structured(
        self,
        *,
        role: str,
        text: str,
        reference_date: str,
        schema_version: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Один структурированный вызов роли с проверкой по Pydantic (не больше одного повтора)."""
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
                uncertainties=["LLM live требует ключ, адрес и модель (DEC-07): свободный диалог недоступен"],
            )
        context = (payload or {}).get("replay_context")
        if self.mode == "live":
            return self._live(
                role=role,
                text=text,
                reference_date=reference_date,
                schema_version=schema_version,
                payload=payload,
                context=context,
            )
        return self._replay(
            role=role,
            text=text,
            reference_date=reference_date,
            schema_version=schema_version,
            context=context,
        )

    # ------------------------------------------------------------------ live

    def _live(
        self,
        *,
        role: str,
        text: str,
        reference_date: str,
        schema_version: str,
        payload: dict[str, Any] | None,
        context: Any,
    ) -> AgentResult:
        from odin.llm.prompts import OUTPUT_MODELS, build_messages, repair_message

        model = OUTPUT_MODELS.get(role)
        if model is None:
            return AgentResult(
                agent=role, status="error", structured_output=None, uncertainties=["неизвестная роль LLM"]
            )
        messages = build_messages(role, text=text, reference_date=reference_date, payload=payload)
        repaired = False
        for attempt in range(2):  # исходный вызов + не больше одного повтора по формату
            try:
                self.budget.take()
            except LlmBudgetExceeded as exc:
                self._record(role, "budget_exceeded", "llm_budget_exceeded", None, repaired)
                return AgentResult(
                    agent=role, status="unavailable", structured_output=None, uncertainties=[str(exc)]
                )
            try:
                result = self._client_instance().complete(messages)
            except Exception as exc:  # адрес вне allowlist и т. п.: без сети и без текста в журнале
                self._record(role, "error", f"client:{type(exc).__name__}", None, repaired)
                return AgentResult(
                    agent=role,
                    status="unavailable",
                    structured_output=None,
                    uncertainties=[f"LLM недоступен: {type(exc).__name__}"],
                )
            if result.status != "ok" or result.content is None:
                self._record(role, result.status, result.error_code, result, repaired)
                return AgentResult(
                    agent=role,
                    status="unavailable",
                    structured_output=None,
                    uncertainties=[f"LLM недоступен: {result.error_code}"],
                )
            try:
                parsed = extract_json_object(result.content)
                validated = model.model_validate(parsed)
            except (ValueError, ValidationError) as exc:
                self._record(role, "invalid_output", "schema_violation", result, repaired)
                if attempt == 0:
                    repaired = True
                    messages = [
                        *messages,
                        {"role": "assistant", "content": result.content[:4000]},
                        repair_message(_error_summary(exc)),
                    ]
                    continue
                return AgentResult(
                    agent=role,
                    status="error",
                    structured_output=None,
                    uncertainties=["ответ LLM не прошёл проверку схемы после повтора"],
                )
            self._record(role, "ok", None, result, repaired)
            structured = validated.model_dump(mode="json", exclude_none=True)
            self._maybe_record_replay(
                role=role,
                text=text,
                reference_date=reference_date,
                schema_version=schema_version,
                context=context,
                structured=structured,
            )
            return AgentResult(
                agent=role,
                status="ok",
                structured_output=structured,
                evidence_refs=[f"llm:live:{self.settings.model}"],
                uncertainties=[],
            )
        raise AssertionError("недостижимо")

    def _record(self, role: str, status: str, error_code: str | None, result: Any, repaired: bool) -> None:
        record = LlmCallRecord(
            role=role,
            status=status,
            error_code=error_code,
            attempts=getattr(result, "attempts", 0),
            latency_ms=getattr(result, "latency_ms", 0),
            prompt_tokens=getattr(result, "prompt_tokens", None),
            completion_tokens=getattr(result, "completion_tokens", None),
            repaired=repaired,
        )
        self.calls.append(record)
        logger.info(
            "llm call role=%s status=%s error=%s latency_ms=%s tokens=%s/%s",
            role,
            status,
            error_code,
            record.latency_ms,
            record.prompt_tokens,
            record.completion_tokens,
        )

    def _maybe_record_replay(
        self,
        *,
        role: str,
        text: str,
        reference_date: str,
        schema_version: str,
        context: Any,
        structured: dict[str, Any],
    ) -> None:
        """Запись реально полученного ответа для ``interpretation_replay`` (локально, вне git — DEC-24)."""
        directory = self.settings.replay_dir
        if not self.settings.record or directory is None or role != "interpreter":
            return
        key = replay_key(
            text=text,
            reference_date=reference_date,
            schema_version=schema_version,
            role=role,
            context=context,
        )
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            record = {
                "key": key,
                "role": role,
                "schema_version": schema_version,
                "reference_date": reference_date,
                "recorded_at": datetime.now(tz=UTC).isoformat(),
                "model": self.settings.model,
                "structured_output": structured,
            }
            (Path(directory) / f"{key}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("запись replay LLM не удалась: %s", type(exc).__name__)

    # ------------------------------------------------------------------ replay

    def _replay(
        self,
        *,
        role: str,
        text: str,
        reference_date: str,
        schema_version: str,
        context: Any,
    ) -> AgentResult:
        key = replay_key(
            text=text,
            reference_date=reference_date,
            schema_version=schema_version,
            role=role,
            context=context,
        )
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
        except (OSError, ValueError, ValidationError, LlmBudgetExceeded) as exc:
            logger.warning("replay не пригоден: %s", type(exc).__name__)
            return AgentResult(
                agent=role,
                status="error",
                structured_output=None,
                uncertainties=[f"Replay-запись непригодна: {type(exc).__name__}"],
            )
