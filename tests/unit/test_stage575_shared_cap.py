"""Общий HTTP-cap за ход: две роли LLM + schema repair + compatibility retry (offline, MockTransport).

Аудит 57.4 «NOT VERIFIED»: бюджет и клиент проверялись по отдельности. Здесь всё идёт через реальный
``LlmGateway.complete_structured``: каждый настоящий POST резервирует квоту хода, и сумма POST по всем
ролям не превышает ``ODIN_LLM_HTTP_ATTEMPTS_PER_TURN``. Без ключей, сети и платных вызовов.
"""

from __future__ import annotations

from typing import Any

import httpx

from odin.config import LlmSettings
from odin.llm.gateway import LlmGateway
from odin.llm.openai_client import ChatCompletionsClient

INTERPRETER_OK = '{"origin": {"value": "Москва", "span": "из Москвы"}}'


def _gateway(responses: list[httpx.Response], *, cap: int, sent: list[str]) -> tuple[LlmGateway, list[dict]]:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return responses[min(len(sent) - 1, len(responses) - 1)]

    settings = LlmSettings(
        mode="live", base_url="https://example.org/v1", model="m", api_key="fake", http_attempts_per_turn=cap
    )
    gateway = LlmGateway(
        settings=settings,
        client_factory=lambda s: ChatCompletionsClient(
            base_url=s.base_url,
            api_key=s.api_key,
            model=s.model,
            timeout_s=1,
            max_output_tokens=10,
            retries=1,
            sleep=lambda _seconds: None,
            transport=httpx.MockTransport(handler),
        ),
    )
    events: list[dict[str, Any]] = []
    gateway.set_call_limit(4)
    gateway.start_turn(request_id="r-cap", session_id="s-cap", trip_id="t-cap", attempt_sink=events.append)
    return gateway, events


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _call(gateway: LlmGateway, role: str) -> Any:
    return gateway.complete_structured(
        role=role, text="из Москвы в Казань", reference_date="2026-10-27", schema_version="1.0.0"
    )


def test_compat_and_schema_repair_share_one_turn_cap() -> None:
    """HTTP 400 → совместимый повтор → невалидный JSON → repair: все POST под одним cap=3."""
    sent: list[str] = []
    responses = [httpx.Response(400), _ok("not json at all"), _ok(INTERPRETER_OK), _ok(INTERPRETER_OK)]
    gateway, events = _gateway(responses, cap=3, sent=sent)
    result = _call(gateway, "interpreter")
    assert result.status == "ok", result
    assert len(sent) == 3 and gateway.budget.http_used == 3
    started = [e for e in events if e["phase"] == "started"]
    assert len(started) == 3 and len({e["attempt_id"] for e in started}) == 3
    assert len({e["logical_call_id"] for e in started}) == 2, "repair — новый логический вызов"


def test_second_role_is_stopped_by_exhausted_turn_cap() -> None:
    """Интерпретатор израсходовал cap — второй роли POST не отправляется, ход деградирует безопасно."""
    sent: list[str] = []
    responses = [httpx.Response(400), _ok(INTERPRETER_OK)]
    gateway, events = _gateway(responses, cap=2, sent=sent)
    assert _call(gateway, "interpreter").status == "ok"
    second = _call(gateway, "composer")
    assert second.status != "ok"
    assert len(sent) == 2 and gateway.budget.http_used == 2
    assert len([e for e in events if e["phase"] == "started"]) == 2


def test_new_turn_resets_cap_but_not_mid_turn() -> None:
    sent: list[str] = []
    gateway, _events = _gateway([_ok(INTERPRETER_OK)], cap=1, sent=sent)
    assert _call(gateway, "interpreter").status == "ok"
    assert _call(gateway, "interpreter").status != "ok"
    assert len(sent) == 1
    gateway.start_turn(request_id="r-next", session_id="s-cap", trip_id="t-cap", attempt_sink=lambda e: None)
    assert _call(gateway, "interpreter").status == "ok"
    assert len(sent) == 2
