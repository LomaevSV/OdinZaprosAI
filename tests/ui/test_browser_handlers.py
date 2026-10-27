"""UI-001 / CART-UI-001: реальные обработчики app.js против реального API (Mock, loopback).

Без браузера и без тяжёлых зависимостей: ``tests/ui/app_harness.js`` подставляет минимальный DOM
и выполняет настоящий ``src/odin/static/app.js``; запросы идут в FastAPI на 127.0.0.1 с временной БД.
Тест пропускается, если Node.js недоступен. Сеть наружу, LLM и платные вызовы не используются.
"""

from __future__ import annotations

import json
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


def _serve(tmp_path: Path, env: dict[str, str]) -> Iterator[str]:
    import uvicorn

    settings = Settings.from_env(
        {"ODIN_DB_PATH": str(tmp_path / "ui.sqlite3"), "ODIN_DATA_MODE": "mock", **env}
    )
    port = _free_port()
    config = uvicorn.Config(
        create_app(application=build_application(settings)), host="127.0.0.1", port=port, log_level="warning"
    )
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


def _run(base: str, mode: str = "full") -> dict[str, Any]:
    completed = subprocess.run(
        [NODE, str(HARNESS)],  # type: ignore[list-item]
        env={"ODIN_BASE": base, "PATH": "", "HARNESS_MODE": mode},
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


def test_mock_banner_replaces_per_card_availability_repeats(observed: dict[str, Any]) -> None:
    assert observed["mock_banner_hidden"] is False, "Mock обозначен общим баннером"
    assert observed["demo_card_availability_lines"] == 0, (
        "без повтора «не подтверждено (ДЕМО)» на каждой карточке"
    )


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
        assert result["llm_badge"] == "LLM: live"
        assert result["structured_open"] is False
        assert result["post_requests"] == ["/api/v1/sessions"], "только создание сессии, без ходов LLM"
