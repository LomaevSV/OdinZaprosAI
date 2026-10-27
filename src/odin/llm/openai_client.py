"""Клиент OpenAI-совместимого Chat Completions API (ARCH-06, план §2.2).

Свойства:
- ``base_url`` из конфигурации; хост запроса проверяется до сети (только хост ``base_url``, без редиректов);
- HTTPS обязателен, кроме локальных серверов моделей (loopback, ``host.docker.internal``);
- ключ передаётся только в заголовке ``Authorization`` и не попадает в журнал, ошибки и ответы API;
- тексты запросов и ответов модели не журналируются (SECURITY-01);
- повтор только для 429/5xx/тайм-аута/обрыва (не больше ``retries``), 401/403/404 — без повтора;
- HTTP 400 на расширенных параметрах (``response_format``, ``temperature``) — один повтор в
  совместимом виде: часть OpenAI-совместимых провайдеров эти параметры не поддерживает.

Нативный function calling не используется (ARCH-06: «не обязателен»): структура проверяется Pydantic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from odin.logging_setup import get_logger

logger = get_logger("llm")

ChatStatus = Literal["ok", "error", "unavailable", "timeout"]

# Хосты, для которых допустим HTTP без TLS: локальный сервер модели (Ollama, vLLM и т. п.).
LOCAL_HTTP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})


class LlmEndpointError(ValueError):
    """Недопустимый адрес LLM: запрос не отправляется."""


@dataclass(frozen=True, slots=True)
class ChatResult:
    status: ChatStatus
    content: str | None
    error_code: str | None
    http_status: int | None
    attempts: int
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None
    finish_reason: str | None = None


def chat_completions_url(base_url: str) -> str:
    """``https://host/v1`` → ``https://host/v1/chat/completions``; готовый путь не дублируется."""
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def validate_endpoint(base_url: str) -> str:
    """Проверка адреса до сети. Возвращает хост — единственный разрешённый для запросов."""
    parts = urlsplit(base_url.strip())
    host = (parts.hostname or "").lower()
    if not host:
        raise LlmEndpointError("ODIN_LLM_BASE_URL: не указан хост")
    if parts.scheme == "https":
        return host
    if parts.scheme == "http" and host in LOCAL_HTTP_HOSTS:
        return host
    raise LlmEndpointError(
        "ODIN_LLM_BASE_URL: нужен https:// (http:// допустим только для локального сервера модели)"
    )


def _retry_after(response: httpx.Response, cap: float) -> float:
    try:
        return min(cap, max(0.0, float(response.headers.get("Retry-After", ""))))
    except ValueError:
        return min(cap, 1.0)


