"""Этап 59: DATE-002 («завтра»), DATE-003 (периоды), REPLAN-001 («останемся на четыре»), INT-006/007.

Без сети и без LLM: дельта Интерпретатора задаётся в тесте, время фиксировано. Сквозные проверки через API —
со сценарным LLM (tests/llm_fakes.py), платных вызовов нет.
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
from odin.llm.free_text import (
    DATES_END_PENDING_ID,
    DATES_PERIOD_PENDING_ID,
    DATES_TZ_PENDING_ID,
    InterpretationDelta,
    outcome_from_delta,
)
from odin.llm.interpreter import InterpretationOutcome, request_to_form

pytestmark = pytest.mark.req("DATE-002", "DATE-003", "REPLAN-001", "DEC-34")

# Среда 28.10.2026 20:30 UTC: в Москве 23:30 28.10, во Владивостоке уже 06:30 29.10.
REF = datetime(2026, 10, 28, 20, 30, tzinfo=UTC)
TRIP = {
    "origin": {"value": "Москва", "span": "из Москвы"},
    "destination": {"value": "Казань", "span": "в Казань"},
    "adults": {"value": 2, "span": "вдвоём"},
}


def _turn(
    text: str, delta: dict[str, Any], previous: InterpretationOutcome | None = None, reference: datetime = REF
) -> InterpretationOutcome:
    request = previous.request if previous is not None else None
    return outcome_from_delta(
        InterpretationDelta.model_validate(delta),
        text=text,
        base=request_to_form(request) if request is not None else StructuredForm(),
        session_id="s-stage59",
        request_id="req-stage59",
        reference_datetime=reference,
        mode="free_text_llm",
        previous_constraints=list(request.constraints) if request is not None else None,
        previous_ambiguities=list(request.ambiguities) if request is not None else None,
        previous_date_expressions=list(request.date_expressions) if request is not None else None,
    )


def _dates(outcome: InterpretationOutcome) -> tuple[date, date] | None:
    assert outcome.request is not None
    found = outcome.request.date_range
    return (found.start, found.end) if found is not None else None


def _ids(outcome: InterpretationOutcome) -> set[str]:
    return {question.ambiguity_id for question in outcome.questions}


# ---------------------------------------------------------------- DATE-002: «завтра»


def test_tomorrow_without_return_asks_only_for_return_and_is_remembered() -> None:
    """Раньше: «Нужны обе даты поездки» — «завтра» выглядело непонятым и терялось на следующем ходу."""
    first = _turn("Завтра из Москвы в Казань вдвоём", {**TRIP, "dates": {"date_phrase": "Завтра"}})
    assert _dates(first) is None
    [question] = [q for q in first.questions if q.field in ("date_from", "date_to")]
    assert question.ambiguity_id == DATES_END_PENDING_ID
    assert "29 октября 2026" in question.question and "«Москва»" in question.question
    assert "Нужны обе даты" not in question.question

    later = REF + timedelta(hours=5)  # ответ после полуночи: «завтра» считается от исходной опорной даты
    nights = _turn("на 3 ночи", {"dates": {"nights_phrase": "на 3 ночи"}}, first, later)
    assert _dates(nights) == (date(2026, 10, 29), date(2026, 11, 1))
    assert not _ids(nights) & {DATES_END_PENDING_ID, DATES_TZ_PENDING_ID}

    back = _turn("обратно 2 ноября", {"dates": {"date_phrase": "2 ноября"}}, first)
    assert _dates(back) == (date(2026, 10, 29), date(2026, 11, 2)), "одна дата в ответе — возвращение"

    bare = _turn("четыре", {"dates": {"nights_phrase": "четыре"}}, first)
    assert _dates(bare) == (date(2026, 10, 29), date(2026, 11, 2)), "ответ на вопрос о длительности"


def test_tomorrow_from_vladivostok_uses_vladivostok_date() -> None:
    text = "Завтра из Владивостока в Казань вдвоём на 3 ночи"
    delta = {
        **TRIP,
        "origin": {"value": "Владивосток", "span": "из Владивостока"},
        "dates": {"date_phrase": "Завтра", "nights_phrase": "на 3 ночи"},
    }
    outcome = _turn(text, delta)
    local = REF.astimezone(ZoneInfo("Asia/Vladivostok")).date()
    assert _dates(outcome) == (local + timedelta(days=1), local + timedelta(days=4))
    assert outcome.request is not None and outcome.request.date_expressions[-1].timezone == "Asia/Vladivostok"


def test_tomorrow_with_unknown_origin_still_asks_for_origin() -> None:
    outcome = _turn(
        "Завтра в Казань", {"destination": TRIP["destination"], "dates": {"date_phrase": "Завтра"}}
    )
    assert _dates(outcome) is None and DATES_TZ_PENDING_ID in _ids(outcome)


def test_model_computed_date_falls_back_to_literal_tomorrow() -> None:
    """Модель вернула «29 октября» вместо цитаты «завтра»: даты считает календарь по дословной фразе."""
    outcome = _turn(
        "Завтра из Москвы в Казань вдвоём на 3 ночи",
        {**TRIP, "dates": {"date_phrase": "29 октября", "nights_phrase": "на 3 ночи"}},
    )
    assert _dates(outcome) == (date(2026, 10, 29), date(2026, 11, 1))
    assert outcome.request is not None and outcome.request.date_expressions[-1].raw_text == "завтра"


# ---------------------------------------------------------------- DATE-003: периоды


@pytest.mark.parametrize(
    ("phrase", "nights", "first", "count"),
    [
        ("в ноябре", "на 3 ночи", (date(2026, 11, 6), date(2026, 11, 9)), 4),
        ("в конце ноября", "на 3 ночи", (date(2026, 11, 21), date(2026, 11, 24)), 4),
        ("в середине января", "на 4 ночи", (date(2027, 1, 11), date(2027, 1, 15)), 4),
        ("последние выходные марта", None, (date(2027, 3, 27), date(2027, 3, 28)), 2),
        ("летом", "на неделю", (date(2027, 6, 4), date(2027, 6, 11)), 4),
    ],
)
@pytest.mark.parametrize("zone", ["Europe/Moscow", None])
def test_periods_offer_dated_windows(
    phrase: str, nights: str | None, first: tuple[date, date], count: int, zone: str | None
) -> None:
    result = resolve_trip_dates(phrase, nights, REF, zone)
    assert result.status == "period" and result.start is None and result.end is None, "одна дата не выдаётся"
    assert len(result.windows) == count and result.windows[0] == first
    assert result.question and "предложения дат" in result.question and "цены и наличие" in result.question
    assert all(end > start for start, end in result.windows)


def test_period_without_duration_asks_nights_instead_of_assuming() -> None:
    first = _turn("Из Москвы в Казань в ноябре вдвоём", {**TRIP, "dates": {"date_phrase": "в ноябре"}})
    assert _dates(first) is None
    [question] = [q for q in first.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert "Сколько ночей" in question.question and "не удалось" not in question.question.lower()

    second = _turn("на 3 ночи", {"dates": {"nights_phrase": "на 3 ночи"}}, first)
    [windows] = [q for q in second.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert "6–9 ноября 2026" in windows.question and _dates(second) is None

    third = _turn("давай на 4 ночи", {"dates": {"nights_phrase": "на 4 ночи"}}, second)
    [changed] = [q for q in third.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert "6–10 ноября 2026" in changed.question and "«в ноябре»" in changed.question

    chosen = _turn("с 13 по 16 ноября", {"dates": {"date_phrase": "с 13 по 16 ноября"}}, third)
    assert _dates(chosen) == (date(2026, 11, 13), date(2026, 11, 16))
    assert DATES_PERIOD_PENDING_ID not in _ids(chosen)


def test_period_in_existing_trip_keeps_duration_and_does_not_fake_dates() -> None:
    plan = _turn(
        "Из Москвы в Казань с 10 по 13 ноября вдвоём", {**TRIP, "dates": {"date_phrase": "с 10 по 13 ноября"}}
    )
    moved = _turn("перенесём на ноябрь", {"dates": {"date_phrase": "на ноябрь"}}, plan)
    [question] = [q for q in moved.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert "на 3 ночи" in question.question and _dates(moved) is None


def test_past_period_with_explicit_year_is_not_guessed() -> None:
    result = resolve_trip_dates("в ноябре 2025", None, REF, "Europe/Moscow")
    assert result.status == "unresolved" and result.question and "уже прошёл" in result.question


# ---------------------------------------------------------------- REPLAN-001: «останемся на четыре»


def _planned() -> InterpretationOutcome:
    return _turn(
        "Из Москвы в Казань с 10 по 13 ноября вдвоём", {**TRIP, "dates": {"date_phrase": "с 10 по 13 ноября"}}
    )


@pytest.mark.parametrize(
    "delta",
    [{"dates": {"nights_phrase": "на четыре"}}, {"unparsed": ["останемся на четыре"]}],
    ids=["nights_phrase", "unparsed"],
)
def test_stay_for_four_applies_four_nights(delta: dict[str, Any]) -> None:
    outcome = _turn("останемся на четыре", delta, _planned())
    assert _dates(outcome) == (date(2026, 11, 10), date(2026, 11, 14))
    assert not outcome.questions
    assert not any("Не удалось однозначно учесть" in note for note in outcome.notes)


def test_bare_four_without_context_is_asked_once() -> None:
    outcome = _turn("на четыре", {"dates": {"nights_phrase": "на четыре"}}, _planned())
    assert _dates(outcome) == (date(2026, 11, 10), date(2026, 11, 13)), "без контекста «четыре» не ночи"
    assert any("Сколько ночей" in question.question for question in outcome.questions)


def test_new_departure_keeps_existing_duration_with_note() -> None:
    outcome = _turn("давай завтра", {"dates": {"date_phrase": "завтра"}}, _planned())
    assert _dates(outcome) == (date(2026, 10, 29), date(2026, 11, 1))
    assert any("Длительность текущих условий сохранена: 3 ночи" in note for note in outcome.notes)


# ---------------------------------------------------------------- INT-006 / INT-007


@pytest.mark.req("INT-006", "INT-007")
def test_hotel_needed_and_family_room_are_not_reported_as_unparsed() -> None:
    text = "Из Москвы в Казань с 10 по 13 ноября вдвоём. Нужен отель, желательно семейный номер"
    outcome = _turn(
        text,
        {
            **TRIP,
            "dates": {"date_phrase": "с 10 по 13 ноября"},
            "unparsed": ["Нужен отель", "желательно семейный номер"],
        },
    )
    assert not any("Не удалось однозначно учесть" in note for note in outcome.notes)
    [note] = [note for note in outcome.notes if "семейный номер" in note]
    assert "мягкое предпочтение" in note and "не подтверждён" in note and "не отсеиваются" in note


@pytest.mark.req("INT-007")
def test_mandatory_family_room_is_not_silently_accepted() -> None:
    text = "Из Москвы в Казань с 10 по 13 ноября вдвоём, обязательно семейный номер"
    outcome = _turn(
        text,
        {**TRIP, "dates": {"date_phrase": "с 10 по 13 ноября"}, "unparsed": ["обязательно семейный номер"]},
    )
    assert any("Не удалось однозначно учесть" in note and "семейный номер" in note for note in outcome.notes)


# ---------------------------------------------------------------- через API (сценарный LLM, без сети)


@pytest.fixture
def dialog(tmp_path: Path) -> Iterator[tuple[Any, Any]]:
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.config import Settings
    from tests.llm_fakes import LIVE_ENV, ScriptedLlm

    script = {
        "Из Москвы в Казань с 10 по 13 ноября вдвоём": {
            **TRIP,
            "dates": {"date_phrase": "с 10 по 13 ноября"},
        },
        "останемся на четыре": {"dates": {"nights_phrase": "на четыре"}},
        "Из Москвы в Казань в ноябре на 3 ночи вдвоём с ребёнком": {
            **TRIP,
            "dates": {"date_phrase": "в ноябре", "nights_phrase": "на 3 ночи"},
            "children_count": {"value": 1, "span": "с ребёнком"},
        },
        "перенесём на ноябрь": {"dates": {"date_phrase": "на ноябрь"}},
        "Из Москвы в Казань с 10 по 13 ноября вдвоём, желательно семейный номер": {
            **TRIP,
            "dates": {"date_phrase": "с 10 по 13 ноября"},
            "unparsed": ["желательно семейный номер"],
        },
        "лучше с завтраком": {"breakfast": {"value": True, "required": False, "span": "с завтраком"}},
        "Хочу на море из Москвы с 10 по 13 ноября вдвоём": {
            "origin": {"value": "Москва", "span": "из Москвы"},
            "dates": {"date_phrase": "с 10 по 13 ноября"},
            "adults": {"value": 2, "span": "вдвоём"},
            "wishes_tags": {"value": ["море"], "span": "на море"},
        },
    }
    fake = ScriptedLlm(script)
    settings = Settings.from_env({**LIVE_ENV, "ODIN_DB_PATH": str(tmp_path / "odin.sqlite3")})
    application = build_application(settings, llm_client_factory=lambda _: fake)
    with TestClient(create_app(settings, application=application)) as client:
        yield client, fake


def _say(client: Any, session: dict[str, Any], body: dict[str, Any], revision: int, n: int) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={"request_id": f"req-stage59-{n:04d}", "trip_revision": revision, **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_replan_stay_for_four_through_api(dialog: tuple[Any, Any]) -> None:
    client, _fake = dialog
    session = client.post("/api/v1/sessions").json()
    first = _say(
        client, session, {"text": "Из Москвы в Казань с 10 по 13 ноября вдвоём"}, session["trip_revision"], 1
    )
    assert first["status"] in ("planned", "agreed") and first["state"]["plan"]["components"]
    second = _say(client, session, {"text": "останемся на четыре"}, first["trip_revision"], 2)
    request = second["state"]["request"]
    assert request["date_range"]["start"] == first["state"]["request"]["date_range"]["start"]
    assert request["date_range"]["end"].endswith("-14"), request["date_range"]
    assert not second["questions"] and "Сколько ночей" not in second["assistant_text"]
    assert request["origin"]["name"] == "Москва" and request["party"]["adults"] == 2
    hotel = next(c for c in second["state"]["plan"]["components"] if c["role"] == "hotel")
    offer = next(o for o in second["state"]["hotel_offers"] if o["offer_id"] == hotel["offer_id"])
    assert offer["check_out"] == request["date_range"]["end"], "гостиница пересобрана на 4 ночи"


@pytest.mark.req("CITY-001")
def test_destination_choice_keeps_trip_and_does_not_call_llm(dialog: tuple[Any, Any]) -> None:
    client, fake = dialog
    session = client.post("/api/v1/sessions").json()
    first = _say(
        client,
        session,
        {"text": "Хочу на море из Москвы с 10 по 13 ноября вдвоём"},
        session["trip_revision"],
        1,
    )
    candidates = first["state"]["candidate_destinations"]
    assert len(candidates) >= 2 and first["state"]["request"]["destination"] is None
    calls = len(fake.requests)
    name = candidates[0]["place"]["name"]
    chosen = _say(client, session, {"form": {"destination": name}}, first["trip_revision"], 2)
    request = chosen["state"]["request"]
    assert request["destination"]["name"] == name and request["origin"]["name"] == "Москва"
    assert (
        request["party"]["adults"] == 2 and request["date_range"] == first["state"]["request"]["date_range"]
    )
    assert chosen["state"]["plan"]["components"], "после выбора направления собран план"
    assert len(fake.requests) == calls, "выбор нажатием не вызывает LLM"

    other = candidates[1]["place"]["name"]
    switched = _say(client, session, {"form": {"destination": other}}, chosen["trip_revision"], 3)
    assert switched["state"]["request"]["destination"]["name"] == other
    assert switched["state"]["request"]["origin"]["name"] == "Москва", "«откуда» не подменено"

    stale = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={
            "request_id": "req-stage59-0004",
            "trip_revision": first["trip_revision"],
            "form": {"destination": name},
        },
    )
    assert stale.status_code == 409, "повторный клик по устаревшей ревизии отклоняется, а не применяется"


# ---------------------------------------------------------------- DATE-003: варианты дат — кнопки


def test_period_question_carries_clickable_options_with_weekday_preference() -> None:
    outcome = _turn(
        "Из Москвы в Казань в ноябре на 3 ночи с субботы вдвоём",
        {**TRIP, "dates": {"date_phrase": "в ноябре", "nights_phrase": "на 3 ночи"}},
    )
    [question] = [q for q in outcome.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert 1 <= len(question.options) <= 4
    assert all(option.date_from.weekday() == 5 for option in question.options), "отъезд в субботу"
    assert all((option.date_to - option.date_from).days == 3 for option in question.options)
    assert all(
        date(2026, 11, 1) <= option.date_from and option.date_to <= date(2026, 11, 30)
        for option in question.options
    )
    assert question.options[0].option_id == "dates-20261107-20261110"
    assert "с отъездом в субботу" in question.question
    later = _turn("на 4 ночи", {"dates": {"nights_phrase": "на 4 ночи"}}, outcome)
    [again] = [q for q in later.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert again.options and all(
        o.date_from.weekday() == 5 and (o.date_to - o.date_from).days == 4 for o in again.options
    )


def _choice(question: dict[str, Any], index: int = 0) -> dict[str, Any]:
    option = question["options"][index]
    return {
        "choice": {
            "ambiguity_id": question["ambiguity_id"],
            "option_id": option["option_id"],
            "date_from": option["date_from"],
            "date_to": option["date_to"],
        }
    }


def _period_question(turn: dict[str, Any]) -> dict[str, Any]:
    return next(q for q in turn["questions"] if q["ambiguity_id"] == DATES_PERIOD_PENDING_ID)


def test_choosing_dates_keeps_conditions_and_pending_questions_without_llm(dialog: tuple[Any, Any]) -> None:
    client, fake = dialog
    session = client.post("/api/v1/sessions").json()
    first = _say(
        client,
        session,
        {"text": "Из Москвы в Казань в ноябре на 3 ночи вдвоём с ребёнком"},
        session["trip_revision"],
        1,
    )
    question = _period_question(first)
    assert len(question["options"]) == 4 and first["state"]["request"]["date_range"] is None
    calls = len(fake.requests)

    wrong = _choice(question)
    wrong["choice"]["date_to"] = "2026-11-12"
    rejected = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={"request_id": "req-stage59-0101", "trip_revision": first["trip_revision"], **wrong},
    )
    assert rejected.status_code == 409, "подменённые даты варианта не принимаются"

    chosen = _say(client, session, _choice(question, 1), first["trip_revision"], 2)
    assert chosen["input_mode"] == "choice"
    request = chosen["state"]["request"]
    option = question["options"][1]
    assert (
        request["date_range"]["start"] == option["date_from"]
        and request["date_range"]["end"] == option["date_to"]
    )
    assert request["origin"]["name"] == "Москва" and request["destination"]["name"] == "Казань"
    assert request["party"]["adults"] == 2
    ids = {q["ambiguity_id"] for q in chosen["questions"]}
    assert DATES_PERIOD_PENDING_ID not in ids and "amb-llm-children" in ids, (
        "вопрос о возрасте ребёнка не потерян"
    )
    assert len(fake.requests) == calls, "выбор нажатием не вызывает LLM"

    stale = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={
            "request_id": "req-stage59-0103",
            "trip_revision": first["trip_revision"],
            **_choice(question, 2),
        },
    )
    assert stale.status_code == 409, "выбор по устаревшей ревизии отклоняется"


def test_choosing_dates_for_existing_plan_replans(dialog: tuple[Any, Any]) -> None:
    client, fake = dialog
    session = client.post("/api/v1/sessions").json()
    first = _say(
        client, session, {"text": "Из Москвы в Казань с 10 по 13 ноября вдвоём"}, session["trip_revision"], 1
    )
    moved = _say(client, session, {"text": "перенесём на ноябрь"}, first["trip_revision"], 2)
    assert moved["trip_revision"] == first["trip_revision"], "неудачная пересборка не записана (REPLAN-02)"
    question = _period_question(moved)
    option = next(o for o in question["options"] if o["date_from"] != "2026-11-10")
    calls = len(fake.requests)
    index = question["options"].index(option)
    replanned = _say(client, session, _choice(question, index), moved["trip_revision"], 3)
    request = replanned["state"]["request"]
    assert (request["date_range"]["start"], request["date_range"]["end"]) == (
        option["date_from"],
        option["date_to"],
    )
    assert replanned["state"]["plan"]["components"] and request["party"]["adults"] == 2
    assert len(fake.requests) == calls
    again = client.post(
        f"/api/v1/sessions/{session['session_id']}/messages",
        json={
            "request_id": "req-stage59-0204",
            "trip_revision": replanned["trip_revision"],
            **_choice(question, index),
        },
    )
    assert again.status_code == 409, "повторный выбор тех же дат не пересобирает план"


def test_choice_request_is_validated() -> None:
    from pydantic import ValidationError

    from odin.contracts.api import MessageRequest

    choice = {"ambiguity_id": "a", "option_id": "o", "date_from": "2026-11-06", "date_to": "2026-11-09"}
    assert MessageRequest.model_validate({"request_id": "req-12345678", "trip_revision": 1, "choice": choice})
    with pytest.raises(ValidationError):
        MessageRequest.model_validate(
            {"request_id": "req-12345678", "trip_revision": 1, "choice": choice, "text": "x"}
        )
    with pytest.raises(ValidationError):
        MessageRequest.model_validate(
            {"request_id": "req-12345678", "trip_revision": 1, "choice": {**choice, "date_to": "2026-11-06"}}
        )


# ---------------------------------------------------------------- INT-007: пожелание сохраняется между ходами


@pytest.mark.req("INT-007")
def test_family_room_preference_persists_and_is_never_confirmed(dialog: tuple[Any, Any]) -> None:
    client, _fake = dialog
    session = client.post("/api/v1/sessions").json()
    text = "Из Москвы в Казань с 10 по 13 ноября вдвоём, желательно семейный номер"
    first = _say(client, session, {"text": text}, session["trip_revision"], 1)
    second = _say(client, session, {"text": "лучше с завтраком"}, first["trip_revision"], 2)
    for turn in (first, second):
        [constraint] = [c for c in turn["state"]["request"]["constraints"] if c["field"] == "hotel_room_type"]
        assert constraint["requiredness"] == "preferred" and constraint["value"] == "family"
        assert constraint["source_kind"] == "text_span" and "семейный номер" in constraint["source_span"]
        statuses = {
            e["status"]
            for e in turn["state"]["constraint_evaluations"]
            if e["constraint_id"] == constraint["constraint_id"]
        }
        assert statuses and statuses <= {"unknown", "not_applicable"}, (
            "семейный номер не объявлен подтверждённым"
        )
    assert "Не удалось однозначно учесть" not in first["assistant_text"]
    revoked = _turn(
        "семейный номер не нужен",
        {"unparsed": ["семейный номер не нужен"]},
        _turn(
            text,
            {
                **TRIP,
                "dates": {"date_phrase": "с 10 по 13 ноября"},
                "unparsed": ["желательно семейный номер"],
            },
        ),
    )
    assert revoked.request is not None
    assert not [c for c in revoked.request.constraints if c.field == "hotel_room_type"]
