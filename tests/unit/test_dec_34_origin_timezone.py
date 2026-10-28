"""DEC-34: относительные даты считаются по часовому поясу пункта отправления.

Пояс пункта назначения и московский пояс по умолчанию не подставляются. Если пояс отправления неизвестен
и влияет на толкование («завтра»), задаётся вопрос, а фраза ждёт пункт отправления; исправление DATE-001
(«с 1 по 8 марта» без пояса) сохраняется. Без сети и без LLM: дельта интерпретатора задаётся в тесте.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from odin.contracts.api import StructuredForm
from odin.core.calendar import resolve_trip_dates
from odin.llm.free_text import DATES_TZ_PENDING_ID, InterpretationDelta, apply_delta, outcome_from_delta
from odin.llm.interpreter import request_to_form

pytestmark = pytest.mark.req("DEC-34", "INTERPRET-02", "DATE-001")

# 20:30 UTC: в Калининграде 28.10, в Москве 23:30 28.10, в Новосибирске уже 03:30 29.10.
REF = datetime(2026, 10, 28, 20, 30, tzinfo=UTC)


def _apply(text: str, delta: dict[str, Any], **kwargs: Any) -> tuple[StructuredForm, Any]:
    return apply_delta(
        InterpretationDelta.model_validate(delta),
        text=text,
        base=kwargs.pop("base", None) or StructuredForm(),
        reference_datetime=kwargs.pop("reference", REF),
        **kwargs,
    )


def _trip(origin: tuple[str, str] | None, destination: tuple[str, str] | None, phrase: str) -> dict[str, Any]:
    delta: dict[str, Any] = {"dates": {"date_phrase": phrase}}
    if origin:
        delta["origin"] = {"value": origin[0], "span": origin[1]}
    if destination:
        delta["destination"] = {"value": destination[0], "span": destination[1]}
    return delta


def _tz_questions(report: Any) -> list[Any]:
    return [question for question in report.questions if question.ambiguity_id == DATES_TZ_PENDING_ID]


@pytest.mark.parametrize(
    ("text", "origin", "destination", "expected"),
    [
        ("Из Новосибирска в Москву завтра", ("Новосибирск", "Из Новосибирска"), ("Москва", "в Москву"), 30),
        ("Из Москвы в Новосибирск завтра", ("Москва", "Из Москвы"), ("Новосибирск", "в Новосибирск"), 29),
        (
            "Из Калининграда в Новосибирск завтра",
            ("Калининград", "Из Калининграда"),
            ("Новосибирск", "в Новосибирск"),
            29,
        ),
    ],
)
def test_relative_date_uses_origin_timezone_not_destination(
    text: str, origin: tuple[str, str], destination: tuple[str, str], expected: int
) -> None:
    form, report = _apply(text, _trip(origin, destination, "завтра"))
    assert form.date_from == date(2026, 10, expected)
    assert not _tz_questions(report)
    assert report.date_expressions[0].timezone is not None


def test_unknown_origin_timezone_does_not_fall_back_to_destination() -> None:
    """Пермь вне демо-справочника: раньше бралась Казань (Europe/Moscow) — теперь вопрос."""
    text = "Из Перми в Казань завтра"
    form, report = _apply(text, _trip(("Пермь", "Из Перми"), ("Казань", "в Казань"), "завтра"))
    assert form.date_from is None and form.date_to is None
    [question] = _tz_questions(report)
    assert "«Пермь»" in question.question and "завтра" in question.question
    assert question.field == "date_from" and question.source_span == "завтра"
    assert report.date_expressions[0].timezone is None
    assert report.date_expressions[0].status == "unresolved"


def test_missing_origin_asks_instead_of_moscow_default() -> None:
    form, report = _apply("В Казань завтра", _trip(None, ("Казань", "в Казань"), "завтра"))
    assert form.date_from is None
    [question] = _tz_questions(report)
    assert "пункт отправления ещё не назван" in question.question


@pytest.mark.parametrize("phrase", ["с 1 по 8 марта", "с 30 декабря по 5 января"])
def test_date_001_ranges_still_resolve_without_origin_timezone(phrase: str) -> None:
    form, report = _apply(f"Из Перми в Казань {phrase}", _trip(("Пермь", "Из Перми"), ("Казань", "в Казань"), phrase))
    assert form.date_from is not None and form.date_to is not None
    assert not _tz_questions(report)
    assert "dates" in report.accepted


def test_unrecognized_phrase_is_not_blamed_on_timezone() -> None:
    result = resolve_trip_dates("когда-нибудь летом", None, REF, None)
    assert result.status == "unresolved" and not result.needs_timezone
    assert "DEC-34" not in result.rule


def test_live_resolver_is_asked_for_origin_only() -> None:
    asked: list[str] = []

    def resolver(name: str) -> str | None:
        asked.append(name)
        return {"Казань": "Europe/Moscow"}.get(name)

    form, report = _apply(
        "Из Перми в Казань завтра",
        _trip(("Пермь", "Из Перми"), ("Казань", "в Казань"), "завтра"),
        timezone_for=resolver,
    )
    assert asked == ["Пермь"], "пояс назначения не используется вместо пояса отправления"
    assert form.date_from is None and _tz_questions(report)

    asked.clear()
    form, _ = _apply(
        "Из Перми в Казань завтра",
        _trip(("Пермь", "Из Перми"), ("Казань", "в Казань"), "завтра"),
        timezone_for=lambda name: "Asia/Yekaterinburg" if name == "Пермь" else None,
    )
    assert form.date_from == date(2026, 10, 30)  # в Перми (UTC+5) 28.10 20:30 UTC — уже 29.10


def test_pending_phrase_resolves_from_original_reference_once_origin_is_known() -> None:
    """«завтра на 3 ночи» без пункта отправления → вопрос; ответ «из Новосибирска» на следующий день
    разрешает фразу от исходной опорной даты в поясе Новосибирска, длительность не теряется (UC-02)."""
    first_text = "В Казань завтра на 3 ночи"
    first = outcome_from_delta(
        InterpretationDelta.model_validate(
            {
                "destination": {"value": "Казань", "span": "в Казань"},
                "dates": {"date_phrase": "завтра", "nights_phrase": "на 3 ночи"},
            }
        ),
        text=first_text,
        base=StructuredForm(),
        session_id="s-dec34",
        request_id="req-dec34-1",
        reference_datetime=REF,
        mode="free_text_llm",
    )
    assert first.request is not None and first.request.date_range is None
    assert any(question.ambiguity_id == DATES_TZ_PENDING_ID for question in first.questions)

    later = REF + timedelta(days=1)
    second = outcome_from_delta(
        InterpretationDelta.model_validate({"origin": {"value": "Новосибирск", "span": "Из Новосибирска"}}),
        text="Из Новосибирска",
        base=request_to_form(first.request),
        session_id="s-dec34",
        request_id="req-dec34-2",
        reference_datetime=later,
        mode="free_text_llm",
        previous_ambiguities=list(first.request.ambiguities),
        previous_date_expressions=list(first.request.date_expressions),
    )
    assert second.request is not None and second.request.date_range is not None
    assert second.request.date_range.start == date(2026, 10, 30)
    assert second.request.date_range.end == date(2026, 11, 2)
    assert not {question.field for question in second.questions} & {"date_from", "date_to"}
    expression = second.request.date_expressions[-1]
    assert expression.timezone == "Asia/Novosibirsk" and expression.reference_datetime == REF


def test_pending_phrase_is_asked_again_while_origin_timezone_is_unknown() -> None:
    first, report = _apply("В Казань завтра", _trip(None, ("Казань", "в Казань"), "завтра"))
    form, report2 = _apply(
        "Из Перми",
        {"origin": {"value": "Пермь", "span": "Из Перми"}},
        base=first,
        previous_ambiguities=report.questions,
        previous_date_expressions=report.date_expressions,
    )
    assert form.date_from is None
    [question] = _tz_questions(report2)
    assert "«Пермь»" in question.question


def test_explicit_dates_replace_pending_phrase() -> None:
    first, report = _apply("В Казань завтра", _trip(None, ("Казань", "в Казань"), "завтра"))
    form, report2 = _apply(
        "Из Перми с 10 по 13 ноября",
        _trip(("Пермь", "Из Перми"), None, "с 10 по 13 ноября"),
        base=first,
        previous_ambiguities=report.questions,
        previous_date_expressions=report.date_expressions,
    )
    assert (form.date_from, form.date_to) == (date(2026, 11, 10), date(2026, 11, 13))
    assert not _tz_questions(report2)


# ---------------------------------------------------------------- через API (сценарный LLM, без сети)


@pytest.fixture
def live_client(tmp_path: Path) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.config import Settings
    from tests.llm_fakes import LIVE_ENV, ScriptedLlm

    script = {
        "Хочу завтра в Казань вдвоём": {
            "destination": {"value": "Казань", "span": "в Казань"},
            "dates": {"date_phrase": "завтра"},
            "adults": {"value": 2, "span": "вдвоём"},
        },
        "Из Новосибирска": {"origin": {"value": "Новосибирск", "span": "Из Новосибирска"}},
    }
    fake = ScriptedLlm(script)
    settings = Settings.from_env({**LIVE_ENV, "ODIN_DB_PATH": str(tmp_path / "odin.sqlite3")})
    application = build_application(settings, llm_client_factory=lambda _: fake)
    with TestClient(create_app(settings, application=application)) as client:
        yield client


def test_dialog_carries_relative_date_until_origin_is_named(live_client: Any) -> None:
    session = live_client.post("/api/v1/sessions").json()

    def say(text: str, revision: int, n: int) -> dict[str, Any]:
        response = live_client.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            json={"request_id": f"req-dec34-{n:04d}", "trip_revision": revision, "text": text},
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = say("Хочу завтра в Казань вдвоём", session["trip_revision"], 1)
    assert first["status"] == "awaiting_clarification"
    ids = {question["ambiguity_id"] for question in first["questions"]}
    assert DATES_TZ_PENDING_ID in ids
    assert first["state"]["request"]["date_range"] is None
    [expression] = first["state"]["request"]["date_expressions"]
    assert expression["timezone"] is None and expression["status"] == "unresolved"

    second = say("Из Новосибирска", first["trip_revision"], 2)
    reference = datetime.fromisoformat(expression["reference_datetime"])
    expected = reference.astimezone(ZoneInfo("Asia/Novosibirsk")).date() + timedelta(days=1)
    request = second["state"]["request"]
    assert request["origin"]["name"] == "Новосибирск"
    assert DATES_TZ_PENDING_ID not in {question["ambiguity_id"] for question in second["questions"]}
    assert any(
        item["timezone"] == "Asia/Novosibirsk" and item["resolved_start"] == expected.isoformat()
        for item in request["date_expressions"]
    ), request["date_expressions"]
