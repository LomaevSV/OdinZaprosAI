"""Язык интерфейса (этап 59): весь пользовательский веб-интерфейс — на русском.

Проверяются статические тексты ``index.html`` (видимый текст, ``title``, ``placeholder``, ``aria-label``),
пользовательские строки ``app.js`` и то, что реально показал настоящий ``app.js`` на реальном API (Mock,
loopback, без LLM и платных вызовов). Латиница допустима только в общепринятых обозначениях и технических
идентификаторах (``odin_mock``, ``cart-…``, коды валют).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from tests.ui.test_browser_handlers import NODE, _run, _serve

pytestmark = pytest.mark.req("UI-01", "UI-LANG-RU")

STATIC = Path(__file__).resolve().parents[2] / "src" / "odin" / "static"

# Общепринятые обозначения, которые остаются латиницей (названия режимов, LLM, коды валют, ссылки на решения).
ALLOWED_WORDS = {"LLM", "Live", "Mock", "Replay", "API", "DEC", "UC", "RUB", "EUR", "USD"}
# Технические идентификаторы: латиница вместе с цифрами, «_», «-», «.», «/» или «:» (odin_mock, cart-1a2b).
_IDENTIFIER = re.compile(r"[A-Za-z0-9]*(?:[0-9_\-./:][A-Za-z0-9]+)+|[A-Za-z]+[0-9][A-Za-z0-9]*")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def english_words(texts: Iterable[str]) -> list[str]:
    found: list[str] = []
    for value in texts:
        cleaned = _IDENTIFIER.sub(" ", value)
        found.extend(word for word in _LATIN_WORD.findall(cleaned) if word not in ALLOWED_WORDS)
    return sorted(set(found))


def test_detector_catches_english_and_allows_identifiers() -> None:
    assert english_words(["Корзина cart-1a2b3c; Источник: odin_mock / search_hotels", "LLM: подключён"]) == []
    assert english_words(["Add to cart", "Ошибка: Failed to fetch"]) == [
        "Add",
        "Failed",
        "cart",
        "fetch",
        "to",
    ]


class _Texts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        for name, value in attrs:
            if name in ("title", "placeholder", "aria-label", "alt") and value:
                self.texts.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.texts.append(data.strip())


def test_static_page_texts_are_russian() -> None:
    parser = _Texts()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert parser.texts, "тексты страницы не найдены"
    assert english_words(parser.texts) == []
    assert '<html lang="ru">' in (STATIC / "index.html").read_text(encoding="utf-8")


_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`')


def _user_literals(script: str) -> Iterator[str]:
    """Строки ``app.js`` с кириллицей — пользовательские сообщения (подстановки ``${…}`` отбрасываются)."""
    for match in _LITERAL.finditer(script):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        value = re.sub(r"\$\{[^}]*\}", " ", value)
        if re.search(r"[А-Яа-яЁё]", value):
            yield value


def test_script_user_strings_are_russian() -> None:
    literals = list(_user_literals((STATIC / "app.js").read_text(encoding="utf-8")))
    assert len(literals) > 50
    assert english_words(literals) == []


@pytest.mark.skipif(NODE is None, reason="Node.js недоступен: UI-стенд не запускается")
def test_rendered_interface_is_russian(tmp_path: Path) -> None:
    """Реальные обработчики: план, выбор, корзина, вдохновение и выбор направления — без английских надписей."""
    observed: list[str] = []
    for base in _serve(tmp_path, {"ODIN_LLM_MODE": "off"}):
        full: dict[str, Any] = _run(base)
        inspiration: dict[str, Any] = _run(base, "inspiration")
        observed = [*full["visible_text"], full["cart_text"], *inspiration["visible_text"]]
    assert any("Корзина" in text for text in observed) and any(
        "Предложенные направления" in text for text in observed
    )
    assert english_words(observed) == []


@pytest.mark.skipif(NODE is None, reason="Node.js недоступен: UI-стенд не запускается")
def test_date_option_buttons_are_russian(tmp_path: Path) -> None:
    from tests.llm_fakes import ScriptedLlm
    from tests.ui.test_browser_handlers import FAKE_LLM_ENV

    text = "Из Москвы в Казань в ноябре на 3 ночи вдвоём"
    delta = {
        "origin": {"value": "Москва", "span": "Из Москвы"},
        "destination": {"value": "Казань", "span": "в Казань"},
        "dates": {"date_phrase": "в ноябре", "nights_phrase": "на 3 ночи"},
        "adults": {"value": 2, "span": "вдвоём"},
    }
    for base in _serve(tmp_path, FAKE_LLM_ENV, llm=ScriptedLlm({text: delta})):
        result = _run(base, "dates", HARNESS_TEXT=text)
    texts = [*(o["label"] for o in result["options"]), *(o["title"] for o in result["options"])]
    assert english_words([*texts, *result["options_group_label"], *result["visible_text"]]) == []


@pytest.mark.skipif(NODE is None, reason="Node.js недоступен: UI-стенд не запускается")
@pytest.mark.req("CITY-002", "UI-008", "UI-010")
def test_chat_feed_and_destination_change_are_russian(tmp_path: Path) -> None:
    from tests.llm_fakes import ScriptedLlm
    from tests.ui.test_browser_handlers import CITY_SCRIPT, FAKE_LLM_ENV

    for base in _serve(tmp_path, FAKE_LLM_ENV, llm=ScriptedLlm(CITY_SCRIPT)):
        city = _run(base, "city", HARNESS_TEXT="Хочу на море из Москвы вдвоём", HARNESS_TEXT_DATES="послезавтра")
        public = _run(base, "public")
    observed = [*city["visible_text"], *public["visible_text"], public["llm_notice"]]
    assert any("Изменить направление" in text for text in observed)
    assert english_words(observed) == []
