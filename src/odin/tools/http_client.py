"""Тонкий HTTP-клиент для REST-провайдеров (план §2.2, §2.12; приложение A).

Проверка allowlist выполняется в коде **до** обращения к сети. Ошибки HTTP переводятся в нормативные
статусы `ToolResult` (CONTRACT-01). Ретраи — только для чтения, не больше `retries`; для 429 соблюдается
`Retry-After`. Ответ — недоверенные данные: проверяется тип содержимого и JSON.

Не является MCP-клиентом: выбор MCP-клиента — решение DEC-04 (открыто).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class HostNotAllowedError(PermissionError):
    """Хост не входит в allowlist провайдера: запрос не отправляется."""


@dataclass(frozen=True, slots=True)
class HttpOutcome:
    status: str  # ok | empty | error | unavailable | timeout
    data: Any
    error_code: str | None
    http_status: int | None
    attempts: int


def _retry_after_seconds(response: httpx.Response, cap: float) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return min(cap, max(0.0, float(raw)))
    except ValueError:
        return min(cap, 1.0)


class HttpJsonClient:
    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        timeout_s: float,
        retries: int = 1,
        backoff_s: float = 0.5,
        max_wait_s: float = 2.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.timeout_s = timeout_s
        self.retries = retries
        self.backoff_s = backoff_s
        self.max_wait_s = max_wait_s
        self.sleep = sleep
        self._client = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False)

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "") not in self.allowed_hosts:
            raise HostNotAllowedError(f"URL вне allowlist провайдера: {parts.scheme}://{parts.hostname}")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> HttpOutcome:
        self.check_url(url)  # до сети
        attempts = 0
        last: HttpOutcome | None = None
        while attempts <= self.retries:
            attempts += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException:
                last = HttpOutcome("timeout", None, "timeout", None, attempts)
                continue
            except httpx.TransportError as exc:
                last = HttpOutcome("unavailable", None, f"transport:{type(exc).__name__}", None, attempts)
                continue
            code = response.status_code
            if code == 429:
                last = HttpOutcome("unavailable", None, "rate_limited", code, attempts)
                if attempts <= self.retries:
                    self.sleep(_retry_after_seconds(response, self.max_wait_s))
                continue
            if code in (401, 403):
                return HttpOutcome("error", None, f"auth_{code}", code, attempts)  # повтор бесполезен
            if code >= 500:
                last = HttpOutcome("unavailable", None, f"server_{code}", code, attempts)
                if attempts <= self.retries:
                    self.sleep(min(self.max_wait_s, self.backoff_s * attempts))
                continue
            if code >= 400:
                return HttpOutcome("error", None, f"http_{code}", code, attempts)
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return HttpOutcome("error", None, "unexpected_content_type", code, attempts)
            try:
                data = response.json()
            except ValueError:
                return HttpOutcome("error", None, "invalid_json", code, attempts)
            if data in (None, [], {}):
                return HttpOutcome("empty", data, None, code, attempts)
            return HttpOutcome("ok", data, None, code, attempts)
        assert last is not None
        return last

    def close(self) -> None:
        self._client.close()
