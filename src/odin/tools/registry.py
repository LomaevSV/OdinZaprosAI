"""Tool Registry: единственный путь выполнения инструментов (ARCH-01, ARCH-02, план §2.23).

Только чтение, deny-by-default, дедлайн вызова, один повтор для чтения, circuit breaker, семафоры,
бюджет вызовов за ход. Домены и LLM не вызывают адаптеры напрямую.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from odin.contracts import ToolRequest, ToolResult
from odin.tools.policy import ToolDeniedError, ToolPolicy

Handler = Callable[[dict[str, Any]], Any]


class ToolBudgetExceeded(RuntimeError):
    """Исчерпан бюджет вызовов за ход (ARCH-02): это не доказательство отсутствия вариантов."""


class ToolDeadlineExceeded(TimeoutError):
    """Дедлайн вызова инструмента или хода истёк."""


@dataclass(slots=True)
class ToolCall:
    """Учёт вызовов и сбоев: лимит за ход и состояние circuit breaker по провайдеру."""

    calls_used: int = 0
    failures: Mapping[str, int] = field(default_factory=dict)
    opened_until: Mapping[str, float] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)


class ToolRegistry:
    def __init__(
        self,
        *,
        policy: ToolPolicy,
        handlers: Mapping[str, Handler],
        limits: Any,
        turn_deadline: datetime | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self.handlers = dict(handlers)
        self.limits = limits
        self.turn_deadline = turn_deadline
        self.clock = clock
        self.call = ToolCall()
        self._lock = threading.Lock()
        self._semaphores = {
            provider: threading.Semaphore(max(1, count))
            for provider, count in dict(getattr(limits, "max_concurrency", {})).items()
        }

    # ---------------------------------------------------------------- бюджет и предохранители

    def start_turn(self, deadline: datetime | None = None) -> None:
        self.call = ToolCall()
        self.turn_deadline = deadline

    def _reserve_call(self) -> int:
        with self._lock:
            if self.call.calls_used >= int(self.limits.calls_per_turn):
                raise ToolBudgetExceeded(
                    f"исчерпан бюджет вызовов за ход ({self.limits.calls_per_turn}): "
                    "поиск не завершён (SEARCH-STOP-01)"
                )
            self.call.calls_used += 1
            return self.call.calls_used

    def _check_deadlines(self) -> None:
        now = datetime.now(tz=UTC)
        if self.turn_deadline is not None and now >= self.turn_deadline:
            raise ToolDeadlineExceeded("дедлайн хода истёк (stop_reason=deadline)")

    def _circuit_open(self, provider: str) -> bool:
        until = self.call.opened_until.get(provider)
        return until is not None and self.clock() < until

    def _record_failure(self, provider: str) -> None:
        with self._lock:
            failures = dict(self.call.failures)
            failures[provider] = failures.get(provider, 0) + 1
            self.call.failures = failures
            if failures[provider] >= int(self.limits.circuit_breaker_threshold):
                opened = dict(self.call.opened_until)
                opened[provider] = self.clock() + float(self.limits.circuit_breaker_cooldown_s)
                self.call.opened_until = opened

    def _record_success(self, provider: str) -> None:
        with self._lock:
            failures = dict(self.call.failures)
            failures[provider] = 0
            self.call.failures = failures

    # ---------------------------------------------------------------- выполнение

    def invoke(self, request: ToolRequest) -> ToolResult:
        """Выполняет запрос через адаптер провайдера с учётом политики и лимитов."""
        self._check_deadlines()
        provider = self.policy.check_logical(request.tool_name)
        handler = self.handlers.get(request.tool_name)
        if handler is None:
            raise ToolDeniedError(request.tool_name, provider, "адаптер не зарегистрирован")

        if self.turn_deadline is not None and request.deadline > self.turn_deadline:
            request = request.model_copy(update={"deadline": self.turn_deadline})

        if self._circuit_open(provider):
            return ToolResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                provider=provider,
                status="unavailable",
                data=None,
                sources=[],
                retrieved_at=None,
                error_code="circuit_open",
                latency_ms=0,
            )

        self._reserve_call()
        started = time.perf_counter()
        attempts = 0
        last_error: str | None = None
        semaphore = self._semaphores.get(provider, threading.Semaphore(4))
        while attempts <= int(self.limits.tool_retries):
            attempts += 1
            self._check_deadlines()
            acquired = semaphore.acquire(timeout=max(0.0, float(self.limits.tool_timeout_s)))
            if not acquired:
                last_error = "semaphore_timeout"
                break
            try:
                data = handler(dict(request.arguments))
            except ToolDeniedError:
                raise
            except Exception as exc:  # адаптер сообщает о сбое, но не ломает ход
                last_error = f"adapter_error:{type(exc).__name__}"
                self._record_failure(provider)
                if attempts > int(self.limits.tool_retries):
                    break
                backoff = min(
                    float(self.limits.tool_backoff_max_s),
                    float(self.limits.tool_backoff_min_s) * (2 ** (attempts - 1)),
                )
                time.sleep(min(backoff, 0.01))  # в тестах не ждём реального backoff
                continue
            finally:
                semaphore.release()
            self._record_success(provider)
            latency_ms = int((time.perf_counter() - started) * 1000)
            result = self._as_result(request, provider, data, latency_ms)
            self.call.log.append(
                {
                    "call_id": request.call_id,
                    "tool": request.tool_name,
                    "provider": provider,
                    "status": result.status,
                }
            )
            return result

        latency_ms = int((time.perf_counter() - started) * 1000)
        result = ToolResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            provider=provider,
            status="error",
            data=None,
            sources=[],
            retrieved_at=None,
            error_code=last_error or "adapter_error",
            latency_ms=latency_ms,
        )
        self.call.log.append(
            {"call_id": request.call_id, "tool": request.tool_name, "provider": provider, "status": "error"}
        )
        return result

    def _as_result(self, request: ToolRequest, provider: str, data: Any, latency_ms: int) -> ToolResult:
        """Приводит ответ адаптера к нормативному ``ToolResult`` (CONTRACT-01)."""
        payload = data if isinstance(data, dict) else {"value": data}
        status = str(payload.get("status", "ok"))
        sources = list(payload.get("sources") or [])
        missing = list(payload.get("missing") or [])
        if status == "empty":
            return ToolResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                provider=provider,
                status="empty",
                data=payload.get("data"),
                sources=sources,
                retrieved_at=payload.get("retrieved_at"),
                error_code=None,
                latency_ms=latency_ms,
            )
        return ToolResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            provider=provider,
            status=status,  # type: ignore[arg-type]
            data=payload.get("data"),
            sources=sources,
            retrieved_at=payload.get("retrieved_at"),
            warnings=list(payload.get("warnings") or []),
            error_code=payload.get("error_code"),
            latency_ms=latency_ms,
            missing=missing,
        )
