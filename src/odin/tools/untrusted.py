"""Защита от недоверенных данных провайдеров (ARCH-03, план §2.12).

- Закрепление схем инструментов: хеш фиксируется при старте; изменение → инструмент закрывается
  с `error_code=schema_changed` (T-NEG-SCHEMA-01).
- Тексты ответов — данные, а не команды: строки не интерпретируются, URL принимаются только из
  доменов allowlist провайдера, телефоны — только по формату (T-NEG-INJ-01).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

PHONE_RE = re.compile(r"^\+?[0-9 ()\-]{5,25}$")


def schema_hash(schema: Any) -> str:
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class SchemaPins:
    """Закреплённые хеши схем разрешённых инструментов одного провайдера."""

    pins: dict[str, str] = field(default_factory=dict)
    closed: dict[str, str] = field(default_factory=dict)

    def pin(self, tool: str, schema: Any) -> None:
        self.pins[tool] = schema_hash(schema)

    def verify(self, tool: str, schema: Any) -> bool:
        """`False` и закрытие инструмента, если схема изменилась или не была закреплена."""
        expected = self.pins.get(tool)
        if expected is None:
            self.closed[tool] = "schema_not_pinned"
            return False
        if schema_hash(schema) != expected:
            self.closed[tool] = "schema_changed"
            return False
        return tool not in self.closed

    def error_code(self, tool: str) -> str | None:
        return self.closed.get(tool)


def safe_url(value: Any, allowed_domains: frozenset[str]) -> str | None:
    """URL из ответа — только https и только домен (или поддомен) из allowlist провайдера."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return None
    if any(host == domain or host.endswith("." + domain) for domain in allowed_domains):
        return value.strip()
    return None


def safe_phone(value: Any) -> str | None:
    if isinstance(value, str) and PHONE_RE.match(value.strip()):
        return value.strip()
    return None


def safe_text(value: Any, limit: int = 500) -> str | None:
    """Текст источника сохраняется как данные: обрезается, управляющие символы удаляются."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(char for char in value if char.isprintable() or char == " ")
    return cleaned[:limit] or None
