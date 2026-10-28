"""Composer S1: ответ собирается из таблицы фактов через слоты (ARCH-04, TEST-04, план §2.11).

Свободный текст не генерируется: Composer только подставляет значения слотов. Проверка фактов
сравнивает все числа и даты в готовом тексте с таблицей; при расхождении возвращается шаблон.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from decimal import Decimal
from typing import Any

NumberToken = re.compile(r"\d[\d\s.,]*\d|\d")


@dataclass(frozen=True, slots=True)
class ComposerFacts:
    """Таблица фактов для ответа. Всё, чего здесь нет, в текст не попадает."""

    destination: str | None = None
    origin: str | None = None
    dates: str | None = None
    party: str | None = None
    known_amount: str | None = None
    currency: str | None = None
    completeness: str | None = None
    offers_count: int | None = None
    candidates: tuple[str, ...] = field(default=())
    unknowns: tuple[str, ...] = field(default=())
    stop_reason: str | None = None
    mode_note: str = "ДЕМО-данные: не цена, не наличие и не расписание"
    cart_id: str | None = None
    receipt_id: str | None = None
    questions: tuple[str, ...] = field(default=())
    # Пояснения хода (например, итог пересборки) и допущения расчёта рядом с суммой (UC-03, DEC-25).
    notes: tuple[str, ...] = field(default=())
    assumptions: tuple[str, ...] = field(default=())
    # Нарушенные условия (UC-06, UC-10: превышение бюджета видно, а не скрыто).
    violations: tuple[str, ...] = field(default=())
    # Состав выбранного плана и статус каждого условия пользователя (UC-05/06: что выбрано и почему).
    selected: tuple[str, ...] = field(default=())
    conditions: tuple[str, ...] = field(default=())

    def numeric_values(self) -> set[str]:
        """Все числа, которые разрешено упоминать в тексте."""
        values: set[str] = set()
        for item in dataclass_fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, int):
                values.add(str(value))
            elif isinstance(value, str):
                values.update(self._from_text(value))
            elif isinstance(value, tuple):
                for entry in value:
                    if isinstance(entry, str):
                        values.update(self._from_text(entry))
        return values

    @staticmethod
    def _from_text(text: str) -> set[str]:
        found: set[str] = set()
        for match in NumberToken.findall(text):
            normalized = match.replace(" ", "").replace(",", ".")
            found.add(normalized)
            found.add(normalized.rstrip("0").rstrip(".") if "." in normalized else normalized)
        return found


COMPLETENESS_TEXT = {
    "complete": "стоимость полная",
    "partial": "стоимость известна частично",
    "unknown": "стоимость неизвестна",
}

# Причины остановки поиска человеческим языком (код остаётся в API и журнале).
STOP_REASON_TEXT = {
    "sufficient_results": "найдено достаточно вариантов",
    "sources_exhausted": "опрошены все подключённые источники",
    "limits_exhausted": "исчерпан лимит запросов на ход",
    "deadline": "истекло время на ход",
    "provider_unavailable": "источник данных недоступен",
    "user_stopped": "остановлен пользователем",
    "blocked_by_decision": "источник не разрешён к использованию",
}


def _sentence(text: str) -> str:
    text = text.strip().rstrip(".")
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _money(amount: str | None, currency: str | None) -> str:
    if amount is None:
        return "сумма неизвестна"
    suffix = {"RUB": "₽", "EUR": "€", "USD": "$"}.get(currency or "", currency or "")
    integer, _, fraction = amount.partition(".")
    grouped = f"{int(integer):,}".replace(",", " ") if integer.lstrip("-").isdigit() else integer
    shown = grouped + (f",{fraction}" if fraction and fraction != "00" else "")
    return f"{shown} {suffix}".strip()


def compose_answer(facts: ComposerFacts) -> str:
    """Шаблонный ответ по таблице фактов. Все числа берутся только из слотов."""
    parts: list[str] = [_sentence(note) for note in facts.notes if note.strip()]
    if facts.questions:
        parts.append("Нужно уточнить: " + " ".join(facts.questions))
        return "\n".join(parts)

    if facts.cart_id is not None:
        parts.append(f"Корзина {facts.cart_id} готова к подтверждению.")
    if facts.receipt_id is not None:
        parts.append(
            "Демонстрационная покупка оформлена. Реального бронирования и списания средств нет. "
            f"Чек {facts.receipt_id}."
        )
        return "\n".join(parts)

    if facts.destination is None and facts.candidates:
        parts.append("Направление не задано. Заготовка вдохновения (ДЕМО):")
        parts.extend(f"— {entry}" for entry in facts.candidates)
        # UC-04: норма климата не равна прогнозу; визы без официального источника не утверждаются.
        parts.append(
            "Климатические нормы не загружены, прогноз погоды не выполнялся; визовые требования не проверены."
        )
    elif facts.destination is not None:
        route = f"{facts.origin} - {facts.destination}" if facts.origin else facts.destination
        parts.append(f"Поездка: {route}.")
        if facts.dates:
            parts.append(f"Даты: {facts.dates}.")
        if facts.party:
            parts.append(f"Состав группы: {facts.party}.")
        if facts.offers_count is not None:
            parts.append(f"Найдено вариантов: {facts.offers_count}.")
        if facts.known_amount is not None or facts.completeness is not None:
            total = f"Итог: {_money(facts.known_amount, facts.currency)}"
            if facts.completeness:
                total += " — " + COMPLETENESS_TEXT.get(facts.completeness, facts.completeness)
            parts.append(total + ".")
            # UC-03: допущения показываются рядом с суммой; без допущений — так и сказано.
            if facts.assumptions:
                parts.append("Допущения расчёта: " + "; ".join(facts.assumptions) + ".")
            else:
                parts.append("Допущений в расчёте нет: использованы только ваши данные.")
        if facts.selected:
            parts.append("Выбрано:")
            parts.extend(f"— {entry}" for entry in facts.selected)
        if facts.conditions:
            parts.append("Ваши условия: " + "; ".join(facts.conditions) + ".")
    elif not facts.notes:
        parts.append("Данных для плана пока нет.")

    if facts.violations:
        parts.append("Нарушено: " + "; ".join(facts.violations) + ".")
    if facts.unknowns:
        parts.append("Не подтверждено: " + "; ".join(facts.unknowns) + ".")
    if facts.stop_reason:
        reason = STOP_REASON_TEXT.get(facts.stop_reason, facts.stop_reason)
        parts.append(f"Поиск завершён: {reason}. Это не означает, что вариантов нет.")
    parts.append(facts.mode_note + ".")
    return "\n".join(parts)


def verify_facts(text: str, facts: ComposerFacts) -> tuple[bool, list[str]]:
    """Проверка фактов: каждое число и каждая ISO-дата текста должны быть в таблице фактов."""
    allowed = facts.numeric_values()
    offenders = []
    for match in NumberToken.findall(text):
        normalized = match.replace(" ", "").replace(",", ".").rstrip("0").rstrip(".")
        candidates = {match.replace(" ", ""), normalized, match}
        if not candidates & allowed:
            offenders.append(match)
    return (not offenders), offenders


def safe_answer(facts: ComposerFacts) -> tuple[str, bool]:
    """Ответ с проверкой фактов. При нарушении — консервативный шаблон без чисел (TEST-04)."""
    answer = compose_answer(facts)
    ok, offenders = verify_facts(answer, facts)
    if ok:
        return answer, True
    fallback = (
        "Ответ сформирован в ограниченном режиме: часть данных не подтверждена проверкой фактов. "
        f"{facts.mode_note}."
    )
    return fallback, False


def amount_text(amount: Decimal | None) -> str | None:
    return None if amount is None else format(amount, "f")


# Утверждения, которые LLM-Composer не может добавить сам (UC-08, AVAIL-01): только если они есть в черновике.
_CLAIM_STEMS = ("заброниров", "оплачен", "оплатил", "гарантир", "в наличии", "точно есть", "свободн")


def facts_payload(facts: ComposerFacts) -> dict[str, Any]:
    """Таблица фактов для LLM-Composer: только непустые слоты."""
    data: dict[str, Any] = {}
    for item in dataclass_fields(facts):
        value = getattr(facts, item.name)
        if value in (None, (), ""):
            continue
        data[item.name] = list(value) if isinstance(value, tuple) else value
    return data


def check_rewrite(draft: str, candidate: str, facts: ComposerFacts) -> tuple[bool, str, list[str]]:
    """Проверка переформулировки LLM (TEST-04): числа — только из фактов, без новых обещаний.

    Возвращает (принято, итоговый текст, причины отказа). Пометка режима данных добавляется кодом,
    если модель её потеряла: маркировка Mock/Replay не может исчезнуть (DATA-MODE-01).
    """
    text = candidate.strip()
    reasons: list[str] = []
    if not text:
        return False, draft, ["пустой ответ"]
    ok, offenders = verify_facts(text, facts)
    if not ok:
        reasons.append("числа вне таблицы фактов: " + ", ".join(offenders[:5]))
    lowered, draft_lowered = text.lower(), draft.lower()
    for stem in _CLAIM_STEMS:
        if stem in lowered and stem not in draft_lowered:
            reasons.append(f"новое утверждение «{stem}…»")
    if facts.questions and "?" not in text:
        reasons.append("вопросы черновика потеряны")
    if len(text) > max(1500, 3 * len(draft)):
        reasons.append("ответ слишком длинный")
    if reasons:
        return False, draft, reasons
    note = facts.mode_note.rstrip(".")
    if note and note.lower() not in lowered and note.lower() in draft_lowered:
        text = f"{text}\n{note}."
    return True, text, []


def money_text(amount: Decimal | None, currency: str | None) -> str:
    """Сумма в формате ответа (те же правила, что у итога плана)."""
    return _money(amount_text(amount), currency)


def facts_from_values(**values: Any) -> ComposerFacts:
    """Удобная сборка фактов с отбрасыванием лишних ключей."""
    allowed = {item.name for item in dataclass_fields(ComposerFacts)}
    return ComposerFacts(**{key: value for key, value in values.items() if key in allowed})
