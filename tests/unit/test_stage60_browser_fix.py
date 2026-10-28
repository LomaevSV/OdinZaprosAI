"""Этап 60: CITY-002 (смена направления), DATE-004 («послезавтра»), DATE-005 («в середине лета»).

Без сети и без платных LLM: дельта Интерпретатора задаётся в тесте, время фиксировано. Сквозные проверки через API
— со сценарным LLM (tests/llm_fakes.py) и данными Mock.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from odin.contracts.api import StructuredForm
from odin.core.calendar import literal_date_phrase, resolve_trip_dates
from odin.llm.free_text import (
    DATES_END_PENDING_ID,
    DATES_PERIOD_PENDING_ID,
    DATES_TZ_PENDING_ID,
    InterpretationDelta,
    outcome_from_delta,
)
from odin.llm.interpreter import InterpretationOutcome, request_to_form

pytestmark = pytest.mark.req("CITY-002", "DATE-004", "DATE-005")

# Среда 28.10.2026 20:30 UTC: в Москве 23:30 28.10, во Владивостоке уже 06:30 29.10.
REF = datetime(2026, 10, 28, 20, 30, tzinfo=UTC)
ORIGIN = {"origin": {"value": "Москва", "span": "из Москвы"}}
TRIP = {
    **ORIGIN,
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
        session_id="s-stage60",
        request_id="req-stage60",
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


def _destination(outcome: InterpretationOutcome) -> str | None:
    assert outcome.request is not None
    return outcome.request.destination.name if outcome.request.destination else None


# ---------------------------------------------------------------- DATE-004: «послезавтра»


def test_day_after_tomorrow_is_two_calendar_days_in_origin_timezone() -> None:
    first = _turn("Из Москвы в Казань послезавтра вдвоём", {**TRIP, "dates": {"date_phrase": "послезавтра"}})
    [question] = [q for q in first.questions if q.ambiguity_id == DATES_END_PENDING_ID]
    assert "30 октября 2026" in question.question and "«Москва»" in question.question
    later = _turn("на 3 ночи", {"dates": {"nights_phrase": "на 3 ночи"}}, first, REF + timedelta(hours=5))
    assert _dates(later) == (date(2026, 10, 30), date(2026, 11, 2)), "считается от исходной опорной даты"
    assert not _ids(later) & {DATES_END_PENDING_ID, DATES_TZ_PENDING_ID}, "отвеченный вопрос не повторяется"

    # 22:30 UTC = 01:30 29.10 в Москве: +2 календарных дня по московской дате, а не по дате UTC/сервера.
    late = datetime(2026, 10, 28, 22, 30, tzinfo=UTC)
    moscow = _turn(
        "Из Москвы в Казань послезавтра на 3 ночи вдвоём",
        {**TRIP, "dates": {"date_phrase": "послезавтра", "nights_phrase": "на 3 ночи"}},
        reference=late,
    )
    assert _dates(moscow) == (date(2026, 10, 31), date(2026, 11, 3))
    vladivostok = _turn(
        "Из Владивостока в Казань послезавтра на 3 ночи вдвоём",
        {
            **TRIP,
            "origin": {"value": "Владивосток", "span": "из Владивостока"},
            "dates": {"date_phrase": "послезавтра", "nights_phrase": "на 3 ночи"},
        },
    )
    assert _dates(vladivostok) == (date(2026, 10, 31), date(2026, 11, 3)), "во Владивостоке уже 29.10"


def test_day_after_tomorrow_with_unknown_origin_asks_only_for_origin_and_keeps_phrase() -> None:
    first = _turn(
        "Послезавтра в Казань вдвоём на 3 ночи",
        {
            "destination": TRIP["destination"],
            "adults": TRIP["adults"],
            "dates": {"date_phrase": "Послезавтра", "nights_phrase": "на 3 ночи"},
        },
    )
    assert _dates(first) is None and DATES_TZ_PENDING_ID in _ids(first)
    assert {q.field for q in first.questions} <= {"date_from", "origin"}, "спрашивается только недостающее"
    answered = _turn("из Москвы", ORIGIN, first, REF + timedelta(hours=1))
    assert _dates(answered) == (date(2026, 10, 30), date(2026, 11, 2))
    assert not answered.questions, "после ответа о городе отправления вопросов о датах нет"


@pytest.mark.parametrize(
    "phrase", ["уехать послезавтра", "вылет послезавтра", "через два дня", "через 2 дня"]
)
@pytest.mark.parametrize(
    "shape",
    ["phrase", "computed", "unparsed", "omitted"],
    ids=["model-phrase", "model-computed-date", "model-unparsed", "model-omitted"],
)
def test_day_after_tomorrow_synonyms_are_understood(phrase: str, shape: str) -> None:
    """Модель может вернуть фразу, вычисленную дату, «неразобранное» или ничего — даты считает календарь."""
    text = f"Из Москвы в Казань {phrase} на 3 ночи вдвоём"
    nights = {"nights_phrase": "на 3 ночи"}
    delta: dict[str, Any] = {
        "phrase": {**TRIP, "dates": {"date_phrase": phrase, **nights}},
        "computed": {**TRIP, "dates": {"date_phrase": "30 октября", **nights}},
        "unparsed": {**TRIP, "dates": nights, "unparsed": [phrase]},
        "omitted": {**TRIP, "dates": nights},
    }[shape]
    outcome = _turn(text, delta)
    assert _dates(outcome) == (date(2026, 10, 30), date(2026, 11, 2))
    assert not outcome.questions
    assert not any("Не удалось однозначно учесть" in note for note in outcome.notes)


def test_tomorrow_today_and_absolute_dates_are_not_broken() -> None:
    tomorrow = _turn(
        "Завтра из Москвы в Казань на 3 ночи вдвоём",
        {**TRIP, "dates": {"date_phrase": "Завтра", "nights_phrase": "на 3 ночи"}},
    )
    assert _dates(tomorrow) == (date(2026, 10, 29), date(2026, 11, 1))
    absolute = _turn(
        "Из Москвы в Казань с 10 по 13 ноября вдвоём", {**TRIP, "dates": {"date_phrase": "с 10 по 13 ноября"}}
    )
    assert _dates(absolute) == (date(2026, 11, 10), date(2026, 11, 13))
    # Слово «сегодня» без фразы даты от модели даты поездки не задаёт; «завтра» при известных датах — тоже.
    today = _turn("Сегодня хочу спланировать: из Москвы в Казань вдвоём", TRIP)
    assert _dates(today) is None and not today.request.date_expressions  # type: ignore[union-attr]
    kept = _turn("завтра напишу про бюджет", {}, absolute)
    assert _dates(kept) == (date(2026, 11, 10), date(2026, 11, 13))


def test_literal_relative_phrases() -> None:
    assert literal_date_phrase("хочу уехать послезавтра утром") == "послезавтра"
    assert literal_date_phrase("через два дня") == "через два дня"
    assert literal_date_phrase("завтра или послезавтра") is None, "две разные фразы — не угадывается"


# ---------------------------------------------------------------- DATE-005: «в середине лета» и близкие периоды


@pytest.mark.parametrize(
    ("phrase", "month", "year", "reading"),
    [
        ("в середине лета", 7, 2027, "середина лета — июль"),
        ("в начале лета", 6, 2027, "начало лета — июнь"),
        ("в конце лета", 8, 2027, "конец лета — август"),
        ("в середине зимы", 1, 2027, "середина зимы — январь"),
        ("ближе к осени", 8, 2027, "конец лета, август"),
        ("в середине ноября", 11, 2026, None),
        ("последние выходные марта", 3, 2027, None),
    ],
)
@pytest.mark.parametrize("zone", ["Europe/Moscow", None])
def test_approximate_periods_offer_windows_inside_period(
    phrase: str, month: int, year: int, reading: str | None, zone: str | None
) -> None:
    nights = None if "выходные" in phrase else "на 5 ночей"
    result = resolve_trip_dates(phrase, nights, REF, zone)
    assert result.status == "period" and result.start is None and result.end is None, "одна дата не выдаётся"
    assert 1 <= len(result.windows) <= 4
    for start, end in result.windows:
        assert (start.year, start.month) == (year, month) or (end.year, end.month) == (year, month)
        if nights:
            assert (end - start).days == 5, "длительность поездки сохраняется"
    assert result.question and "Не удалось" not in result.question
    assert "цены и наличие проверяются после выбора" in result.question and "₽" not in result.question
    if reading:
        assert f"понимаю так: {reading}" in result.question or reading in result.question, result.question


def test_middle_of_summer_without_duration_keeps_period_and_asks_nights() -> None:
    first = _turn(
        "Из Москвы в Казань в середине лета вдвоём", {**TRIP, "dates": {"date_phrase": "в середине лета"}}
    )
    [question] = [q for q in first.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert "Сколько ночей" in question.question and "середина лета — июль" in question.question
    assert "не удалось" not in question.question.lower() and not question.options
    assert not any(note.startswith("даты не указаны") for note in first.notes), "фраза даты понята"
    second = _turn("на неделю", {"dates": {"nights_phrase": "на неделю"}}, first)
    [windows] = [q for q in second.questions if q.ambiguity_id == DATES_PERIOD_PENDING_ID]
    assert 1 <= len(windows.options) <= 4
    assert all(o.date_from.month == 7 and (o.date_to - o.date_from).days == 7 for o in windows.options)
    assert all(o.option_id.startswith("dates-2027") for o in windows.options)


def test_past_season_part_with_year_is_not_guessed() -> None:
    result = resolve_trip_dates("в середине лета 2026", "на 3 ночи", REF, "Europe/Moscow")
    assert result.status == "unresolved" and result.question and "уже прошёл" in result.question


# ---------------------------------------------------------------- CITY-002: смена направления текстом


def test_text_change_of_destination_falls_back_when_model_leaves_city_unparsed() -> None:
    base = _turn(
        "Из Москвы в Калининград с 10 по 13 ноября вдвоём",
        {
            **TRIP,
            "destination": {"value": "Калининград", "span": "в Калининград"},
            "dates": {"date_phrase": "с 10 по 13 ноября"},
        },
    )
    for text, delta in [
        ("Нет, лучше Казань", {}),
        ("Нет, лучше Казань", {"unparsed": ["Нет, лучше Казань"]}),
        ("Нет, лучше Казань", {"destination": {"value": "Казань", "span": "лучше Казань"}}),
        ("не Сочи, а Казань", {}),
    ]:
        outcome = _turn(text, delta, base)
        assert _destination(outcome) == "Казань", (text, delta)
        assert _dates(outcome) == (date(2026, 11, 10), date(2026, 11, 13)), "даты сохранены"
        assert outcome.request is not None and outcome.request.origin is not None
        assert outcome.request.origin.name == "Москва" and outcome.request.party.adults == 2
    for text in ("Калининград или Казань?", "хочу сочный шашлык", "из Казани", "не в Казань"):
        assert _destination(_turn(text, {}, base)) == "Калининград", f"не угадывается: {text}"
    # Город отправления неизвестен: голый город может быть ответом «откуда» — роль не угадывается.
    no_origin = _turn(
        "В Калининград вдвоём", {"destination": {"value": "Калининград", "span": "В Калининград"}}
    )
    assert _destination(_turn("Казань", {}, no_origin)) == "Калининград"


# ---------------------------------------------------------------- через API (сценарный LLM, без сети)


@pytest.fixture
def dialog(tmp_path: Path) -> Iterator[tuple[Any, Any, Any]]:
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.config import Settings
    from tests.llm_fakes import LIVE_ENV, ScriptedLlm

    script = {
        "Хочу на море из Москвы вдвоём": {
            **ORIGIN,
            "adults": {"value": 2, "span": "вдвоём"},
            "wishes_tags": {"value": ["море"], "span": "на море"},
        },
        "Хочу на море из Москвы с 10 по 13 ноября вдвоём, не дороже 90000 рублей": {
            **ORIGIN,
            "adults": {"value": 2, "span": "вдвоём"},
            "wishes_tags": {"value": ["море"], "span": "на море"},
            "dates": {"date_phrase": "с 10 по 13 ноября"},
            "budget": {
                "amount": "90000",
                "currency": "RUB",
                "required": True,
                "span": "не дороже 90000 рублей",
            },
        },
        "послезавтра": {"dates": {"date_phrase": "послезавтра"}},
        "в середине лета": {"dates": {"date_phrase": "в середине лета"}},
        "на неделю": {"dates": {"nights_phrase": "на неделю"}},
        "Нет, лучше Казань": {"unparsed": ["Нет, лучше Казань"]},
        "Из Москвы в Казань с 10 по 13 ноября вдвоём": {
            **TRIP,
            "dates": {"date_phrase": "с 10 по 13 ноября"},
        },
    }
    fake = ScriptedLlm(script)
    settings = Settings.from_env({**LIVE_ENV, "ODIN_DB_PATH": str(tmp_path / "odin.sqlite3")})
    application = build_application(settings, llm_client_factory=lambda _: fake)
    with TestClient(create_app(settings, application=application)) as client:
        yield client, fake, application


class _Chat:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.session = client.post("/api/v1/sessions").json()
        self.revision = self.session["trip_revision"]
        self.n = 0

    def post(self, body: dict[str, Any], revision: int | None = None) -> Any:
        self.n += 1
        return self.client.post(
            f"/api/v1/sessions/{self.session['session_id']}/messages",
            json={
                "request_id": f"req-stage60-{self.n:04d}",
                "trip_revision": revision or self.revision,
                **body,
            },
        )

    def say(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self.post(body)
        assert response.status_code == 200, response.text
        turn = response.json()
        self.revision = turn["trip_revision"]
        return turn

    def city(self, name: str) -> dict[str, Any]:
        return self.say({"form": {"destination": name}})


def _request(turn: dict[str, Any]) -> dict[str, Any]:
    return turn["state"]["request"]


def test_destination_a_b_a_keeps_pending_date_and_does_not_call_llm(dialog: tuple[Any, Any, Any]) -> None:
    client, fake, _app = dialog
    chat = _Chat(client)
    first = chat.say({"text": "Хочу на море из Москвы вдвоём"})
    names = [c["place"]["name"] for c in first["state"]["candidate_destinations"]]
    assert len(names) >= 2 and _request(first)["destination"] is None
    dated = chat.say({"text": "послезавтра"})
    [pending] = [q for q in dated["questions"] if q["ambiguity_id"] == DATES_END_PENDING_ID]
    calls = len(fake.requests)
    seen = []
    for name in (names[0], names[1], names[0]):
        turn = chat.city(name)
        request = _request(turn)
        assert request["destination"]["name"] == name
        assert request["origin"]["name"] == "Москва" and request["party"]["adults"] == 2
        assert request["wishes_tags"] == ["море"]
        [kept] = [q for q in turn["questions"] if q["ambiguity_id"] == DATES_END_PENDING_ID]
        assert kept["question"] == pending["question"], "«послезавтра» не сброшено в общий вопрос о датах"
        assert "На какие даты планируется" not in turn["assistant_text"]
        assert f"Направление: {name}" in turn["assistant_text"]
        assert [c["place"]["name"] for c in turn["state"]["candidate_destinations"]] == names, (
            "список не исчез"
        )
        seen.append(turn["trip_revision"])
    assert seen == sorted(seen) and len(set(seen)) == 3, "каждая смена — новая ревизия"
    assert len(fake.requests) == calls, "выбор нажатием не вызывает LLM"

    text = chat.say({"text": "Нет, лучше Казань"})
    assert _request(text)["destination"]["name"] == "Казань" and _request(text)["origin"]["name"] == "Москва"
    assert any(q["ambiguity_id"] == DATES_END_PENDING_ID for q in text["questions"])

    stale = chat.post({"form": {"destination": names[1]}}, revision=seen[0])
    assert stale.status_code == 409, "выбор по устаревшей ревизии отклоняется явно"


def test_destination_change_after_period_question_keeps_period(dialog: tuple[Any, Any, Any]) -> None:
    client, _fake, _app = dialog
    chat = _Chat(client)
    first = chat.say({"text": "Хочу на море из Москвы вдвоём"})
    names = [c["place"]["name"] for c in first["state"]["candidate_destinations"]]
    chat.say({"text": "в середине лета"})
    week = chat.say({"text": "на неделю"})
    [period] = [q for q in week["questions"] if q["ambiguity_id"] == DATES_PERIOD_PENDING_ID]
    assert period["options"] and all(o["date_from"][5:7] == "07" for o in period["options"])
    changed = chat.city(names[1])
    [again] = [q for q in changed["questions"] if q["ambiguity_id"] == DATES_PERIOD_PENDING_ID]
    assert again["options"] == period["options"], "период и длительность сохранены при смене города"
    assert not changed["state"]["transport_offers"] and not changed["state"]["hotel_offers"]
    assert changed["state"]["plan"] is None, "цены и наличие до выбора дат не выдумываются"
    option = again["options"][0]
    chosen = chat.say(
        {
            "choice": {
                "ambiguity_id": again["ambiguity_id"],
                "option_id": option["option_id"],
                "date_from": option["date_from"],
                "date_to": option["date_to"],
            }
        }
    )
    request = _request(chosen)
    assert request["destination"]["name"] == names[1]
    assert (request["date_range"]["start"], request["date_range"]["end"]) == (
        option["date_from"],
        option["date_to"],
    )
    assert chosen["state"]["plan"]["components"]


def _plan_offers(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    offers = {o["offer_id"]: o for o in state["transport_offers"] + state["hotel_offers"]}
    return {c["role"]: offers[c["offer_id"]] for c in state["plan"]["components"] if c["offer_id"] in offers}


def test_destination_change_after_plan_and_cart_invalidates_old_offers(dialog: tuple[Any, Any, Any]) -> None:
    """Инвариант offer_id → revision → dates → guests → price → availability → cart при смене города."""
    client, fake, _app = dialog
    chat = _Chat(client)
    first = chat.say({"text": "Хочу на море из Москвы с 10 по 13 ноября вдвоём, не дороже 90000 рублей"})
    names = [c["place"]["name"] for c in first["state"]["candidate_destinations"]]
    planned = chat.city(names[0])
    old = _plan_offers(planned["state"])
    assert old and planned["state"]["plan"]["components"]
    cart = client.post(
        f"/api/v1/trips/{planned['trip_id']}/cart",
        json={"request_id": "req-stage60-cart1", "trip_revision": chat.revision},
    )
    assert cart.status_code == 201, cart.text
    cart_body = cart.json()
    chat.revision = cart_body["trip_revision"]
    calls = len(fake.requests)

    switched = chat.city(names[1])
    state = switched["state"]
    request = state["request"]
    assert request["destination"]["name"] == names[1] and request["origin"]["name"] == "Москва"
    assert request["party"]["adults"] == 2 and request["budget"]["amount"] == "90000.00"
    assert request["date_range"] == planned["state"]["request"]["date_range"], "даты сохранены"
    assert state["cart"] is None, "прежняя корзина недействительна"
    new = _plan_offers(state)
    assert new and not set(o["offer_id"] for o in new.values()) & set(o["offer_id"] for o in old.values())
    for role in ("outbound", "return"):
        if role in new:
            assert names[1] in new[role]["title"] and names[0] not in new[role]["title"]
    hotel = new.get("hotel")
    if hotel is not None:
        assert (hotel["check_in"], hotel["check_out"]) == (
            request["date_range"]["start"],
            request["date_range"]["end"],
        )
        assert hotel["occupancy"]["adults"] == 2
    assert len(fake.requests) == calls, "смена нажатием без LLM"

    confirm = client.post(
        f"/api/v1/carts/{cart_body['cart']['cart_id']}/confirm",
        json={
            "request_id": "req-stage60-conf1",
            "trip_revision": cart_body["trip_revision"],
            "confirmation_id": "conf-stage60-0001",
            "purchase_consent": True,
            "risk_acknowledgement": True,
        },
    )
    assert confirm.status_code == 409, "старая корзина не подтверждается после смены города"

    back = chat.city(names[0])
    assert _request(back)["destination"]["name"] == names[0] and back["state"]["plan"]["components"]


def test_destination_only_form_without_candidates_merges_into_trip(dialog: tuple[Any, Any, Any]) -> None:
    """«Изменить направление» при плане без вдохновения: прежде форма с одним полем стирала остальные условия."""
    client, _fake, _app = dialog
    chat = _Chat(client)
    first = chat.say({"text": "Из Москвы в Казань с 10 по 13 ноября вдвоём"})
    assert first["state"]["plan"]["components"] and not first["state"]["candidate_destinations"]
    changed = chat.city("Сочи")
    request = _request(changed)
    assert request["destination"]["name"] == "Сочи" and request["origin"]["name"] == "Москва"
    assert (
        request["date_range"] == first["state"]["request"]["date_range"] and request["party"]["adults"] == 2
    )
    assert changed["state"]["plan"]["components"]