def _content_text(message: Any) -> str | None:
    """``content`` бывает строкой или списком частей ``{type: text, text}``."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "".join(parts) if parts else None
    return None


class ChatCompletionsClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float,
        max_output_tokens: int,
        json_mode: bool = True,
        temperature: float | None = 0.0,
        retries: int = 1,
        backoff_s: float = 0.5,
        max_wait_s: float = 2.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        before_post: Callable[[], None] | None = None,
        on_attempt: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.allowed_host = validate_endpoint(base_url)
        self.url = chat_completions_url(base_url)
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.json_mode = json_mode
        self.temperature = temperature
        self.retries = retries
        self.backoff_s = backoff_s
        self.max_wait_s = max_wait_s
        self.sleep = sleep
        self.before_post = before_post
        self.on_attempt = on_attempt
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False)

    def _body(self, messages: list[dict[str, str]], *, compatible: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }
        if not compatible:
            if self.temperature is not None:
                body["temperature"] = self.temperature
            if self.json_mode:
                body["response_format"] = {"type": "json_object"}
        return body

    def complete(self, messages: list[dict[str, str]]) -> ChatResult:
        """Один логический вызов (с ограниченными повторами транспорта)."""
        if (urlsplit(self.url).hostname or "").lower() != self.allowed_host:  # до сети
            raise LlmEndpointError("адрес LLM вне allowlist")
        started = time.perf_counter()
        attempts = 0
        compatible = False
        compat_used = False
        last: ChatResult | None = None

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        def budget() -> int:
            return 1 + self.retries + (1 if compat_used else 0)

        while attempts < budget():
            if self.before_post is not None:
                self.before_post()  # fail closed, before every real HTTP POST
            attempts += 1
            post_started = time.perf_counter()
            response = None
            post_error = None
            try:
                response = self._client.post(
                    self.url, headers=self._headers, json=self._body(messages, compatible=compatible)
                )
            except httpx.TimeoutException:
                post_error = "timeout"
                last = ChatResult("timeout", None, "timeout", None, attempts, elapsed())
                continue
            except httpx.TransportError as exc:
                post_error = "transport:" + type(exc).__name__
                last = ChatResult(
                    "unavailable", None, f"transport:{type(exc).__name__}", None, attempts, elapsed()
                )
                continue
            finally:
                if self.on_attempt is not None:
                    usage = {}
                    if response is not None:
                        try:
                            raw = response.json()
                            if isinstance(raw, dict) and isinstance(raw.get("usage"), dict):
                                usage = raw["usage"]
                        except (ValueError, TypeError):
                            pass
                    try:
                        self.on_attempt(
                            {
                                "model": self.model,
                                "status": "error" if post_error else "http",
                                "http_status": response.status_code if response is not None else None,
                                "error_code": post_error,
                                "compatibility": compatible,
                                "latency_ms": int((time.perf_counter() - post_started) * 1000),
                                "prompt_tokens": usage.get("prompt_tokens")
                                if isinstance(usage.get("prompt_tokens"), int)
                                else None,
                                "completion_tokens": usage.get("completion_tokens")
                                if isinstance(usage.get("completion_tokens"), int)
                                else None,
                                "usage_status": "reported" if usage else "missing",
                                "cost_status": "unknown",
                                "cost_amount": None,
                            }
                        )
                    except Exception as exc:  # телеметрия не меняет бизнес-результат (OBS-SPAN-FAIL-01)
                        logger.warning("llm attempt sink failed: %s", type(exc).__name__)
            code = response.status_code
            if code == 400 and not compatible and (self.json_mode or self.temperature is not None):
                compatible = True
                compat_used = True
                last = ChatResult("error", None, "http_400", code, attempts, elapsed())
                continue
            if code == 429:
                last = ChatResult("unavailable", None, "rate_limited", code, attempts, elapsed())
                if attempts < budget():
                    self.sleep(_retry_after(response, self.max_wait_s))
                continue
            if code in (401, 403):
                return ChatResult("error", None, f"auth_{code}", code, attempts, elapsed())
            if code >= 500:
                last = ChatResult("unavailable", None, f"server_{code}", code, attempts, elapsed())
                if attempts < budget():
                    self.sleep(min(self.max_wait_s, self.backoff_s * attempts))
                continue
            if code >= 300:  # 3xx: редиректы не исполняются (адрес вне allowlist), 4xx — ошибка запроса
                return ChatResult("error", None, f"http_{code}", code, attempts, elapsed())
            try:
                data = response.json()
            except ValueError:
                return ChatResult("error", None, "invalid_json", code, attempts, elapsed())
            return self._parse(data, code, attempts, elapsed())
        assert last is not None
        return last

    @staticmethod
    def _parse(data: Any, code: int, attempts: int, latency_ms: int) -> ChatResult:
        if not isinstance(data, dict):
            return ChatResult("error", None, "unexpected_payload", code, attempts, latency_ms)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return ChatResult("error", None, "no_choices", code, attempts, latency_ms)
        choice = choices[0]
        content = _content_text(choice.get("message"))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None
        completion_tokens = (
            usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None
        )
        finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
        model = data.get("model") if isinstance(data.get("model"), str) else None
        if not content:
            return ChatResult(
                "error", None, "empty_content", code, attempts, latency_ms, prompt_tokens, completion_tokens
            )
        if finish == "length":
            return ChatResult(
                "error",
                None,
                "truncated",
                code,
                attempts,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                model,
                finish,
            )
        return ChatResult(
            "ok", content, None, code, attempts, latency_ms, prompt_tokens, completion_tokens, model, finish
        )

    def close(self) -> None:
        self._client.close()
