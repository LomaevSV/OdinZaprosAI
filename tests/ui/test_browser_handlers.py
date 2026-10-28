"""UI-001 / CART-UI-001: реальные обработчики app.js против реального API (Mock, loopback).

Без браузера и без тяжёлых зависимостей: ``tests/ui/app_harness.js`` подставляет минимальный DOM
и выполняет настоящий ``src/odin/static/app.js``; запросы идут в FastAPI на 127.0.0.1 с временной БД.
Тест пропускается, если Node.js недоступен. Сеть наружу, LLM и платные вызовы не используются.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from odin.api import create_app
from odin.app import build_application
from odin.config import Settings
from odin.llm.openai_client import ChatResult
from tests.conftest import TRIP_END, TRIP_START
from tests.llm_fakes import ScriptedLlm, ru_date

HARNESS = Path(__file__).with_name("app_harness.js")
NODE = shutil.which("node")

pytestmark = [
    pytest.mark.skipif(NODE is None, reason="Node.js недоступен: UI-стенд не запускается"),
    pytest.mark.req("UI-01", "UI-001", "CART-UI-001"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(tmp_path: Path, env: dict[str, str], llm: Any = None) -> Iterator[str]:
    import uvicorn

    settings = Settings.from_env(
        {"ODIN_DB_PATH": str(tmp_path / "ui.sqlite3"), "ODIN_DATA_MODE": "mock", **env}
    )
    port = _free_port()
    factory = (lambda _: llm) if llm is not None else None
    application = build_application(settings, llm_client_factory=factory)
    config = uvicorn.Config(create_app(application=application), host="127.0.0.1", port=port, log_level="warning")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not instance.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn не запустился")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        instance.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, {"ODIN_LLM_MODE": "off"})


def _run(base: str, mode: str = "full", **extra: str) -> dict[str, Any]:
    completed = subprocess.run(
        [NODE, str(HARNESS)],  # type: ignore[list-item]
        env={"ODIN_BASE": base, "PATH": "", "HARNESS_MODE": mode, **extra},
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert lines, f"стенд не вернул результат: {completed.stderr[-2000:]}"
    result = json.loads(lines[-1])
    assert "error" not in result, result.get("error")
    return result


@pytest.fixture
def observed(server: str) -> dict[str, Any]:
    return _run(server)


def test_cart_button_without_plan_explains_instead_of_silence(observed: dict[str, Any]) -> None:
    assert observed["cart_without_plan_requests"] == 0
    assert "Корзину пока нельзя сформировать" in observed["cart_without_plan_message"]
    assert "план поездки ещё не собран" in observed["cart_hint_without_plan"]
    assert observed["cart_hint_with_plan"] == ""


def test_offer_cards_are_selectable_for_transport_and_hotel(observed: dict[str, Any]) -> None:
    assert observed["selectable_count"] > 0, "у карточек нет обработчиков выбора (UI-001)"
    assert observed["roles"] == ["hotel", "outbound", "return"]
    for role in ("hotel", "outbound", "return"):
        assert len(observed["initial"][role]) == 1, f"ровно один выбранный вариант: {role}"


def test_selecting_hotel_updates_plan_and_keeps_transport(observed: dict[str, Any]) -> None:
    after = observed["after_hotel"]
    assert after["hotel"] == [observed["alt_hotel"]]
    assert after["outbound"] == observed["initial"]["outbound"], "выбор гостиницы не меняет транспорт"
    assert after["return"] == observed["initial"]["return"]
    assert observed["summary_changed"], "итог/панель поездки перерисованы"
    assert "Итог пересчитан" in observed["selection_message"]


def test_selecting_transport_keeps_hotel(observed: dict[str, Any]) -> None:
    if observed["alt_outbound"] is None:
        pytest.skip("в ДЕМО-выдаче нет альтернативного транспорта туда")
    after = observed["after_outbound"]
    assert after["outbound"] == [observed["alt_outbound"]]
    assert after["hotel"] == [observed["alt_hotel"]], "выбор транспорта не меняет гостиницу"
    assert after["return"] == observed["initial"]["return"]


def test_reselecting_same_offer_sends_no_request(observed: dict[str, Any]) -> None:
    assert observed["reselect_requests"] == 0


def test_cart_is_built_from_current_selection_and_shown(observed: dict[str, Any]) -> None:
    assert observed["cart_posts"] == 1
    assert "Корзина" in observed["cart_text"]
    assert observed["cart_scrolled"] >= 1, "корзина прокручивается в поле зрения"
    assert "Корзина сформирована" in observed["cart_message"]
    assert observed["cart_button_restored"] == "Сформировать корзину"
    assert observed["cart_hidden_after_reselect"] is True, "после нового выбора старая корзина скрыта"
    selections = [path for path in observed["requests"] if path.endswith("/selections")]
    assert len(selections) >= 2


# ---------------------------------------------------------------- P2: UI-002 / UI-004 / UI-003


def test_three_offer_columns_and_selection_bar(observed: dict[str, Any]) -> None:
    assert observed["columns"] == ["column-outbound", "column-hotel", "column-return"]
    assert observed["selection_bar_hidden"] is False
    bar = observed["selection_bar"]
    assert "Туда:" in bar and "Гостиница:" in bar and "Обратно:" in bar and "Итого:" in bar


@pytest.mark.req("UI-004", "UI-005")
def test_single_mock_notice_replaces_duplicate_banner(observed: dict[str, Any]) -> None:
    assert observed["mock_banner_present"] is False, "второй крупный баннер ДЕМО убран (UI-005)"
    assert "синтетические" in observed["data_notice"] and "ДЕМО" in observed["data_notice"], (
        "Mock обозначен одним общим уведомлением"
    )
    assert observed["demo_card_availability_lines"] == 0, (
        "без повтора «не подтверждено (ДЕМО)» на каждой карточке"
    )


@pytest.mark.req("UI-007", "CART-UI-003")
def test_column_headings_and_separate_transfers(observed: dict[str, Any]) -> None:
    titles = observed["column_titles"]
    assert [title.split(" (")[0] for title in titles[:3]] == ["Транспорт туда", "Гостиницы", "Транспорт обратно"]
    assert all(title.endswith(")") for title in titles[:3]), "у заголовка колонки — число вариантов"
    assert observed["column_labelled"] == ["column-title-outbound", "column-title-hotel", "column-title-return"]
    assert observed["transfer_section"] and observed["transfer_section"][0].startswith("Трансферы")
    assert observed["transfers_inside_columns"] is False, "трансфер не внутри колонки «Транспорт обратно»"


@pytest.mark.req("CART-UI-002", "CART-UI-003")
def test_cart_follows_trip_order_with_separate_transfers(observed: dict[str, Any]) -> None:
    order = ["outbound", "transfer_in", "hotel", "transfer_out", "return"]
    sections = observed["cart_sections"]
    assert sections == [role for role in order if role in sections], sections
    assert {"outbound", "hotel", "return"} <= set(sections)
    for role, roles in observed["cart_items_by_section"].items():
        assert set(roles) == {role}, (role, roles)
    if "transfer_in" in sections:
        assert "Трансфер туда: к гостинице" in observed["cart_section_titles"]


@pytest.mark.req("UI-006")
def test_new_manual_form_has_no_demo_route(observed: dict[str, Any]) -> None:
    initial = observed["form_values_initial"]
    assert (initial["origin"], initial["destination"], initial["adults"]) == ("", "", "")


def test_structured_form_stays_open_without_llm(observed: dict[str, Any]) -> None:
    assert observed["structured_open"] is True, "без LLM форма — основной путь ввода (DEC-08)"


def test_structured_form_is_collapsed_when_llm_is_live(tmp_path: Path) -> None:
    """UI-003: при Live LLM форма свёрнута, но остаётся в странице. Ходов нет — LLM не вызывается."""
    env = {
        "ODIN_LLM_MODE": "live",
        "ODIN_LLM_BASE_URL": "https://llm.invalid/v1",
        "ODIN_LLM_MODEL": "test-model",
        "ODIN_LLM_API_KEY": "fake",
        "ODIN_LLM_COMPOSER": "false",
    }
    for base in _serve(tmp_path, env):
        result = _run(base, "session")
        assert result["llm_badge"] == "LLM: подключён"
        assert result["structured_open"] is False
        assert result["post_requests"] == ["/api/v1/sessions"], "только создание сессии, без ходов LLM"


# ---------------------------------------------------------------- UI-003: режим «Без LLM»

FAKE_LLM_ENV = {
    "ODIN_LLM_MODE": "live",
    "ODIN_LLM_BASE_URL": "https://llm.example.test/v1",
    "ODIN_LLM_MODEL": "fake-model",
    "ODIN_LLM_API_KEY": "test-key",
}
FORM_DATES = f"{TRIP_START.isoformat()},{TRIP_END.isoformat()}"
# UI-010: при подключённом LLM ручной режим скрыт из публичного интерфейса; его проверки — по диагностическому
# адресу «?manual=1» (реализация и API ручного режима не изменены).
MANUAL_DIAGNOSTIC = "?manual=1"


@pytest.mark.req("UI-003", "DEC-08")
def test_manual_mode_is_offered_when_llm_is_off_and_keeps_text(server: str) -> None:
    result = _run(
        server,
        "manual",
        HARNESS_TEXT_FAIL="Хочу в Казань на выходные",
        HARNESS_OPEN="offer",
        HARNESS_FORM_DATES=FORM_DATES,
        HARNESS_FORM_FIELDS="origin=Москва,destination=Казань,adults=2",
    )
    assert result["manual_button"] == "Без LLM"
    assert result["offer_at_start"] is True, "без LLM ручной режим предлагается сразу"
    assert "Свободный диалог недоступен" in result["fail_reply"]
    assert result["fail_text_kept"] == "Хочу в Казань на выходные", "введённый текст не стирается"
    assert result["offer_after_fail"] is True and "без LLM" in result["offer_text"]
    assert result["opener_found"] and result["structured_open_after"] is True
    assert result["offer_after_open"] is False
    assert "Ручной режим без LLM" in result["manual_message"]
    assert result["form_user_message"] == "Запрос из формы (без LLM)"
    assert result["cards_after_form"] > 0, result["form_reply"]


@pytest.mark.req("UI-003", "DEC-08")
def test_manual_mode_keeps_dialog_parameters_and_does_not_call_llm(tmp_path: Path) -> None:
    dates = f"с {ru_date(TRIP_START)} по {ru_date(TRIP_END)}"
    text = f"Хочу из Москвы в Казань {dates} втроём, туда и обратно, не дороже 90000 рублей"
    fake = ScriptedLlm(
        {
            text: {
                "origin": {"value": "Москва", "span": "из Москвы"},
                "destination": {"value": "Казань", "span": "в Казань"},
                "dates": {"date_phrase": dates},
                "adults": {"value": 3, "span": "втроём"},
                "return_trip": {"value": True, "span": "туда и обратно"},
                "budget": {"amount": "90000", "currency": "RUB", "required": True, "span": "не дороже 90000 рублей"},
            }
        }
    )
    for base in _serve(tmp_path, FAKE_LLM_ENV, llm=fake):
        result = _run(base, "manual", HARNESS_TEXT_OK=text, HARNESS_TOUCH="stars_min=4", HARNESS_SEARCH=MANUAL_DIAGNOSTIC)
    assert result["offer_at_start"] is False and result["offer_after_ok"] is False
    assert result["ok_text_cleared"] is True
    assert fake.roles() == ["interpreter", "composer"], "ход из формы не вызывает ни Интерпретатор, ни Composer"
    values = result["form_values"]
    assert (values["origin"], values["destination"]) == ("Москва", "Казань")
    assert (values["date_from"], values["date_to"]) == (TRIP_START.isoformat(), TRIP_END.isoformat())
    assert values["adults"] == "3" and values["return_trip"] is True
    assert values["budget_amount"] == "90000.00" and values["budget_required"] is True
    assert values["stars_min"] == "4", "поле, изменённое пользователем в форме, не перезаписано"
    assert "перенесены в форму" in result["manual_message"]
    assert result["cards_after_form"] > 0, result["form_reply"]


@pytest.mark.req("UI-003", "DEC-08")
def test_manual_mode_is_offered_when_llm_fails(tmp_path: Path) -> None:
    fake = ScriptedLlm({}, failure=ChatResult("error", None, "auth_401", 401, 1, 4))
    for base in _serve(tmp_path, FAKE_LLM_ENV, llm=fake):
        result = _run(
            base,
            "manual",
            HARNESS_TEXT_FAIL="Хочу в Казань",
            HARNESS_OPEN="offer",
            HARNESS_FORM_DATES=FORM_DATES,
            HARNESS_FORM_FIELDS="origin=Москва,destination=Казань,adults=2",
            HARNESS_SEARCH=MANUAL_DIAGNOSTIC,
        )
    assert result["offer_at_start"] is False
    assert result["fail_text_kept"] == "Хочу в Казань"
    assert result["offer_after_fail"] is True and "LLM сейчас недоступен" in result["offer_text"]
    assert result["structured_open_after"] is True
    assert result["cards_after_form"] > 0, result["form_reply"]
    assert set(fake.roles()) == {"interpreter"}, "после сбоя ход из формы к LLM не обращается"


# ---------------------------------------------------------------- CITY-001: выбор предложенного направления


@pytest.mark.req("CITY-001")
def test_destination_candidates_are_clickable_and_keep_origin(server: str) -> None:
    result = _run(server, "inspiration")
    cards = result["destination_cards"]
    assert len(cards) >= 2
    assert all(card["selectable"] and card["role"] == "button" and card["tabindex"] == "0" for card in cards)
    assert result["choice_body"]["form"] == {"destination": result["after_click"]["destination"]}, (
        "клик передаёт только «куда», а не весь набор полей"
    )
    after = result["after_click"]
    assert after["origin"] == "Москва", "«откуда» не подменяется выбранным городом"
    assert after["destination"] != "Москва" and after["adults"] == 2 and after["dates"] is not None
    assert result["offer_cards_after_click"] > 0, "после выбора собран план с вариантами"
    assert len(result["selected_destination"]) == 1
    assert result["reclick_requests"] == 0 and "уже выбрано" in result["reclick_message"]
    keyboard = result.get("after_keyboard")
    assert keyboard is not None, "второй кандидат выбирается клавиатурой"
    assert keyboard["origin"] == "Москва" and keyboard["destination"] in keyboard["card_text"]


# ---------------------------------------------------------------- DATE-003: кнопки вариантов дат


@pytest.mark.req("DATE-003")
def test_date_options_are_buttons_and_choice_keeps_trip_without_llm(tmp_path: Path) -> None:
    text = "Из Москвы в Казань в ноябре на 3 ночи вдвоём"
    fake = ScriptedLlm(
        {
            text: {
                "origin": {"value": "Москва", "span": "Из Москвы"},
                "destination": {"value": "Казань", "span": "в Казань"},
                "dates": {"date_phrase": "в ноябре", "nights_phrase": "на 3 ночи"},
                "adults": {"value": 2, "span": "вдвоём"},
            }
        }
    )
    for base in _serve(tmp_path, FAKE_LLM_ENV, llm=fake):
        result = _run(base, "dates", HARNESS_TEXT=text)
    options = result["options"]
    assert 2 <= len(options) <= 4 and all("ноч" in option["label"] for option in options)
    assert result["options_group_label"] == ["Варианты дат поездки"]
    assert all("не бронирование" in option["title"] for option in options)
    assert result["choice_posts"] == 1, "двойной клик — один запрос"
    choice = result["choice_body"]["choice"]
    assert choice["option_id"] == options[1]["id"] and "text" not in result["choice_body"]
    after = result["after_choice"]
    assert (after["dates"]["start"], after["dates"]["end"]) == (choice["date_from"], choice["date_to"])
    assert (after["origin"], after["destination"], after["adults"]) == ("Москва", "Казань", 2)
    assert result["options_after"] == 0 and result["offer_cards_after"] > 0
    assert result["options_in_assistant_message"] is True, "варианты — в сообщении ассистента (UI-008)"
    assert result["stale_options_after"] == len(options), "варианты прошлого хода не кликаются"
    assert result["choice_user_message"].startswith("Выбираю даты:")
    assert fake.roles() == ["interpreter", "composer"], "выбор дат не вызывает ни Интерпретатор, ни Composer"


# ---------------------------------------------------------------- этап 60: CITY-002, UI-008/009/010

CITY_SCRIPT = {
    "Хочу на море из Москвы вдвоём": {
        "origin": {"value": "Москва", "span": "из Москвы"},
        "adults": {"value": 2, "span": "вдвоём"},
        "wishes_tags": {"value": ["море"], "span": "на море"},
    },
    "послезавтра": {"dates": {"date_phrase": "послезавтра"}},
    "Нет, лучше Казань": {"unparsed": ["Нет, лучше Казань"]},
}


@pytest.fixture
def city_dialog(tmp_path: Path) -> tuple[dict[str, Any], ScriptedLlm]:
    fake = ScriptedLlm(CITY_SCRIPT)
    for base in _serve(tmp_path, FAKE_LLM_ENV, llm=fake):
        result = _run(
            base,
            "city",
            HARNESS_TEXT="Хочу на море из Москвы вдвоём",
            HARNESS_TEXT_DATES="послезавтра",
            HARNESS_TEXT_CHANGE="Нет, лучше Казань",
            HARNESS_CHANGE_TO="Сочи",
        )
    return result, fake


@pytest.mark.req("CITY-002")
def test_destination_can_be_changed_again_and_again(city_dialog: tuple[dict[str, Any], ScriptedLlm]) -> None:
    result, fake = city_dialog
    names = result["names"]
    assert len(names) >= 2 and result["city_group_label"] == 1, "города — кнопки в сообщении с предложением"
    expected = [names[0], names[1], names[0]]
    assert [step["destination"] for step in result["steps"]] == expected, "A → B → A (лента и панель)"
    for step, name in zip(result["steps"], expected, strict=True):
        assert step["panel_cards"] == len(names) and step["chat_buttons"] == len(names), "список не исчезает"
        assert step["chat_selected"] == [name]
    after = result["after_steps"]
    assert after["origin"] == "Москва" and after["adults"] == 2 and after["date_expressions"] == ["послезавтра"]
    assert "отъезд" in result["dates_text"] and "«послезавтра»" in result["dates_text"], result["dates_text"]
    assert result["after_text"] == "Казань", "смена текстом «Нет, лучше Казань»"
    assert result["change_button"] == "Изменить направление" and result["change_form_visible"] is True
    assert result["change_requests"] == 0, "кнопка открывает форму, а не отправляет запрос"
    assert result["after_change_form"]["destination"] == "Сочи" and result["change_form_hidden_after"] is True
    assert result["change_body"]["form"] == {"destination": "Сочи"}
    assert fake.roles().count("interpreter") == 3, "клики и «Изменить направление» не вызывают LLM"


@pytest.mark.req("UI-008", "UI-009")
def test_single_chat_feed_without_duplicates(city_dialog: tuple[dict[str, Any], ScriptedLlm]) -> None:
    result, _fake = city_dialog
    assert result["questions_panel"] is False, "отдельной панели «Вопрос» нет"
    messages = result["messages"]
    roles = [m["role"] for m in messages]
    assert roles == ["msg user", "msg assistant"] * (len(messages) // 2), "сообщения по очереди, каждое один раз"
    assert not any(m["text"].startswith("Вопрос:") for m in messages)
    users = [m["text"] for m in messages if m["role"] == "msg user"]
    assert users[:2] == ["Хочу на море из Москвы вдвоём", "послезавтра"] and "Нет, лучше Казань" in users
    assert result["placeholder_initial"].startswith("Например: из Москвы")
    assert result["placeholder_after_first"] == "Напишите сообщение…"
    assert result["draft_kept"] == "черновик", "введённый текст не сбрасывается при обновлении интерфейса"


def test_chat_feed_has_no_inner_scroll() -> None:
    css = (Path(__file__).resolve().parents[2] / "src/odin/static/app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.messages\s*\{([^}]*)\}", css)
    assert rule is not None and "overflow" not in rule.group(1) and "max-height" not in rule.group(1)
    html = (Path(__file__).resolve().parents[2] / "src/odin/static/index.html").read_text(encoding="utf-8")
    assert 'id="questions"' not in html, "вопросы — в ленте, а не в отдельной панели"


@pytest.mark.req("UI-010")
def test_manual_mode_is_hidden_in_public_interface_with_llm(tmp_path: Path) -> None:
    fake = ScriptedLlm({}, failure=ChatResult("error", None, "auth_401", 401, 1, 4))
    for base in _serve(tmp_path, FAKE_LLM_ENV, llm=fake):
        result = _run(base, "public", HARNESS_TEXT_FAIL="Хочу в Казань")
    assert result["manual_btn_hidden"] is True and result["form_hidden"] is True
    assert result["offer_hidden_at_start"] is True and result["offer_hidden_after_fail"] is True
    assert result["fail_text_kept"] == "Хочу в Казань", "текст при сбое LLM не стирается"
    notice = result["llm_notice"]
    assert "передаются выбранному LLM-провайдеру" in notice, "уведомление о передаче данных сохранено"
    assert "Форма" not in notice and "без LLM" in notice
    text = " ".join(result["visible_text"])
    assert "Без LLM" not in text and "Заполнить форму" not in text and "заполните форму" not in text
