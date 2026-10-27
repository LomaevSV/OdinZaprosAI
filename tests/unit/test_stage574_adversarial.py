"""Stage 57.4 adversarial checks — corrected expectations after the P1 fixes.

The archived audit file of the same name asserted the *defective* behaviour (audit failure
overrides a successful result; a failing sink after POST). Those expectations are inverted
here: telemetry must never change the business result, and a paid POST is not sent unless
its reservation is durably recorded. Offline only: MockTransport, no keys, no network.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

import httpx
import pytest

from odin.llm.gateway import LlmAttemptNotRecorded, LlmBudget, LlmBudgetExceeded, LlmGateway
from odin.llm.openai_client import ChatCompletionsClient
from odin.orchestrator import _observed_stage

SECRET = "marker-" + "never-logged"  # маркер, не секрет
PRIVATE = "PRIVATE USER TEXT"


class _Storage:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit(self, event: str, **kwargs: Any) -> int:
        if self.exc is not None:
            raise self.exc
        self.events.append((event, kwargs.get("payload") or {}))
        return len(self.events)


class _Orch:
    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self._observed_request_id = "r-1"
        self._observed_trip_id = "t-1"
        self._observed_session_id = "s-1"


# ---------------------------------------------------------------- OBS-SPAN-FAIL-01


def test_audit_failure_does_not_override_successful_result() -> None:
    """Падение SQLite при записи span не превращает успешную операцию в ошибку."""
    wrapped = _observed_stage("dummy", lambda self: "SUCCESS")
    assert wrapped(_Orch(_Storage(RuntimeError("sqlite disk full")))) == "SUCCESS"


def test_audit_failure_does_not_mask_original_exception() -> None:
    """Исходная ошибка операции видна как есть, а не подменяется ошибкой журнала."""

    def boom(self: Any) -> Any:
        raise ValueError("business failure")

    wrapped = _observed_stage("dummy", boom)
    with pytest.raises(ValueError, match="business failure"):
        wrapped(_Orch(_Storage(RuntimeError("sqlite disk full"))))


def test_successful_span_is_recorded() -> None:
    storage = _Storage()
    _observed_stage("stage", lambda self: 42)(_Orch(storage))
    assert [event for event, _ in storage.events] == ["pipeline_span"]
    assert storage.events[0][1]["stage"] == "stage"
    assert storage.events[0][1]["status"] == "ok"


def test_failed_span_is_recorded_with_error_code() -> None:
    storage = _Storage()

    def boom(self: Any) -> Any:
        raise TimeoutError("slow")

    with pytest.raises(TimeoutError):
        _observed_stage("stage", boom)(_Orch(storage))
    assert storage.events[0][1]["status"] == "error"
    assert storage.events[0][1]["error_code"] == "TimeoutError"


# ---------------------------------------------------------------- OBS-ATTEMPT-DURABILITY-02


def _client(before_post: Any, sink: Any, *, sent: list[int], retries: int = 0) -> ChatCompletionsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    return ChatCompletionsClient(
        base_url="https://example.org/v1",
        api_key=SECRET,
        model="test",
        timeout_s=1,
        max_output_tokens=10,
        retries=retries,
        transport=httpx.MockTransport(handler),
        before_post=before_post,
        on_attempt=sink,
    )


def _wired(sink: Any, *, http_limit: int = 2) -> tuple[LlmGateway, Any, Any]:
    """Шлюз с хуками попыток, как в продукте: резервация до POST, итог после."""
    from odin.config import LlmSettings

    settings = LlmSettings(
        mode="live",
        base_url="https://example.org/v1",
        model="m",
        api_key=SECRET,
        http_attempts_per_turn=http_limit,
    )
    gateway = LlmGateway(settings=settings)
    gateway.set_call_limit(4)
    gateway.start_turn(request_id="r-1", session_id="s-1", trip_id="t-1", attempt_sink=sink)
    return gateway, gateway._before_post, gateway._on_attempt


def test_failing_reservation_blocks_billable_post() -> None:
    """Запись резервации не удалась → платный POST не отправляется (fail-closed)."""
    sent: list[int] = []

    def broken_sink(event: dict[str, Any]) -> None:
        raise RuntimeError("sqlite disk full")

    _gateway, before_post, on_attempt = _wired(broken_sink)
    client = _client(before_post, on_attempt, sent=sent)
    with pytest.raises(LlmAttemptNotRecorded):
        client.complete([{"role": "user", "content": PRIVATE}])
    assert sent == [], "платный HTTP-запрос не должен уходить без устойчивой записи"


def test_outcome_sink_failure_keeps_result_and_reservation() -> None:
    """После ответа отказ записи итога не меняет результат; резервация остаётся в журнале."""
    sent: list[int] = []
    recorded: list[dict[str, Any]] = []

    def flaky_sink(event: dict[str, Any]) -> None:
        if event.get("phase") == "finished":
            raise RuntimeError("sqlite disk full")
        recorded.append(event)

    gateway, before_post, on_attempt = _wired(flaky_sink)
    result = _client(before_post, on_attempt, sent=sent).complete([{"role": "user", "content": PRIVATE}])
    assert result.status == "ok" and sent == [1]
    assert gateway.budget.http_used == 1
    assert [event["phase"] for event in recorded] == ["started"]


def test_reservation_precedes_post_and_survives_restart(tmp_path: Any) -> None:
    """`started` записывается до POST и остаётся читаемым после перезапуска процесса."""
    import json

    from odin.storage import Storage

    db = tmp_path / "stage57.sqlite3"
    storage = Storage(db)
    sent: list[int] = []
    order: list[str] = []

    def sink(event: dict[str, Any]) -> None:
        order.append(f"{event['phase']}@{len(sent)}")
        storage.audit("llm_http_attempt", request_id="r-1", payload=event)

    _gateway, before_post, on_attempt = _wired(sink)
    _client(before_post, on_attempt, sent=sent).complete([{"role": "user", "content": PRIVATE}])
    assert order == ["started@0", "finished@1"], "резервация записана до отправки POST"

    reopened = sqlite3.connect(db)
    try:
        rows = reopened.execute("SELECT payload_json FROM audit WHERE event='llm_http_attempt'").fetchall()
    finally:
        reopened.close()
    payloads = [json.loads(row[0]) for row in rows]
    assert [p["phase"] for p in payloads] == ["started", "finished"]
    assert payloads[0]["attempt_id"] == payloads[1]["attempt_id"]
    joined = "".join(row[0] for row in rows)
    assert SECRET not in joined and PRIVATE not in joined


def test_gateway_records_started_and_finished_with_one_attempt_id() -> None:
    """Через реальный ``complete_structured``: одна пара записей на один HTTP POST, общий request_id."""
    from odin.config import LlmSettings

    events: list[dict[str, Any]] = []
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    settings = LlmSettings(mode="live", base_url="https://example.org/v1", model="m", api_key=SECRET)
    gateway = LlmGateway(
        settings=settings,
        client_factory=lambda s: ChatCompletionsClient(
            base_url=s.base_url,
            api_key=s.api_key,
            model=s.model,
            timeout_s=1,
            max_output_tokens=10,
            retries=0,
            transport=httpx.MockTransport(handler),
        ),
    )
    gateway.set_call_limit(4)
    gateway.start_turn(request_id="r-9", session_id="s-9", trip_id="t-9", attempt_sink=events.append)
    result = gateway.complete_structured(
        role="interpreter", text="из Москвы в Казань", reference_date="2026-10-27", schema_version="1.0.0"
    )
    assert result.status == "ok", result
    assert len(sent) == 1
    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[0]["attempt_id"] == events[1]["attempt_id"]
    assert all(event["request_id"] == "r-9" for event in events)
    assert gateway.budget.http_used == 1


# ---------------------------------------------------------------- бюджет


def test_thread_safe_budget_under_contention() -> None:
    budget = LlmBudget(http_limit=3)
    passed: list[int] = []

    def worker() -> None:
        try:
            budget.take_http()
            passed.append(1)
        except LlmBudgetExceeded:
            pass

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(passed) == 3 and budget.http_used == 3
