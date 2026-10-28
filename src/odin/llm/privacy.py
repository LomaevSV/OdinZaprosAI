"""Минимизация данных перед передачей текста LLM-провайдеру (решение руководителя 25.10, DEC-07, SECURITY-01).

Текст пользователя уходит провайдеру только при ``ODIN_LLM_MODE=live`` и только текущее сообщение
(плюс краткие условия поездки, без истории переписки). Перед отправкой из него вырезаются секреты и
персональные данные, не нужные для подбора поездки: ключи и токены, e-mail, длинные номера
(телефоны, карты, документы). Пользователь получает уведомление о том, что скрыто.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PLACEHOLDER = "[скрыто]"

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ключ или токен", re.compile(r"\b(?:sk|pk|rk|ghp|xox[abp])[-_][A-Za-z0-9_\-]{12,}")),
    ("ключ или токен", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    ("ключ или токен", re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
    ("e-mail", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    # Телефон, номер карты или документа: 10+ цифр подряд (с пробелами, дефисами, скобками).
    ("номер телефона, карты или документа", re.compile(r"(?<!\d)\+?\d(?:[\s\-()]*\d){9,}(?!\d)")),
)


@dataclass(frozen=True, slots=True)
class Redaction:
    text: str
    kinds: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.kinds)

    def notice(self) -> str | None:
        if not self.kinds:
            return None
        return "Перед передачей LLM из сообщения скрыто: " + ", ".join(self.kinds)


def redact(text: str) -> Redaction:
    """Возвращает текст без секретов и лишних персональных данных и список скрытых категорий."""
    kinds: list[str] = []
    result = text

    for kind, pattern in _PATTERNS:

        def replace(match: re.Match[str], kind: str = kind) -> str:
            value = match.group(0)
            if _ISO_DATE.search(value):  # диапазон ISO-дат — не номер
                return value
            if kind not in kinds:
                kinds.append(kind)
            return PLACEHOLDER

        result = pattern.sub(replace, result)
    return Redaction(result, tuple(kinds))


PRIVACY_NOTICE = (
    "Сообщения в чате передаются выбранному LLM-провайдеру для разбора условий поездки: только текущее "
    "сообщение и краткие условия поездки, без истории переписки. Ключи, e-mail, номера телефонов, карт и "
    "документов скрываются до отправки; тексты не пишутся в журналы. Форма отправляется без LLM."
)
