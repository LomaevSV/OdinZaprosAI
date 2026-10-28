"""Минимальный MCP-клиент: Streamable HTTP, ответы JSON и SSE (DEC-04, план §4 S2).

- Только методы ``initialize``, ``notifications/initialized``, ``tools/list``, ``tools/call``,
  ``resources/read``. Сервер может не выдавать идентификатор сессии (Tutu, 24.10) — это допустимо.
- Хост проверяется до сети (allowlist провайдера), редиректы не исполняются.
- Политику «какой инструмент можно вызывать» проверяет адаптер через ``ToolPolicy`` **до** вызова;
  клиент ничего не решает о правах.
- Ответ сервера — недоверенные данные (ARCH-03): поле ``instructions`` и тексты не исполняются.
"""

from __future__ import annotations

import itertools
import json
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "odin-zapros", "version": "0.2.0"}


class McpError(RuntimeError):
    """Сбой транспорта или протокола. ``code`` — нормативный код для ``ToolResult.error_code``."""

    def __init__(self, code: str, status: str = "error", message: str | None = None) -> None:
        self.code = code
        self.status = status  # error | unavailable | timeout
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class McpToolResult:
    is_error: bool
    content_text: str | None
    structured: Any
    raw: dict[str, Any]

    def payload(self) -> Any:
        """Полезная нагрузка: ``structuredContent`` или JSON из ``content[].text``; иначе текст."""
        if self.structured is not None:
            return self.structured
        if self.content_text is None:
            return None
        try:
            return json.loads(self.content_text)
        except ValueError:
            return self.content_text


def _parse_sse(text: str, request_id: int | None) -> dict[str, Any] | None:
    """Ищет в потоке SSE JSON-RPC сообщение с нужным ``id`` (или первое, если ``id`` не задан)."""
    data_lines: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line == "" and data_lines:
            chunk = "\n".join(data_lines)
            data_lines = []
            try:
                message = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(message, dict) and (request_id is None or message.get("id") == request_id):
                return message
    return None


class McpClient:
    def __init__(
        self,
        *,
        endpoint: str,
        allowed_hosts: frozenset[str],
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self.endpoint = endpoint
        self.allowed_hosts = allowed_hosts
        self.protocol_version = protocol_version
        self.session_id: str | None = None
        self.server_info: dict[str, Any] = {}
        self.negotiated_version: str | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._initialized = False
        self._client = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False)
        self._check_url(endpoint)

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "") not in self.allowed_hosts:
            raise McpError("host_not_allowed", "error", f"MCP-адрес вне allowlist: {parts.hostname}")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._initialized and self.negotiated_version:
            headers["MCP-Protocol-Version"] = self.negotiated_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self._check_url(self.endpoint)  # до сети
        try:
            response = self._client.post(self.endpoint, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise McpError("timeout", "timeout") from exc
        except httpx.TransportError as exc:
            raise McpError(f"transport:{type(exc).__name__}", "unavailable") from exc
        code = response.status_code
        if code == 202:
            return None
        if code in (401, 403):
            raise McpError(f"auth_{code}", "error")
        if code == 429:
            raise McpError("rate_limited", "unavailable")
        if code >= 500:
            raise McpError(f"server_{code}", "unavailable")
        if code >= 300:
            raise McpError(f"http_{code}", "error")
        session = response.headers.get("Mcp-Session-Id")
        if session:
            self.session_id = session
        if "id" not in payload:
            return None
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            message = _parse_sse(response.text, payload["id"])
        elif "json" in content_type:
            try:
                message = response.json()
            except ValueError as exc:
                raise McpError("invalid_json") from exc
        else:
            raise McpError("unexpected_content_type")
        if not isinstance(message, dict):
            raise McpError("empty_response")
        if "error" in message:
            error = message.get("error") or {}
            raise McpError(f"rpc_{error.get('code', 'error')}", "error", str(error.get("message", ""))[:200])
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError("no_result")
        return result

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            payload["params"] = params
        result = self._post(payload)
        assert result is not None
        return result

    def initialize(self) -> dict[str, Any]:
        with self._lock:
            if self._initialized:
                return self.server_info
            result = self._request(
                "initialize",
                {"protocolVersion": self.protocol_version, "capabilities": {}, "clientInfo": CLIENT_INFO},
            )
            self.negotiated_version = str(result.get("protocolVersion") or self.protocol_version)
            info = result.get("serverInfo")
            self.server_info = dict(info) if isinstance(info, dict) else {}
            self._initialized = True
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return self.server_info

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(10):  # защита от бесконечной пагинации
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor") if isinstance(result.get("nextCursor"), str) else None
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        texts = [
            part.get("text", "")
            for part in result.get("content") or []
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        return McpToolResult(
            is_error=bool(result.get("isError")),
            content_text="".join(texts) if texts else None,
            structured=result.get("structuredContent"),
            raw=result,
        )

    def read_resource(self, uri: str) -> list[dict[str, Any]]:
        self.initialize()
        result = self._request("resources/read", {"uri": uri})
        contents = result.get("contents")
        return [item for item in contents if isinstance(item, dict)] if isinstance(contents, list) else []

    def close(self) -> None:
        self._client.close()
