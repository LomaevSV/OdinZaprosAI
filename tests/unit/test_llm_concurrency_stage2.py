"""LLM-CONCURRENCY-01 / LLM-COST-02: состояние хода и учёт попыток LLM (только mock, без сети и ключей).

``LlmGateway`` (бюджет и журнал вызовов), ``ToolRegistry.call`` (бюджет инструментов, дедлайн) и уведомления
хода хранятся в одном экземпляре на приложение. Синхронные обработчики FastAPI выполняются в пуле потоков,
а ``session_lock`` разделяет только ходы одной сессии: без общей блокировки ход другой сессии сбрасывал бы
бюджет посреди чужого хода (лимит вызовов обходится). Платные вызовы, ключи и сеть не используются.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

from odin.config import Settings
from odin.contracts.api import StructuredForm
from odin.llm.gateway import LlmGateway
from odin.llm.openai_client import ChatCompletionsClient
from odin.orchestrator import Orchestrator
from tests.conftest import trip_form

API = "/api/v1"


def _session(client: Any) -> dict[str, Any]:
    return client.post(f"{API}/sessions").json()


def test_turns_of_different_sessions_do_not_overlap(
    client: Any, application: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ход второй сессии не начинается (не сбрасывает общий бюджет), пока идёт ход первой."""
    orchestrator = application.orchestrator
    first_inside = threading.Event()
    release_first = threading.Event()
    started: list[str] = []
    original_start = LlmGateway.start_turn
    original_compute = Orchestrator._compute_turn

    def start_turn(self: LlmGateway) -> None:
        started.append(threading.current_thread().name)
        original_start(self)

    def compute(self: Orchestrator, *args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name == "first":
            first_inside.set()
            assert release_first.wait(timeout=10)
        return original_compute(self, *args, **kwargs)

    monkeypatch.setattr(LlmGateway, "start_turn", start_turn)
    monkeypatch.setattr(Orchestrator, "_compute_turn", compute)
    sessions = [_session(client), _session(client)]
    statuses: dict[str, str] = {}

    def run(name: str, session: dict[str, Any]) -> None:
        result = orchestrator.handle_message(
            session_id=session["session_id"],
            request_id=f"req-conc-{name}",
            trip_revision=session["trip_revision"],
            form=StructuredForm.model_validate(trip_form()),
        )
        statuses[name] = result.state.status

    first = threading.Thread(target=run, args=("first", sessions[0]), name="first")
    first.start()
    assert first_inside.wait(timeout=10)
    second = threading.Thread(target=run, args=("second", sessions[1]), name="second")
    second.start()
    second.join(timeout=0.5)
    try:
        assert started == ["first"], f"ход второй сессии начался посреди первого: {started}"
    finally:
        release_first.set()
        first.join(timeout=30)
        second.join(timeout=30)
    assert statuses == {"first": "planned", "second": "planned"}
    assert started == ["first", "second"]


def test_test_configuration_never_enables_paid_llm(settings: Settings) -> None:
    """Контроль безопасности: тестовая конфигурация не включает live и платные вызовы."""
    assert settings.llm.mode == "off"
    assert settings.llm.live_enabled is False


@pytest.mark.parametrize(
    ("codes", "expected_attempts"),
    [
        ([500, 500, 500, 500], 2),  # retries=1: исходный запрос + 1 повтор
        ([400, 500, 500, 500], 3),  # + один совместимый повтор после HTTP 400
        ([401], 1),  # авторизация — без повторов
    ],
)
def test_http_attempts_per_logical_call_are_bounded(codes: list[int], expected_attempts: int) -> None:
    """LLM-COST-02: число реальных HTTP-попыток одного логического вызова ограничено и учитывается."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = codes[min(len(seen), len(codes) - 1)]
        seen.append(code)
        return httpx.Response(code, json={"error": "mock"})

    client = ChatCompletionsClient(
        base_url="https://llm.invalid/v1",
        api_key="fake",
        model="test-model",
        timeout_s=1,
        max_output_tokens=10,
        retries=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )
    try:
        result = client.complete([{"role": "user", "content": "x"}])
    finally:
        client.close()
    assert len(seen) == expected_attempts
    assert result.attempts == expected_attempts
    assert result.status != "ok"
