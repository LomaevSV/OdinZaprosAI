"""T-INT-LIVE-LLM-01: один реальный вызов LLM-Интерпретатора (ACCEPT-01 п. 3, DEC-07).

Запускается ТОЛЬКО явно: это платный вызов. Пример:
    ODIN_RUN_LIVE=1 ODIN_LLM_MODE=live ODIN_LLM_BASE_URL=... ODIN_LLM_MODEL=... ODIN_LLM_API_KEY=... \
        uv run pytest -m live tests/live -q
Не более 2 вызовов модели (исходный + один повтор по формату).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from odin.config import Settings, load_env_file
from odin.contracts.api import StructuredForm
from odin.llm.free_text import INTERPRETER_SCHEMA_VERSION, outcome_from_delta, parse_delta
from odin.llm.gateway import LlmGateway

pytestmark = [
    pytest.mark.live,
    pytest.mark.req("T-INT-LIVE-LLM-01", "ACCEPT-01"),
    pytest.mark.skipif(os.environ.get("ODIN_RUN_LIVE") != "1", reason="Live LLM только при ODIN_RUN_LIVE=1"),
]

PHRASE = "Хочу из Москвы в Казань с 20 по 22 ноября вдвоём, не самолётом, бюджет до 60 000 рублей"


def test_live_interpreter_extracts_verifiable_fields() -> None:
    load_env_file(".env")
    settings = Settings.from_env()
    if not settings.llm.live_enabled:
        pytest.skip("ODIN_LLM_MODE=live с ключом, адресом и моделью не настроен")
    gateway = LlmGateway(settings=settings.llm)
    gateway.set_call_limit(2)
    reference = datetime.now(tz=UTC)
    result = gateway.complete_structured(
        role="interpreter",
        text=PHRASE,
        reference_date=reference.date().isoformat(),
        schema_version=INTERPRETER_SCHEMA_VERSION,
    )
    assert result.status == "ok", result.uncertainties
    delta = parse_delta(result.structured_output)
    assert delta is not None
    outcome = outcome_from_delta(
        delta,
        text=PHRASE,
        base=StructuredForm(),
        session_id="s-live",
        request_id="r-live-0001",
        reference_datetime=reference,
        mode="free_text_llm",
    )
    request = outcome.request
    assert request is not None
    assert request.origin is not None and request.origin.name == "Москва"
    assert request.destination is not None and request.destination.name == "Казань"
    assert request.party.adults == 2
    assert request.date_range is not None and request.date_range.start.day == 20
    fields = {constraint.field: constraint for constraint in request.constraints}
    assert fields["transport_mode"].value == ["rail"]
    assert fields["budget_total"].value == "60000.00"
    for call in gateway.calls:
        print("llm_call", call.as_dict())  # без текстов: роль, статус, задержка, токены
