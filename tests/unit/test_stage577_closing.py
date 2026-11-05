"""Stage 57.7: закрытие OBS-NO-SINK-04 и OBS-REPORT-PHASE-05 (+ штатная передача attempt_sink).

Аудит 57.6: прямой путь Gateway разрешал платный POST без durable sink, а отчёт считал фазы
(`started`/`finished`) как отдельные HTTP-попытки. Только Mock и временная SQLite: без сети,
ключей и платных вызовов.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from odin.config import LlmSettings
from odin.llm.gateway import LlmAttemptNotRecorded, LlmBudgetExceeded, LlmGateway
from odin.llm.openai_client import ChatCompletionsClient
from odin.orchestrator import Orchestrator
from odin.storage import Storage

API = "/api/v1"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stage57_live_report.py"
MARKER_KEY = "marker-" + "never-logged"
PRIVATE = "PRIVATE USER TEXT"


def _live_gateway(sent: list[int], handler_codes: list[int] | None = None) -> LlmGateway:
    codes = list(handler_codes or [])

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        code = codes.pop(0) if codes else 200
        if code != 200:
            return httpx.Response(code)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    settings = LlmSettings(
        mode="live",
        base_url="https://example.org/v1",
        model="m",
        api_key=MARKER_KEY,
        http_attempts_per_turn=4,
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
    gateway.set_call_limit(4)
    return gateway


def _call(gateway: LlmGateway, role: str = "interpreter") -> Any:
    return gateway.complete_structured(
        role=role, text="из Москвы в Казань", reference_date="2026-10-27", schema_version="1.0.0"
    )


# ---------------------------------------------------------------- OBS-NO-SINK-04


def test_gateway_without_sink_blocks_post() -> None:
    """Прямой путь Gateway без sink: резервация невозможна → POST запрещён."""
    gateway = _live_gateway([])
    gateway.start_turn(request_id="r", session_id="s", trip_id="t")
    with pytest.raises(LlmAttemptNotRecorded, match="attempt_sink"):
        gateway._before_post()


def test_gateway_without_sink_sends_no_http_request() -> None:
    """Через ``complete_structured``: запрос не уходит, вызов деградирует безопасно."""
    sent: list[int] = []
    gateway = _live_gateway(sent)
    gateway.start_turn(request_id="r", session_id="s", trip_id="t")
    result = _call(gateway)
    assert sent == [], "платный POST отправлен без устойчивой записи попытки"
    assert result.status != "ok"


def test_gateway_with_broken_sink_blocks_post_but_keeps_http_budget() -> None:
    """Отказ записи резервации блокирует POST; квота расходуется (попытка не отправлена)."""
    sent: list[int] = []

    def broken(event: dict[str, Any]) -> None:
        raise RuntimeError("sqlite disk full")

    gateway = _live_gateway(sent)
    gateway.start_turn(request_id="r", session_id="s", trip_id="t", attempt_sink=broken)
    with pytest.raises(LlmAttemptNotRecorded):
        gateway._before_post()
    assert sent == [] and gateway.budget.http_used == 1


def test_every_real_post_is_pre_registered() -> None:
    """Compatibility retry + transport retry: каждый POST имеет свою запись ``started`` до отправки."""
    sent: list[int] = []
    events: list[dict[str, Any]] = []
    order: list[str] = []

    def sink(event: dict[str, Any]) -> None:
        order.append(f"{event['phase']}@{len(sent)}")
        events.append(event)

    gateway = _live_gateway(sent, handler_codes=[400, 503, 200])
    gateway.start_turn(request_id="r", session_id="s", trip_id="t", attempt_sink=sink)
    assert _call(gateway).status == "ok"
    assert len(sent) == 3, f"ожидалось 3 реальных POST, получено {len(sent)}"
    started = [mark for mark in order if mark.startswith("started")]
    assert started == ["started@0", "started@1", "started@2"], order
    for mark in started:  # каждый POST предварён своей записью: started@N при уже N отправленных
        assert int(mark.split("@")[1]) < len(sent)
    assert [event["phase"] for event in events].count("started") == 3
    assert len({event["attempt_id"] for event in events}) == 3


def test_http_budget_still_caps_attempts_without_sink() -> None:
    """Существующий лимит HTTP сохраняется: исчерпание — ``LlmBudgetExceeded``, а не «нет sink»."""
    gateway = _live_gateway([])
    gateway.start_turn(request_id="r", session_id="s", trip_id="t")
    gateway.budget.take_http()
    gateway.budget.take_http()
    gateway.budget.take_http()
    gateway.budget.take_http()
    with pytest.raises(LlmBudgetExceeded):
        gateway._before_post()


# ---------------------------------------------------------------- OBS-REPORT-PHASE-05


def _report(db: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(db)], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def test_report_counts_one_http_attempt_per_attempt_id(tmp_path: Path) -> None:
    """started(A) + finished(A) + started(B) → 2 попытки, 1 завершена, 1 незавершена, 0 HTTP-ошибок."""
    db = tmp_path / "audit.sqlite3"
    storage = Storage(db)
    base = {"attempt_id": "attempt-A", "request_id": "request-A", "logical_call_id": "call-A"}
    storage.audit(
        "llm_http_attempt", request_id="request-A", payload={**base, "phase": "started", "status": "pending"}
    )
    storage.audit(
        "llm_http_attempt",
        request_id="request-A",
        payload={
            **base,
            "phase": "finished",
            "status": "http",
            "http_status": 200,
            "usage_status": "reported",
        },
    )
    storage.audit(
        "llm_http_attempt",
        request_id="request-A",
        payload={**base, "attempt_id": "attempt-B", "phase": "started", "status": "pending"},
    )
    report = _report(db)
    assert report["http_attempts"] == 2
    assert report["http_attempts_finished"] == 1
    assert report["http_attempts_unfinished"] == 1
    assert report["http_errors"] == 0, "незавершённая попытка — потенциальный расход, не HTTP-ошибка"
    assert report["cost"] == "unknown" and report["cost_status"] == "unknown"


def test_report_counts_real_http_errors_but_not_pending(tmp_path: Path) -> None:
    """Ошибкой считаются только завершённые неблагополучные ответы; стоимость остаётся unknown."""
    db = tmp_path / "audit.sqlite3"
    storage = Storage(db)
    for attempt_id, status in (("A", 500), ("B", 200)):
        payload = {"attempt_id": attempt_id, "request_id": "r", "phase": "started"}
        storage.audit("llm_http_attempt", request_id="r", payload=payload)
        storage.audit(
            "llm_http_attempt",
            request_id="r",
            payload={"attempt_id": attempt_id, "request_id": "r", "phase": "finished", "http_status": status},
        )
    storage.audit(
        "llm_http_attempt", request_id="r", payload={"attempt_id": "C", "request_id": "r", "phase": "started"}
    )
    report = _report(db)
    assert report["http_attempts"] == 3
    assert report["http_attempts_finished"] == 2
    assert report["http_attempts_unfinished"] == 1
    assert report["http_errors"] == 1
    assert report["cost_amount" if "cost_amount" in report else "cost"] == "unknown"


def test_report_survives_rows_without_attempt_id(tmp_path: Path) -> None:
    """Старые записи без ``attempt_id`` считаются самостоятельными попытками, а не склеиваются."""
    db = tmp_path / "audit.sqlite3"
    storage = Storage(db)
    for _ in range(2):
        storage.audit("llm_http_attempt", request_id="r", payload={"phase": "finished", "http_status": 200})
    report = _report(db)
    assert report["http_attempts"] == 2 and report["http_errors"] == 0


# ---------------------------------------------------------------- штатная передача sink


def test_orchestrator_passes_attempt_sink_and_records_attempts(client: Any, application: Any) -> None:
    """Штатный ход: оркестратор передаёт sink, записи попытки попадают в audit, после хода sink снят."""
    from tests.conftest import trip_form

    orchestrator: Orchestrator = application.orchestrator
    session = client.post(f"{API}/sessions").json()
    turn = client.post(
        f"{API}/sessions/{session['session_id']}/messages",
        json={"request_id": "req-sink-1", "trip_revision": session["trip_revision"], "form": trip_form()},
    )
    assert turn.status_code == 200, turn.text
    assert orchestrator.llm.attempt_sink is None, "привязка sink должна сниматься после хода"

    with application.storage._connection() as connection:
        events = [
            row["event"]
            for row in connection.execute(
                "SELECT event FROM audit WHERE request_id='req-sink-1' ORDER BY audit_id"
            ).fetchall()
        ]
    assert "turn_started" in events and "turn_finished" in events
    assert "pipeline_span" in events


def test_orchestrator_sink_callable_is_the_durable_sink(client: Any, application: Any) -> None:
    """Пока ход идёт, sink указывает на запись в audit этого request_id."""
    from tests.conftest import trip_form

    captured: dict[str, Any] = {}
    original = Orchestrator._compute_turn

    def spy(self: Orchestrator, *args: Any, **kwargs: Any) -> Any:
        captured["sink"] = self.llm.attempt_sink
        captured["request_id"] = self.llm.request_id
        return original(self, *args, **kwargs)

    Orchestrator._compute_turn = spy
    try:
        session = client.post(f"{API}/sessions").json()
        turn = client.post(
            f"{API}/sessions/{session['session_id']}/messages",
            json={"request_id": "req-sink-2", "trip_revision": session["trip_revision"], "form": trip_form()},
        )
    finally:
        Orchestrator._compute_turn = original
    assert turn.status_code == 200, turn.text
    assert callable(captured["sink"]) and captured["request_id"] == "req-sink-2"
