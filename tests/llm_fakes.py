"""Сценарный двойник LLM для тестов (без сети и без платных вызовов).

Отвечает по роли (определяется системным промптом) и тексту пользователя между ``<<<`` и ``>>>``.
Возвращает тот же ``ChatResult``, что и настоящий клиент Chat Completions.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from odin.llm.openai_client import ChatResult

MONTHS_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def ru_date(value: Any) -> str:
    """«24 ноября 2026» для дат в тестовых фразах."""
    return f"{value.day} {MONTHS_GENITIVE[value.month]} {value.year}"


def _between(content: str) -> str:
    start, end = content.find("<<<\n"), content.rfind("\n>>>")
    return content[start + 4 : end] if start != -1 and end != -1 else content


class ScriptedLlm:
    def __init__(
        self,
        interpreter: dict[str, Any] | Callable[[str, list[dict[str, str]]], Any],
        composer: Callable[[str], str] | None = None,
        failure: ChatResult | None = None,
    ) -> None:
        self.interpreter = interpreter
        self.composer = composer
        self.failure = failure
        self.requests: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> ChatResult:
        self.requests.append(messages)
        if self.failure is not None:
            return self.failure
        system = messages[0]["content"]
        if "«Интерпретатор»" in system:
            text = _between(messages[1]["content"])
            if callable(self.interpreter):
                reply = self.interpreter(text, messages)
            else:
                reply = self.interpreter.get(text, {})
            content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        else:
            draft = _between(messages[1]["content"])
            text = self.composer(draft) if self.composer else draft
            content = json.dumps({"text": text}, ensure_ascii=False)
        return ChatResult("ok", content, None, 200, 1, 7, 120, 40, "fake-model", "stop")

    def roles(self) -> list[str]:
        return [
            "interpreter" if "«Интерпретатор»" in request[0]["content"] else "composer"
            for request in self.requests
        ]


LIVE_ENV = {
    # Данные — Mock: тесты LLM-конвейера не ходят в сеть (иначе auto выбрал бы live).
    "ODIN_DATA_MODE": "mock",
    "ODIN_LLM_MODE": "live",
    "ODIN_LLM_BASE_URL": "https://llm.example.test/v1",
    "ODIN_LLM_MODEL": "fake-model",
    "ODIN_LLM_API_KEY": "test-key",
}
