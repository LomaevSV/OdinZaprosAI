"""Оркестратор — детерминированный автомат хода (ARCH-01, ARCH-02, план §2.4, §2.23).

Единственный владелец шагов: политика уточнений, волны поиска, лимиты, ``stop_reason``, атомарная
запись ревизии, аудит и события SSE. Домены и LLM не вызывают инструменты напрямую.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any

from odin.config import Limits, Settings
from odin.contracts import (
    AgentResult,
    Cart,
    ConnectionCheck,
    ConstraintEvaluation,
    DemoReceipt,
    PlanComponent,
    Price,
    ReplanEvent,
    SearchTermination,
    ToolRequest,
    TravelOffer,
    TripPlan,
    TripRequest,
    TripState,
)
from odin.contracts.api import ChoiceAnswer, StructuredForm
from odin.contracts.common import Deviation
from odin.contracts.plan import CartItem
from odin.core import calendar
from odin.core.cart import CartError, CartStaleError, build_cart, confirm_cart, decline_cart
from odin.core.clarify import detect_changes, merge_answer
from odin.core.constraints import (
    OfferFacts,
    aggregate_statuses,
    evaluate_constraints,
    rank_key,
    sort_by_rank,
)
from odin.core.money import known_amount_in_currency, sum_prices_or_unresolved, total_price
from odin.core.replan import changed_groups, invalidated_roles, split_change_set
from odin.core.tree import coverage, tradeoffs
from odin.core.trip import (
    arrival_checkin_check,
    checkout_departure_check,
    evaluate_budget,
    judge_availability,
    offer_staleness,
)
from odin.llm.composer import ComposerFacts, check_rewrite, facts_payload, money_text, safe_answer
from odin.llm.free_text import (
    INTERPRETER_SCHEMA_VERSION,
    InterpretationDelta,
    awaiting_fields,
    outcome_from_delta,
    parse_delta,
    summarize_context,
)
from odin.llm.gateway import LlmGateway
from odin.llm.interpreter import (
    InterpretationOutcome,
    interpret_structured_form,
    refuse_free_text,
    request_to_form,
)
from odin.llm.privacy import redact
from odin.logging_setup import get_logger
from odin.storage import ConflictError, NotFoundError, Storage
from odin.tools.live_common import live_source
from odin.tools.mock_data import mock_source, normalize_city
from odin.tools.places import ResolvedPlace
from odin.tools.policy import ToolDeniedError
from odin.tools.registry import ToolBudgetExceeded, ToolDeadlineExceeded, ToolRegistry

logger = get_logger("orchestrator")

STOP_LIMITS = "limits_exhausted"
STOP_SOURCES = "sources_exhausted"
STOP_DEADLINE = "deadline"
STOP_PROVIDER = "provider_unavailable"
STOP_BLOCKED = "blocked_by_decision"

# История явных выборов пользователя (REPLAN-02): последняя запись определяет, что выбрано явно.
SELECTION_EVENT = "selection_applied"
SELECTION_KEPT_EVENT = "selection_kept"

# UC-04: число лучших кандидатов, для которых оценивается стоимость и время пути (план §4 S5).
INSPIRATION_TOP_N = 3

# Пометка актуальности плана и ответа по режиму данных поездки (DATA-MODE-01).
RELEVANCE_NOTES = {
    "mock": "ДЕМО-данные: наличие и часть цен не подтверждены",
    "mixed": "Смешанный режим: часть компонентов — ДЕМО (см. значки режимов); наличие не подтверждено",
    "live": "Live-данные на момент запроса: наличие и итоговая цена подтверждаются у поставщика при оформлении",
    "replay": "Replay: снимок прошлых ответов — цены на дату получения, не текущие",
}
MODE_NOTES = {
    "mock": "ДЕМО-данные: не цена, не наличие и не расписание",
    "mixed": "Смешанный режим: компоненты с пометкой ДЕМО — не цена, не наличие и не расписание",
    "live": "Данные Tutu/Open-Meteo/OSRM на момент запроса; наличие и итоговая цена подтверждаются при оформлении",
    "replay": "Replay: данные сохранённого снимка, не текущие цены",
}


ROLE_TITLES = {
    "outbound": "туда",
    "return": "обратно",
    "hotel": "гостиница",
    "transfer_in": "трансфер к гостинице",
    "transfer_out": "трансфер от гостиницы",
}
_KIND_TITLES = {"rail": "поезд", "flight": "самолёт"}
_SCOPE_TEXT = {
    "per_seat": "за место",
    "per_person": "за человека",
    "party_total": "за группу",
    "stay_total": "за весь срок",
    "per_room_stay": "за номер на весь срок",
    "per_night": "за ночь",
    "trip_total": "за поездку",
}
_EVAL_RANK = {"violated": 3, "unknown": 2, "matched": 1}
_EVAL_TEXT = {"violated": "нарушено", "unknown": "не подтверждено источником", "matched": "выполнено"}


def _role_title(role: str) -> str:
    return ROLE_TITLES.get(role, role)


def _adults_text(adults: int) -> str:
    return f"{adults} взрослый" if adults % 10 == 1 and adults % 100 != 11 else f"{adults} взрослых"


def _price_text(price: Any) -> str:
    if price is None or price.amount is None:
        return "цена неизвестна"
    scope = _SCOPE_TEXT.get(price.price_scope, "")
    partial = "" if price.total_completeness == "complete" else ", без сборов при оформлении"
    return f"{money_text(price.amount, price.currency)} {scope}".strip() + partial


def _constraint_label(constraint: Any, currency: str | None = None, *, mark_required: bool = True) -> str:
    """Условие пользователя словами (для ответа и списка неподтверждённого)."""
    value = constraint.value
    field_name = constraint.field
    if field_name == "budget_total":
        try:
            label = f"бюджет до {money_text(Decimal(str(value)), currency or 'RUB')}"
        except (InvalidOperation, ValueError):
            label = "бюджет"
    elif field_name == "transport_mode":
        modes = value if isinstance(value, list) else [value]
        names = ", ".join(_KIND_TITLES.get(str(mode), str(mode)) for mode in modes)
        label = f"транспорт: {'кроме ' if constraint.operator == 'not_in' else ''}{names}"
    elif field_name == "hotel_breakfast":
        label = "с завтраком" if value else "без завтрака"
    elif field_name == "hotel_stars_min":
        label = f"гостиница от {value}★"
    elif field_name == "hotel_free_cancellation":
        label = "бесплатная отмена" if value else "без требования бесплатной отмены"
    elif field_name == "pets":
        label = "размещение и проезд с животным"
    elif field_name == "hotel_room_type":
        label = "семейный номер (пожелание)"
    else:
        label = str(field_name)
    return label + (" (обязательно)" if mark_required and constraint.requiredness == "required" else "")


def _offer_price(offer: Any) -> Any:
    return offer.price_total if hasattr(offer, "price_total") else offer.price


def _fit_required_budget(
    selected: dict[str, tuple[Any, list[ConstraintEvaluation], int]],
    ranked: dict[str, list[tuple[Any, list[ConstraintEvaluation], int]]],
    request: TripRequest,
) -> dict[str, tuple[Any, list[ConstraintEvaluation], int]]:
    """RANK-01 на уровне плана: обязательный бюджет (область trip_total) — условие комбинации.

    Если известная часть итога выбранной комбинации больше обязательного бюджета, ищется комбинация,
    которая в него укладывается. Кандидаты — только того же класса RANK-01, что лучший для роли
    (цена не поднимает класс, DEC-26), явные выборы пользователя не заменяются, валюты не смешиваются
    (DEC-17). Из подходящих берётся ближайшая к исходному порядку ранжирования, затем более дешёвая.
    """
    constraint = next((c for c in request.constraints if c.field == "budget_total"), None)
    if constraint is None or constraint.requiredness != "required" or request.budget is None or not ranked:
        return selected
    limit, currency = Decimal(str(request.budget.amount)), request.budget.currency

    def amount(offer: Any) -> Decimal | None:
        price = _offer_price(offer)
        if price is None or price.amount is None or price.currency != currency:
            return None
        return Decimal(str(price.amount))

    fixed_total = Decimal("0")
    for role, (offer, _, _) in selected.items():
        if role in ranked:
            continue
        value = amount(offer)
        if value is None:
            return selected
        fixed_total += value
    current = [amount(selected[role][0]) for role in ranked]
    if any(value is None for value in current) or fixed_total + sum(current) <= limit:  # type: ignore[arg-type]
        return selected

    options: dict[str, list[tuple[int, Decimal, tuple[Any, list[ConstraintEvaluation], int]]]] = {}
    for role, items in ranked.items():
        best_class = items[0][2]
        options[role] = [
            (position, value, item)
            for position, item in enumerate(items)
            if item[2] == best_class and (value := amount(item[0])) is not None
        ][:20]
        if not options[role]:
            return selected
    best: tuple[int, Decimal, dict[str, Any]] | None = None
    roles = list(options)

    def walk(index: int, positions: int, total: Decimal, chosen: dict[str, Any]) -> None:
        nonlocal best
        if fixed_total + total > limit:
            return
        if index == len(roles):
            if best is None or (positions, total) < (best[0], best[1]):
                best = (positions, total, dict(chosen))
            return
        for position, value, item in options[roles[index]]:
            chosen[roles[index]] = item
            walk(index + 1, positions + position, total + value, chosen)
        chosen.pop(roles[index], None)

    walk(0, 0, Decimal("0"), {})
    if best is None:
        return selected  # укладывающейся комбинации нет: нарушение будет видно в плане (UC-06)
    return {**selected, **best[2]}


def _candidate_line(candidate: Any) -> str:
    """Строка кандидата для ответа: сумма только из оценки кода, иначе — честное примечание."""
    cost = candidate.estimated_cost
    if cost is not None and cost.amount is not None:
        return f"{candidate.place.name}, от {money_text(cost.amount, cost.currency)} — {candidate.cost_note}"
    return f"{candidate.place.name}, {candidate.cost_note}"


def _selection_note(selections: dict[str, str]) -> str | None:
    if not selections:
        return None
    return ";".join(f"{role}={offer_id}" for role, offer_id in sorted(selections.items()))


def _parse_selection_note(note: str | None) -> dict[str, str]:
    if not note:
        return {}
    pairs = (item.split("=", 1) for item in note.split(";") if "=" in item)
    return {role: offer_id for role, offer_id in pairs}


def _destination_only(form: StructuredForm) -> bool:
    """Выбор направления нажатием: в форме передано только «куда» (CITY-001/002)."""
    return form.model_fields_set == {"destination"} and bool(form.destination and form.destination.strip())


@dataclass(slots=True)
class TurnResult:
    state: TripState
    assistant_text: str
    questions: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    facts_verified: bool = True
    # Режим ввода хода: structured_form | free_text_llm | free_text_replay | llm_unavailable.
    input_mode: str | None = None


INPUT_MODES = {
    "structured_fallback": "structured_form",
    "free_text_llm": "free_text_llm",
    "free_text_replay": "free_text_replay",
    "llm_unavailable": "llm_unavailable",
}


@dataclass(slots=True)
class _TurnDraft:
    """Вычисленный, но ещё не записанный ход."""

    state: TripState
    assistant_text: str
    agreed: bool = False
    questions: list[dict[str, Any]] = field(default_factory=list)
    facts_verified: bool = True


@dataclass(slots=True)
class _Collector:
    """Накопитель вызовов инструментов и нормализованных офферов в пределах хода."""

    transport: list[TravelOffer] = field(default_factory=list)
    hotels: list[Any] = field(default_factory=list)
    hotel_offers: list[Any] = field(default_factory=list)
    transfers: list[TravelOffer] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    stop_reason: str | None = None
    checked_sources: list[str] = field(default_factory=list)
    unchecked_sources: list[dict[str, str]] = field(default_factory=list)
    candidates_seen: int = 0

    def note_stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason


class Orchestrator:
    """Автомат одного хода. Все внешние эффекты — только через `ToolRegistry` и `Storage`."""

    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        registry: ToolRegistry,
        llm: LlmGateway,
        limits: Limits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.registry = registry
        self.llm = llm
        self.limits = limits or settings.limits
        # Часы для проверки актуальности офферов (DEC-21); подменяются в тестах.
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        # Кэш геокодинга городов (часовой пояс и координаты города не меняются между ходами).
        self._places: dict[str, ResolvedPlace] = {}
        # Уведомления хода о сбоях Live и замене на ДЕМО: попадают в ответ (нет незаметной подмены).
        self._turn_notices: list[str] = []
        # UI-003: ход из формы («Без LLM») не обращается к LLM — ни к Интерпретатору, ни к Composer.
        self._turn_llm_allowed = True
        # LLM-CONCURRENCY-01: бюджеты LLM и инструментов, журнал вызовов и уведомления — состояние одного хода
        # в общем экземпляре. session_lock разделяет только ходы одной сессии, поэтому ходы разных сессий
        # выполняются по очереди: иначе начало чужого хода сбрасывало бы бюджет посреди текущего.
        self._turn_lock = threading.RLock()

    def _now(self) -> datetime:
        return self.clock()

    @property
    def live_data(self) -> bool:
        return self.settings.data_mode != "mock"

    # ------------------------------------------------------------------ места

    def _resolve_place(
        self, name: str | None, request_id: str, collector: _Collector | None = None
    ) -> Any | None:
        """Город → место с часовым поясом. Mock — демо-справочник; иначе геокодинг Open-Meteo (DEC-09).

        В ``mixed`` при сбое геокодера допускается демо-справочник (DEC-23); в ``live`` — нет.
        """
        if not name:
            return None
        if not self.live_data:
            return normalize_city(name)
        key = " ".join(name.lower().replace("ё", "е").split())
        if key in self._places:
            return self._places[key]
        result = self._invoke(
            self._tool_request(request_id=request_id, tool="geocode", arguments={"name": name}), collector
        )
        place: ResolvedPlace | None = None
        if result is not None and result.status in ("ok", "partial") and isinstance(result.data, dict):
            items = result.data.get("places") or []
            if items and isinstance(items[0], dict):
                raw = items[0]
                from odin.contracts import SourceReference

                place = ResolvedPlace(
                    name=str(raw.get("name") or name),
                    country_code=raw.get("country_code"),
                    timezone=raw.get("timezone"),
                    lat=raw.get("lat"),
                    lon=raw.get("lon"),
                    source=SourceReference.model_validate(raw["source"]) if raw.get("source") else None,
                )
        if place is None and self.settings.data_mode == "mixed":
            demo = normalize_city(name)
            if demo is not None:
                place = ResolvedPlace(demo.name, demo.country_code, demo.timezone, demo.lat, demo.lon)
        if place is not None:
            self._places[key] = place
        return place

    def _timezone_resolver(self, request_id: str) -> Callable[[str], str | None]:
        def resolve(name: str) -> str | None:
            place = self._resolve_place(name, request_id)
            return place.timezone if place is not None else None

        return resolve

    @staticmethod
    def _place_payload(place: Any) -> dict[str, Any]:
        return {
            "name": place.name,
            "country_code": place.country_code,
            "timezone": place.timezone,
            "lat": place.lat,
            "lon": place.lon,
        }

    # ------------------------------------------------------------------ сессии

    def create_session(self) -> dict[str, Any]:
        import uuid

        session_id = f"s-{uuid.uuid4().hex[:12]}"
        trip_id = f"t-{uuid.uuid4().hex[:12]}"
        session = self.storage.create_session(
            session_id, trip_id, llm_mode=self.llm.mode, data_mode=self.settings.data_mode
        )
        initial = TripState(
            trip_id=trip_id,
            session_id=session_id,
            revision=1,
            request=None,
            cart=None,
            status="collecting",
            plan=None,
            search_termination=None,
        )
        self.storage.append_revision(initial, request_id="session-create", agreed=False)
        self.storage.add_event(session_id, "session_created", {"trip_id": trip_id})
        return session

    def get_state(self, trip_id: str) -> TripState:
        return self.storage.get_state(trip_id)

    # ------------------------------------------------------------------ ход

    def handle_message(
        self,
        *,
        session_id: str,
        request_id: str,
        trip_revision: int,
        text: str | None = None,
        form: StructuredForm | None = None,
        choice: ChoiceAnswer | None = None,
    ) -> TurnResult:
        session = self.storage.get_session(session_id)
        trip_id = session["trip_id"]
        with (
            self.storage.session_lock(session_id),
            self._turn_lock,
            _span_scope(request_id=request_id, trip_id=trip_id, session_id=session_id),
        ):
            self._locked_head(trip_id, trip_revision)
            current = self.storage.get_state(trip_id)
            # Ход из формы и выбор варианта нажатием обрабатываются без LLM (UI-003, DATE-003).
            self._start_turn(session_id, request_id, trip_id, llm_allowed=form is None and choice is None)
            started = time.perf_counter()
            try:
                return self._handle_message_body(
                    current,
                    session_id=session_id,
                    request_id=request_id,
                    trip_id=trip_id,
                    text=text,
                    form=form,
                    choice=choice,
                )
            except Exception as exc:
                self._turn_failed(session_id, request_id, trip_id, exc, started)
                raise
            finally:
                self._end_turn()

    def _turn_failed(
        self,
        session_id: str,
        request_id: str,
        trip_id: str,
        exc: Exception,
        started: float,
    ) -> None:
        """Диагностика неудачного хода. Ошибки журналирования не подменяют исходное исключение."""
        payload = {
            "request_id": request_id,
            "error_code": type(exc).__name__,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        for record in (
            lambda: self._audit_llm(None, request_id),
            lambda: self.storage.add_event(session_id, "turn_failed", payload),
            lambda: self.storage.audit(
                "turn_failed", request_id=request_id, trip_id=trip_id, session_id=session_id, payload=payload
            ),
        ):
            try:
                record()
            except Exception as failure:  # OBS-SPAN-FAIL-01: телеметрия не маскирует бизнес-ошибку
                logger.warning("turn_failed not recorded: %s", type(failure).__name__)

    def _handle_message_body(
        self,
        current: TripState,
        *,
        session_id: str,
        request_id: str,
        trip_id: str,
        text: str | None,
        form: StructuredForm | None,
        choice: ChoiceAnswer | None = None,
    ) -> TurnResult:
        if choice is not None:
            outcome = self._interpret_choice(
                session_id=session_id, request_id=request_id, choice=choice, previous=current
            )
        else:
            outcome = self._interpret(
                session_id=session_id, request_id=request_id, text=text, form=form, previous=current
            )
        # Детектор изменений: новое сообщение при собранном плане — пересборка (UC-09, REPLAN-01).
        has_plan = current.plan is not None and bool(current.plan.components)
        input_mode = INPUT_MODES.get(outcome.mode, "structured_form")
        if has_plan and current.request is not None and not outcome.free_text_refused:
            assert current.request is not None
            if outcome.questions or (
                outcome.request is not None
                and detect_changes(request_to_form(current.request), request_to_form(outcome.request))
            ):
                result = self._apply_replan(current, outcome, request_id=request_id)
                result.input_mode = input_mode
                return result
        result = self._run_turn(current, outcome, request_id)
        result.input_mode = input_mode
        return result

    def _start_turn(
        self, session_id: str, request_id: str, trip_id: str, *, llm_allowed: bool = True
    ) -> None:
        self._turn_llm_allowed = llm_allowed
        self.registry.start_turn(datetime.now(tz=UTC) + timedelta(seconds=self.limits.turn_deadline_s))
        self.llm.start_turn(
            request_id=request_id,
            session_id=session_id,
            trip_id=trip_id,
            attempt_sink=lambda event: self.storage.audit(
                "llm_http_attempt",
                request_id=request_id,
                trip_id=trip_id,
                session_id=session_id,
                payload=event,
            ),
        )
        self._turn_notices = []
        self.storage.add_event(session_id, "turn_started", {"request_id": request_id})
        self.storage.audit("turn_started", request_id=request_id, trip_id=trip_id, session_id=session_id)

    def _end_turn(self) -> None:
        """Ход завершён: сброс привязки LLM к запросу (OBS-CONTEXT-03)."""
        self.llm.attempt_sink = None
        self._turn_llm_allowed = True

    def _run_turn(
        self,
        current: TripState,
        outcome: InterpretationOutcome,
        request_id: str,
        *,
        notes: tuple[str, ...] = (),
    ) -> TurnResult:
        """Шаги 4–10 цикла хода (§2.4): уточнения → поиск → план → ответ → атомарная запись."""
        draft = self._compute_turn(current, outcome, request_id, notes=notes)
        return self._persist(
            draft.state,
            request_id,
            assistant_text=draft.assistant_text,
            agreed=draft.agreed,
            questions=draft.questions,
            facts_verified=draft.facts_verified,
        )

    def _compute_turn(
        self,
        current: TripState,
        outcome: InterpretationOutcome,
        request_id: str,
        *,
        notes: tuple[str, ...] = (),
        search: bool = True,
        compose: bool = True,
    ) -> _TurnDraft:
        """Вычисление хода без записи: запись выполняет вызывающий (неудачная пересборка не пишется)."""
        answer = self._answer if compose else safe_answer
        state = current.evolve(**{"revision": current.revision + 1})
        if outcome.request is not None:
            state = state.evolve(**{"request": outcome.request})

        if outcome.free_text_refused:
            # Сбой LLM при собранном плане не переводит поездку в «уточнение»: условия не потеряны.
            keep = current.request is not None and current.status not in ("collecting",)
            state = state.evolve(**{"status": current.status if keep else "awaiting_clarification"})
            return _TurnDraft(state, " ".join(outcome.notes))

        # Пояснения свободного текста: отклонённые фрагменты, неразобранное (INTERPRET-01).
        text_notes = tuple(outcome.notes) if outcome.mode in ("free_text_llm", "free_text_replay") else ()
        if outcome.questions:
            state = state.evolve(**{"status": "awaiting_clarification"})
            facts = ComposerFacts(
                questions=tuple(question.question for question in outcome.questions), notes=text_notes
            )
            text_out, verified = answer(facts)
            return _TurnDraft(
                state,
                text_out,
                questions=[question.model_dump(mode="json") for question in outcome.questions],
                facts_verified=verified,
            )

        request = outcome.request
        assert request is not None
        if request.destination is None:
            state = self._inspiration_turn(state, request)
            names = tuple(_candidate_line(candidate) for candidate in state.candidate_destinations)
            text_out, verified = answer(ComposerFacts(candidates=names, notes=text_notes))
            return _TurnDraft(state, text_out, facts_verified=verified)

        if search:
            state = state.evolve(**{"status": "searching"})
            state = self._search(state, request)
        state = self._build_plan(state, request)
        text_out, verified = answer(self._plan_facts(state, notes=(*text_notes, *notes)))
        agreed = state.plan is not None and bool(state.plan.components)
        return _TurnDraft(state, text_out, agreed=agreed, facts_verified=verified)

    # ------------------------------------------------------------------ ответ

    def _answer(self, facts: ComposerFacts) -> tuple[str, bool]:
        """Шаблон по таблице фактов → (если включён) LLM-Composer → проверка фактов → шаблон при сбое."""
        text_out, verified = safe_answer(facts)
        if not verified or not self.llm.composer_enabled or not self._turn_llm_allowed:
            return text_out, verified
        result = self.llm.complete_structured(
            role="composer",
            text=text_out,
            reference_date=datetime.now(tz=UTC).date().isoformat(),
            schema_version="1.0.0",
            payload={"facts": facts_payload(facts)},
        )
        if result.status != "ok" or not isinstance(result.structured_output, dict):
            return text_out, True
        accepted, final, reasons = check_rewrite(
            text_out, str(result.structured_output.get("text", "")), facts
        )
        if not accepted:
            logger.info("composer: переформулировка отклонена (%s)", "; ".join(reasons)[:200])
        return final, True

    # ------------------------------------------------------------------ интерпретация

    def _interpret(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str | None,
        form: StructuredForm | None,
        previous: TripState | None = None,
    ) -> InterpretationOutcome:
        # UC-02: ответ на уточнение дополняет полученное ранее, повторно не спрашиваем.
        # CITY-001: выбор предложенного направления (форма с одним полем) так же дополняет условия поездки.
        answering = (
            previous is not None
            and previous.request is not None
            and (
                previous.status == "awaiting_clarification"
                or (form is not None and bool(previous.candidate_destinations))
            )
        )

        def interpret(value: StructuredForm) -> InterpretationOutcome:
            if answering:
                assert previous is not None and previous.request is not None
                value = merge_answer(request_to_form(previous.request), value)
            return interpret_structured_form(
                value, session_id=session_id, request_id=request_id, demo_places=not self.live_data
            )

        if form is not None:
            if previous is not None and previous.request is not None and _destination_only(form):
                return self._interpret_destination(
                    session_id=session_id, request_id=request_id, form=form, previous=previous
                )
            return interpret(form)
        if text:
            return self._interpret_text(
                session_id=session_id, request_id=request_id, text=text, previous=previous
            )
        return refuse_free_text(session_id=session_id, request_id=request_id)

    def _interpret_choice(
        self, *, session_id: str, request_id: str, choice: ChoiceAnswer, previous: TripState
    ) -> InterpretationOutcome:
        """DATE-003: выбранный нажатием вариант дат. Остальные условия и незакрытые уточнения сохраняются.

        Вариант сверяется с уточнением в текущей ревизии поездки. Если уточнение не записано (при собранном
        плане неудачная пересборка не пишется, REPLAN-02), принимается только вариант с каноническим
        идентификатором выбранных дат — это явный выбор пользователя, как ввод дат в форме."""
        request = previous.request
        if request is None:
            raise ConflictError("вариант дат больше не актуален: условия поездки изменились")
        target = next(
            (item for item in request.ambiguities if item.ambiguity_id == choice.ambiguity_id), None
        )
        dates = (choice.date_from, choice.date_to)
        if target is not None:
            option = next((item for item in target.options if item.option_id == choice.option_id), None)
            if option is None or (option.date_from, option.date_to) != dates:
                raise ConflictError("вариант дат больше не актуален: обновите поездку и выберите снова")
        else:
            has_plan = previous.plan is not None and bool(previous.plan.components)
            current = (request.date_range.start, request.date_range.end) if request.date_range else None
            if not has_plan or choice.option_id != calendar.window_option_id(*dates):
                raise ConflictError("вариант дат больше не актуален: обновите поездку и выберите снова")
            if current == dates:
                raise ConflictError("эти даты уже выбраны")
        label = f"Выбраны даты: {calendar.format_window(*dates)}"
        base = request_to_form(request).evolve(date_from=choice.date_from, date_to=choice.date_to)
        if request.input_mode == "structured_form":
            return interpret_structured_form(
                base, session_id=session_id, request_id=request_id, demo_places=not self.live_data
            )
        remaining = [item for item in request.ambiguities if item.ambiguity_id != choice.ambiguity_id]
        return outcome_from_delta(
            InterpretationDelta(),
            text=label,
            base=base,
            session_id=session_id,
            request_id=request_id,
            reference_datetime=datetime.now(tz=UTC),
            mode=request.input_mode,
            previous_constraints=list(request.constraints),
            timezone_for=self._timezone_resolver(request_id) if self.live_data else None,
            previous_ambiguities=remaining,
            previous_date_expressions=list(request.date_expressions),
        )

    def _interpret_destination(
        self, *, session_id: str, request_id: str, form: StructuredForm, previous: TripState
    ) -> InterpretationOutcome:
        """CITY-002: смена направления нажатием (предложенный город или «Изменить направление»), без LLM.

        Меняется только «куда»; прочие условия (откуда, даты, состав, бюджет, транспорт, пожелания) и
        незакрытые уточнения диалога сохраняются: фраза даты («послезавтра», «в середине лета») ждёт
        ответа дальше и не сбрасывается в общий вопрос о датах, как при разборе ручной формы."""
        request = previous.request
        assert request is not None and form.destination is not None
        name = " ".join(form.destination.split())
        base = request_to_form(request).evolve(destination=name)
        if request.input_mode == "structured_form":
            return interpret_structured_form(
                base, session_id=session_id, request_id=request_id, demo_places=not self.live_data
            )
        remaining = [item for item in request.ambiguities if item.field != "destination"]
        outcome = outcome_from_delta(
            InterpretationDelta(),
            text=f"Выбрано направление: {name}",
            base=base,
            session_id=session_id,
            request_id=request_id,
            reference_datetime=datetime.now(tz=UTC),
            mode=request.input_mode,
            previous_constraints=list(request.constraints),
            timezone_for=self._timezone_resolver(request_id) if self.live_data else None,
            previous_ambiguities=remaining,
            previous_date_expressions=list(request.date_expressions),
        )
        chosen = outcome.request.destination.name if outcome.request and outcome.request.destination else name
        return replace(
            outcome, notes=[f"Направление: {chosen}; прочие условия поездки сохранены", *outcome.notes]
        )

    def _interpret_text(
        self, *, session_id: str, request_id: str, text: str, previous: TripState | None
    ) -> InterpretationOutcome:
        """Свободный текст: LLM-Интерпретатор → проверка фрагментов и календарь в коде (план §2.4 шаг 2)."""
        if self.llm.mode == "off":
            return refuse_free_text(session_id=session_id, request_id=request_id)
        # Минимизация данных (решение 25.10): провайдеру уходит только текущее сообщение без секретов и
        # лишних персональных данных; в состоянии поездки хранится тоже очищенный текст.
        redaction = redact(text)
        text = redaction.text
        reference = datetime.now(tz=UTC)
        known = previous is not None and previous.request is not None
        base = request_to_form(previous.request) if known else StructuredForm()  # type: ignore[union-attr]
        context = summarize_context(base) if known else None
        if known and previous is not None and previous.status == "awaiting_clarification":
            # Модели — только названия ожидаемых полей (без истории переписки): короткий ответ «двое»
            # или «5 и 8 лет» понимается как ответ на заданный вопрос.
            awaiting = awaiting_fields(previous.request.ambiguities)  # type: ignore[union-attr]
            if awaiting:
                context = {**(context or {}), "awaiting_answers": awaiting}
        result = self.llm.complete_structured(
            role="interpreter",
            text=text,
            reference_date=reference.date().isoformat(),
            schema_version=INTERPRETER_SCHEMA_VERSION,
            payload={"context": context, "replay_context": {"context": context} if context else None},
        )
        if result.status != "ok" or not isinstance(result.structured_output, dict):
            return refuse_free_text(
                session_id=session_id, request_id=request_id, reason=self._llm_reason(result)
            )
        mode = (
            "free_text_replay"
            if any(ref.startswith("replay:") for ref in result.evidence_refs)
            else "free_text_llm"
        )
        delta = parse_delta(result.structured_output)
        if delta is None:
            # Запись Replay формата S1 (готовая структурированная форма).
            try:
                legacy = StructuredForm.model_validate(result.structured_output)
            except ValueError:
                return refuse_free_text(
                    session_id=session_id, request_id=request_id, reason="ответ интерпретатора непригоден"
                )
            if known:
                assert previous is not None and previous.request is not None
                legacy = merge_answer(request_to_form(previous.request), legacy)
            outcome = interpret_structured_form(
                legacy, session_id=session_id, request_id=request_id, demo_places=not self.live_data
            )
            assert outcome.request is not None
            return InterpretationOutcome(
                request=outcome.request.evolve(input_mode="free_text_replay"),
                mode="free_text_replay",
                questions=outcome.questions,
                notes=[*outcome.notes, *result.uncertainties],
                decisions=outcome.decisions,
            )
        outcome = outcome_from_delta(
            delta,
            text=text,
            base=base,
            session_id=session_id,
            request_id=request_id,
            reference_datetime=reference,
            mode=mode,  # type: ignore[arg-type]
            previous_constraints=list(previous.request.constraints) if known else None,  # type: ignore[union-attr]
            timezone_for=self._timezone_resolver(request_id) if self.live_data else None,
            previous_ambiguities=list(previous.request.ambiguities) if known else None,  # type: ignore[union-attr]
            previous_date_expressions=list(previous.request.date_expressions) if known else None,  # type: ignore[union-attr]
        )
        if mode == "free_text_replay":
            outcome = replace(outcome, notes=[*outcome.notes, *result.uncertainties])
        notice = redaction.notice()
        if notice:
            outcome = replace(outcome, notes=[notice, *outcome.notes])  # уведомление пользователя
        return outcome

    @staticmethod
    def _llm_reason(result: AgentResult) -> str:
        detail = result.uncertainties[0] if result.uncertainties else "LLM не ответил"
        return f"интерпретатор LLM не дал пригодного ответа ({detail})"

    def _audit_llm(self, state: TripState | None, request_id: str) -> None:
        """Журнал вызовов LLM без текстов (роль, статус, задержка, токены)."""
        for record in self.llm.calls:
            self.storage.audit(
                "llm_call",
                request_id=request_id,
                trip_id=state.trip_id if state else None,
                session_id=state.session_id if state else None,
                payload=record.as_dict(),
            )
        self.llm.calls.clear()

    # ------------------------------------------------------------------ вдохновение

    def _inspiration_turn(self, state: TripState, request: TripRequest) -> TripState:
        origin = normalize_city(request.origin.name if request.origin else None)
        tool_request = self._tool_request(
            request_id=request.request_id,
            tool="suggest_destinations",
            arguments={
                "wishes": list(request.wishes_tags),
                "limit": INSPIRATION_TOP_N,
                "exclude": origin.name if origin else None,
            },
        )
        result = self._invoke(tool_request)
        candidates = []
        if result is not None and result.status in ("ok", "partial") and isinstance(result.data, dict):
            values = result.data.get("candidates") or []
            candidates = [value for value in values]
        collector = _Collector()
        candidates = self._estimate_candidates(request, candidates, collector)
        state = state.evolve(
            **{
                "candidate_destinations": [*candidates],
                "status": "planned",
                "search_termination": SearchTermination(
                    reason=collector.stop_reason or STOP_SOURCES,  # type: ignore[arg-type]
                    checked_sources=sorted({"odin_demo_catalog", *collector.checked_sources}),
                    unchecked_sources=[value for value in collector.unchecked_sources],  # type: ignore[misc]
                    candidates_seen=len(candidates),
                    confirmed_matches=0,
                    unknown_candidates=len(candidates),
                    can_continue=collector.stop_reason in (STOP_LIMITS, STOP_DEADLINE),
                ),
                "history": [
                    *state.history,
                    self._history(
                        revision=state.revision, request_id=request.request_id, event="inspiration"
                    ),
                ],
            }
        )
        return state

    def _estimate_candidates(
        self, request: TripRequest, candidates: list[Any], collector: _Collector
    ) -> list[Any]:
        """UC-04: ориентировочная стоимость и время пути — только при известных городе отправления,
        датах и составе группы (UC-03: даты не предполагаются). Иначе — «стоимость требует дат».

        Итог всегда неполный: стоимость трансфера не оценивается (DEC-18), сборы отеля не подтверждены.
        """
        date_range = request.date_range
        adults = request.party.adults
        if date_range is None or not adults:
            return candidates
        origin = self._resolve_place(
            request.origin.name if request.origin else None, request.request_id, collector
        )
        if origin is None:
            return [
                candidate.evolve(
                    **{
                        "cost_note": (
                            "стоимость не оценена: город отправления вне демо-справочника"
                            if not self.live_data
                            else "стоимость не оценена: город отправления не найден геокодером"
                        )
                    }
                )
                for candidate in candidates
            ]
        estimated: list[Any] = []
        for candidate in candidates:
            transport = self._invoke(
                self._tool_request(
                    request_id=request.request_id,
                    tool="search_transport",
                    arguments={
                        "origin": origin.name,
                        "destination": candidate.place.name,
                        "date": date_range.start.isoformat(),
                        "return_date": (
                            date_range.end.isoformat() if request.return_trip is not False else None
                        ),
                        "adults": adults,
                        "children_ages": list(request.party.children_ages),
                        "modes": [],
                        "ttl_minutes": self.limits.offer_ttl_minutes,
                    },
                ),
                collector,
            )
            hotels = self._invoke(
                self._tool_request(
                    request_id=request.request_id,
                    tool="search_hotels",
                    arguments={
                        "city": candidate.place.name,
                        "check_in": date_range.start.isoformat(),
                        "check_out": date_range.end.isoformat(),
                        "adults": adults,
                        "children_ages": list(request.party.children_ages),
                        "rooms": request.party.rooms or 1,
                        "pets": bool(request.party.pets),
                        "ttl_minutes": self.limits.offer_ttl_minutes,
                    },
                ),
                collector,
            )
            estimated.append(self._candidate_estimate(candidate, request, transport, hotels))
        return estimated

    def _candidate_estimate(self, candidate: Any, request: TripRequest, transport: Any, hotels: Any) -> Any:
        def offers(result: Any) -> list[Any]:
            if result is None or not isinstance(result.data, dict):
                return []
            return list(result.data.get("offers") or [])

        def cheapest(items: list[Any], amount: Any) -> Any | None:
            priced = [item for item in items if amount(item) is not None]
            return min(priced, key=lambda item: (amount(item), item.offer_id)) if priced else None

        transport_offers = offers(transport)
        outbound = cheapest(
            [o for o in transport_offers if o.direction == "outbound"], lambda o: o.price.amount
        )
        back = cheapest([o for o in transport_offers if o.direction == "return"], lambda o: o.price.amount)
        hotel = cheapest(offers(hotels), lambda o: o.price_total.amount)
        parts = [
            ("outbound", outbound.price if outbound else None),
            ("hotel", hotel.price_total if hotel else None),
        ]
        if request.return_trip is not False:
            parts.append(("return", back.price if back else None))
        missing = [role for role, price in parts if price is None]
        known = [(role, price) for role, price in parts if price is not None]
        if not known:
            return candidate.evolve(
                **{
                    "cost_note": "стоимость не оценена: источники не вернули вариантов (это не значит, что их нет)"
                }
            )
        as_of = self._now()
        source = mock_source(
            "estimate_destination_cost", candidate.candidate_id, as_of, source_type="computed"
        )
        totals = sum_prices_or_unresolved([price for _, price in known], [role for role, _ in known])
        currency_unresolved = totals.unknown_reason == "currency_unresolved"
        # Трансфер не оценивается (DEC-18): итог не может быть полным. Разные валюты (DEC-17): итог неизвестен.
        totals = replace(
            totals,
            completeness="unknown" if currency_unresolved else "partial",
            unknown_reason=totals.unknown_reason or "price_incomplete",
            partial_components=(*totals.partial_components, *missing, "transfer"),
        )
        cost = total_price(totals, source, as_of)
        duration = outbound.duration_minutes if outbound else None
        prefix = "ДЕМО-оценка" if any(price.source.data_mode == "mock" for _, price in known) else "Оценка"
        note = f"{prefix} за группу и период: дорога и отель; трансфер не оценён (DEC-18)"
        if currency_unresolved:
            note += "; цены в разных валютах: общая сумма не вычислена без проверенного курса (DEC-17)"
        if missing:
            note += "; не найдено: " + ", ".join(missing)
        if duration is not None:
            note += f"; в пути туда {duration // 60} ч {duration % 60:02d} мин"
        return candidate.evolve(**{"estimated_cost": cost, "cost_note": note})

    # ------------------------------------------------------------------ поиск

    def _search(self, state: TripState, request: TripRequest) -> TripState:
        collector = _Collector()
        destination = self._resolve_place(
            request.destination.name if request.destination else None, request.request_id, collector
        )
        origin = self._resolve_place(
            request.origin.name if request.origin else None, request.request_id, collector
        )
        if destination is None or origin is None:
            collector.unchecked_sources.append(
                {
                    "source": "odin_demo_places" if not self.live_data else "open_meteo:geocode",
                    "reason": (
                        "город вне демо-справочника: Live-геокодинг в режиме mock не используется"
                        if not self.live_data
                        else "город не найден геокодером или геокодер недоступен"
                    ),
                }
            )
            collector.note_stop(STOP_PROVIDER)
            return self._finish_search(state, collector, request)
        if self.live_data and isinstance(origin, ResolvedPlace) and isinstance(destination, ResolvedPlace):
            # Места запроса уточняются по источнику: часовой пояс и координаты (DATA-MODE-02).
            request = request.evolve(origin=origin.contract(), destination=destination.contract())
            state = state.evolve(request=request)

        date_range = request.date_range
        if date_range is None:
            collector.unchecked_sources.append(
                {"source": "odin_mock", "reason": "даты не заданы: цены и наличие без дат не запрашиваются"}
            )
            collector.note_stop(STOP_BLOCKED)
            return self._finish_search(state, collector, request)

        adults = request.party.adults or 0
        children = list(request.party.children_ages)
        modes = [
            constraint.value for constraint in request.constraints if constraint.field == "transport_mode"
        ]
        modes_flat = [str(mode) for item in modes for mode in (item if isinstance(item, list) else [item])]

        # Волна 1: транспорт туда/обратно и гостиницы (независимые чтения).
        transport_arguments: dict[str, Any] = {
            "origin": origin.name,
            "destination": destination.name,
            "date": date_range.start.isoformat(),
            "return_date": date_range.end.isoformat() if request.return_trip is not False else None,
            "adults": adults,
            "children_ages": children,
            "modes": modes_flat,
            "ttl_minutes": self.limits.offer_ttl_minutes,
        }
        hotels_arguments: dict[str, Any] = {
            "city": destination.name,
            "check_in": date_range.start.isoformat(),
            "check_out": date_range.end.isoformat(),
            "adults": adults,
            "children_ages": children,
            "rooms": request.party.rooms or 1,
            "pets": bool(request.party.pets),
            "ttl_minutes": self.limits.offer_ttl_minutes,
        }
        if self.live_data:
            transport_arguments["origin_place"] = self._place_payload(origin)
            transport_arguments["destination_place"] = self._place_payload(destination)
            hotels_arguments["city_place"] = self._place_payload(destination)
        transport_request = self._tool_request(
            request_id=request.request_id, tool="search_transport", arguments=transport_arguments
        )
        hotels_request = self._tool_request(
            request_id=request.request_id, tool="search_hotels", arguments=hotels_arguments
        )
        transport_result = self._invoke(transport_request, collector)
        hotels_result = self._invoke(hotels_request, collector)
        if transport_result is not None and isinstance(transport_result.data, dict):
            collector.transport = list(transport_result.data.get("offers") or [])
        if hotels_result is not None and isinstance(hotels_result.data, dict):
            collector.hotel_offers = list(hotels_result.data.get("offers") or [])
            collector.hotels = list(hotels_result.data.get("hotels") or [])
            if hotels_result.status == "empty":
                collector.unchecked_sources.append(
                    {
                        "source": f"{hotels_result.provider}:search_hotels",
                        "reason": "источник не вернул вариантов по этим параметрам",
                    }
                )
        collector.candidates_seen = len(collector.transport) + len(collector.hotel_offers)

        # Волна 2: детали лучших K гостиниц (только Live) и трансфер для выбранного направления.
        if self.live_data and collector.hotel_offers:
            self._hotel_details(request, collector)
        if collector.hotel_offers:
            transfer_arguments: dict[str, Any] = {
                "city": destination.name,
                "directions": ["transfer_in", "transfer_out"],
                "ttl_minutes": self.limits.offer_ttl_minutes,
            }
            if self.live_data:
                transfer_arguments.update(self._transfer_points(collector))
            transfer_request = self._tool_request(
                request_id=request.request_id, tool="get_transfer_options", arguments=transfer_arguments
            )
            transfer_result = self._invoke(transfer_request, collector)
            if transfer_result is not None and isinstance(transfer_result.data, dict):
                collector.transfers = list(transfer_result.data.get("offers") or [])
        return self._finish_search(state, collector, request)

    @staticmethod
    def _hotel_facts(offer: Any, hotel: Any, request: TripRequest) -> OfferFacts:
        return OfferFacts(
            component_id=offer.offer_id,
            role="hotel",
            total_amount=offer.price_total.amount,
            currency=offer.currency,
            price_completeness=offer.price_total.total_completeness,
            budget_currency=request.budget.currency if request.budget else None,
            pets_allowed=hotel.pets_allowed if hotel else None,
            breakfast_included=offer.terms.breakfast_included,
            star_rating=hotel.star_rating if hotel else None,
            free_cancellation=offer.terms.free_cancellation,
            evidence=(f"{offer.source.provider}:{offer.source.tool}",),
        )

    def _hotel_details(self, request: TripRequest, collector: _Collector) -> None:
        """Волна 2 (план §2.4 п. 7): детали лучших K гостиниц — тарифы и наличие на даты по данным поставщика.

        Детали заменяют поисковые записи этих гостиниц; гостиница, о которой поставщик сообщил «нет номеров
        на даты», исключается из кандидатов с пометкой. Сбой деталей не ломает план: остаётся выдача поиска.
        """
        date_range = request.date_range
        live_offers = [offer for offer in collector.hotel_offers if offer.source.data_mode == "live"]
        if date_range is None or not live_offers:
            return
        hotel_by_id = {hotel.hotel_id: hotel for hotel in collector.hotels}
        required_ids = [c.constraint_id for c in request.constraints if c.requiredness == "required"]
        keys = {
            offer.offer_id: rank_key(
                offer.offer_id,
                aggregate_statuses(
                    evaluate_constraints(
                        request.constraints,
                        self._hotel_facts(offer, hotel_by_id.get(offer.hotel_id), request),
                    )
                ),
                required_ids,
            )
            for offer in live_offers
        }
        by_id = {offer.offer_id: offer for offer in live_offers}
        top: list[str] = []
        for key in sort_by_rank(keys.values()):
            hotel_id = by_id[key.canonical_id].hotel_id
            if hotel_id in hotel_by_id and hotel_id not in top:
                top.append(hotel_id)
            if len(top) >= self.limits.top_k:
                break
        if not top:
            return
        result = self._invoke(
            self._tool_request(
                request_id=request.request_id,
                tool="get_offer_details",
                arguments={
                    "hotels": [hotel_by_id[hotel_id].model_dump(mode="json") for hotel_id in top],
                    "check_in": date_range.start.isoformat(),
                    "check_out": date_range.end.isoformat(),
                    "adults": request.party.adults or 0,
                    "children_ages": list(request.party.children_ages),
                    "ttl_minutes": self.limits.offer_ttl_minutes,
                },
            ),
            collector,
        )
        if (
            result is None
            or result.status not in ("ok", "partial", "empty")
            or not isinstance(result.data, dict)
        ):
            return
        detailed = list(result.data.get("offers") or [])
        refined = {hotel.hotel_id: hotel for hotel in result.data.get("hotels") or []}
        unavailable = set(result.data.get("unavailable_hotel_ids") or [])
        replaced = {offer.hotel_id for offer in detailed} | unavailable
        kept = [offer for offer in collector.hotel_offers if offer.hotel_id not in replaced]
        collector.hotel_offers = [*detailed, *kept]
        collector.hotels = [refined.get(hotel.hotel_id, hotel) for hotel in collector.hotels]
        for hotel_id in sorted(unavailable):
            name = hotel_by_id[hotel_id].name if hotel_id in hotel_by_id else hotel_id
            notice = f"«{name}»: на эти даты у поставщика нет номеров (по деталям Tutu) — вариант исключён"
            if notice not in self._turn_notices:
                self._turn_notices.append(notice)

    @staticmethod
    def _transfer_points(collector: _Collector) -> dict[str, Any]:
        """Точки прибытия/отправления (вокзалы, аэропорты) и отели с координатами для оценки OSRM."""
        points: dict[str, dict[str, Any]] = {}
        for offer in collector.transport:
            if offer.source.data_mode == "mock":
                continue
            if offer.direction == "outbound":
                point_id, label, place = (
                    offer.terms.get("arrival_point_id"),
                    offer.terms.get("arrival_point"),
                    offer.arrival_place,
                )
                direction = "transfer_in"
            elif offer.direction == "return":
                point_id, label, place = (
                    offer.terms.get("departure_point_id"),
                    offer.terms.get("departure_point"),
                    offer.departure_place,
                )
                direction = "transfer_out"
            else:
                continue
            if not point_id or place is None or place.lat is None or place.lon is None:
                continue
            entry = points.setdefault(
                point_id,
                {
                    "point_id": point_id,
                    "label": label or place.name,
                    "lat": place.lat,
                    "lon": place.lon,
                    "directions": [],
                },
            )
            if direction not in entry["directions"]:
                entry["directions"].append(direction)
        hotels = [
            {"hotel_id": hotel.hotel_id, "name": hotel.name, "lat": hotel.geo["lat"], "lon": hotel.geo["lon"]}
            for hotel in collector.hotels
            if hotel.geo and "lat" in hotel.geo and "lon" in hotel.geo
        ][:10]
        return {"points": list(points.values())[:4], "hotels": hotels}

    @staticmethod
    def _plan_transfers(
        state: TripState,
        selected: dict[str, tuple[Any, list[ConstraintEvaluation], int]],
        unknowns: list[str],
    ) -> list[TravelOffer]:
        """Трансферы плана: для Live — только пара «выбранная точка прибытия ↔ выбранный отель»."""
        options = list(state.transfer_options)
        keyed = [item for item in options if "hotel_id" in item.parameters]
        if not keyed:
            return options
        hotel = selected["hotel"][0]
        chosen: list[TravelOffer] = []
        for direction, role, key in (
            ("transfer_in", "outbound", "arrival_point_id"),
            ("transfer_out", "return", "departure_point_id"),
        ):
            transport = selected.get(role)
            if transport is None:
                continue
            point = transport[0].terms.get(key)
            match = next(
                (
                    item
                    for item in keyed
                    if item.direction == direction
                    and item.parameters.get("hotel_id") == hotel.hotel_id
                    and item.parameters.get("point_id") == point
                ),
                None,
            )
            if match is not None:
                chosen.append(match)
            else:
                unknowns.append(f"время трансфера ({direction}) не оценено: нет координат точки или отеля")
        return chosen

    def _finish_search(self, state: TripState, collector: _Collector, request: TripRequest) -> TripState:
        stop_reason = collector.stop_reason or STOP_SOURCES
        termination = SearchTermination(
            reason=stop_reason,  # type: ignore[arg-type]
            checked_sources=sorted(set(collector.checked_sources)),
            unchecked_sources=[],  # type: ignore[arg-type]
            candidates_seen=collector.candidates_seen,
            confirmed_matches=0,
            unknown_candidates=collector.candidates_seen,
            constraints_unresolved=[],
            budget_used={},
            can_continue=stop_reason in (STOP_LIMITS, STOP_DEADLINE),
        )
        termination = termination.evolve(
            **{"unchecked_sources": [value for value in collector.unchecked_sources]}
        )
        return state.evolve(
            **{
                "transport_offers": collector.transport,
                "hotels": collector.hotels,
                "hotel_offers": collector.hotel_offers,
                "transfer_options": collector.transfers,
                "search_termination": termination,
                "status": "planned" if collector.hotel_offers or collector.transport else "searching",
                "history": [
                    *state.history,
                    self._history(
                        revision=state.revision,
                        request_id=request.request_id,
                        event="search_finished",
                        note=stop_reason,
                    ),
                ],
            }
        )

    # ------------------------------------------------------------------ план

    def _build_plan(self, state: TripState, request: TripRequest) -> TripState:
        """Сборка плана кодом (§2.4 п. 8): ранжирование RANK-01, итог и полнота, стыки, неизвестные."""
        required_ids = [c.constraint_id for c in request.constraints if c.requiredness == "required"]
        hotel_by_id = {hotel.hotel_id: hotel for hotel in state.hotels}
        candidates: dict[str, list[tuple[str, Any, list[ConstraintEvaluation]]]] = {}

        for offer in state.hotel_offers:
            hotel = hotel_by_id.get(offer.hotel_id)
            facts = self._hotel_facts(offer, hotel, request)
            candidates.setdefault("hotel", []).append(
                (offer.offer_id, offer, evaluate_constraints(request.constraints, facts))
            )
        for offer in state.transport_offers:
            facts = OfferFacts(
                component_id=offer.offer_id,
                role=offer.direction,
                total_amount=offer.price.amount,
                currency=offer.price.currency,
                price_completeness=offer.price.total_completeness,
                budget_currency=request.budget.currency if request.budget else None,
                transport_mode=offer.kind,
                pets_allowed=offer.pets_allowed,
                evidence=(f"{offer.source.provider}:{offer.source.tool}",),
            )
            candidates.setdefault(offer.direction, []).append(
                (offer.offer_id, offer, evaluate_constraints(request.constraints, facts))
            )

        selected: dict[str, tuple[Any, list[ConstraintEvaluation], int]] = {}
        ranked: dict[str, list[tuple[Any, list[ConstraintEvaluation], int]]] = {}
        for role, items in candidates.items():
            keys = {cid: rank_key(cid, aggregate_statuses(evals), required_ids) for cid, _, evals in items}
            by_id = {cid: (o, e) for cid, o, e in items}
            order = sort_by_rank(keys.values())
            ranked[role] = [(*by_id[k.canonical_id], k.rank_class) for k in order]
            chosen_id = state.selected_components.get(role)
            if chosen_id not in keys:
                chosen_id = order[0].canonical_id
            else:
                ranked.pop(role)  # явный выбор пользователя не заменяется ради бюджета
            offer, evals = by_id[chosen_id]
            selected[role] = (offer, evals, keys[chosen_id].rank_class)
        selected = _fit_required_budget(selected, ranked, request)

        # Бюджет поездки оценивается по итогу плана, а не по отдельным компонентам.
        evaluations = [
            evaluation
            for _, evals, _ in selected.values()
            for evaluation in evals
            if evaluation.constraint_id != "c-budget_total"
        ]

        prices: list[Price] = []
        component_ids: list[str] = []
        components: list[PlanComponent] = []
        for role in ("outbound", "hotel", "return"):
            if role not in selected:
                continue
            offer = selected[role][0]
            if role == "hotel":
                hotel = hotel_by_id.get(offer.hotel_id)
                price, kind, title = offer.price_total, "hotel", hotel.name if hotel else offer.offer_id
            else:
                price, kind, title = offer.price, offer.kind, offer.title
            prices.append(price)
            component_ids.append(role)
            components.append(
                PlanComponent(
                    component_id=role,
                    role=role,  # type: ignore[arg-type]
                    offer_id=offer.offer_id,
                    kind=kind,
                    title=title,
                    data_mode=offer.source.data_mode,
                )
            )
        transfer_unknowns: list[str] = []
        if "hotel" in selected:
            for transfer in self._plan_transfers(state, selected, transfer_unknowns):
                prices.append(transfer.price)
                component_ids.append(transfer.direction)
                components.append(
                    PlanComponent(
                        component_id=transfer.direction,
                        role=transfer.direction,  # type: ignore[arg-type]
                        offer_id=transfer.offer_id,
                        kind="transfer",
                        title=transfer.title,
                        data_mode=transfer.source.data_mode,
                    )
                )

        as_of = datetime.now(tz=UTC)
        component_modes = {component.data_mode for component in components}
        plan_mode = (
            (next(iter(component_modes)) if len(component_modes) == 1 else "mixed") if components else "mock"
        )
        if plan_mode in ("live", "replay"):
            plan_source = live_source(
                provider="odin_core",
                tool="build_plan",
                source_type="computed",
                data_mode=plan_mode,
                retrieved_at=as_of,
                external_id=f"plan-{state.trip_id}-{state.revision}",
            )
        else:
            plan_source = mock_source(
                "build_plan", f"plan-{state.trip_id}-{state.revision}", as_of, source_type="computed"
            )
        plan_id = f"{'P' if plan_mode in ('live', 'replay') else 'DEMO-P'}-{state.trip_id}-{state.revision}"
        # MIXED-CURRENCY-PLAN-01, DEC-17: разные валюты без проверенного курса — итог неизвестен, не HTTP 500.
        totals = sum_prices_or_unresolved(prices, component_ids)
        # PRICE-01, SCOPE-01: без найденного обязательного компонента итог не может быть полным.
        missing_roles = [role for role in ("hotel", "outbound") if role not in selected]
        if request.return_trip and "return" not in selected:
            missing_roles.append("return")
        if missing_roles and totals.completeness == "complete":
            totals = replace(
                totals,
                completeness="partial",
                unknown_reason="data_missing",
                partial_components=(*totals.partial_components, *missing_roles),
            )
        total = total_price(totals, plan_source, as_of)

        checks = self._connection_checks(selected, hotel_by_id)
        budget_evaluation = self._budget_evaluation(
            request, total, state, plan_id=plan_id, component_prices=prices
        )
        if budget_evaluation is not None:
            evaluations.append(budget_evaluation)

        unknowns: list[str] = list(transfer_unknowns)
        if totals.unknown_reason == "currency_unresolved":
            unknowns.append("стоимости в разных валютах: общий итог не вычислен без проверенного курса")
        if "hotel" in selected and state.transfer_options:
            unknowns.append("стоимость трансфера не подтверждена")
        for role, (offer, _, _) in selected.items():
            if judge_availability(offer.availability).status != "available":
                unknowns.append(f"наличие ({_role_title(role)}) не подтверждено источником")
        for role in ("hotel", "outbound"):
            if role not in selected:
                unknowns.append(f"компонент «{_role_title(role)}» не найден")
        if request.return_trip and "return" not in selected:
            unknowns.append(f"компонент «{_role_title('return')}» не найден")

        # COVERAGE-01: у каждого применимого условия есть оценка; потерянные условия видимы пользователю.
        report = coverage(request.constraints, evaluations)
        currency = request.budget.currency if request.budget else None
        labels = {
            c.constraint_id: _constraint_label(c, currency, mark_required=False) for c in request.constraints
        }
        unknowns.extend(f"условие «{labels.get(cid, cid)}» не оценено" for cid in report.missing)
        unknowns.extend(
            f"обязательное условие «{labels.get(cid, cid)}» не подтверждено"
            for cid in report.unknown_required
        )
        constraint_ids = [c.constraint_id for c in request.constraints]
        tradeoff_rows = [
            row.text()
            for row in tradeoffs(
                {cid: evals for cid, _, evals in candidates.get("hotel", [])}, constraint_ids
            )
            if row.better or row.worse or row.unknown
        ]

        modes = {component.component_id: component.data_mode for component in components}
        rank_classes = [rank for _, _, rank in selected.values()]
        plan = TripPlan(
            plan_id=plan_id,
            trip_revision=state.revision,
            components=components,
            total_price=total,
            completeness=total.total_completeness,
            connection_checks=checks,
            constraint_evaluations=evaluations,
            tradeoffs=tradeoff_rows,
            unknowns=sorted(set(unknowns)),
            sources=[plan_source],
            rank_class=max(rank_classes) if rank_classes else 2,  # type: ignore[arg-type]
            trip_data_mode=plan_mode,  # type: ignore[arg-type]
            data_mode_breakdown=modes,
            fully_agreed=False,
            relevance_note=RELEVANCE_NOTES[plan_mode],
        )
        warnings = set(state.warnings)
        if plan_mode in ("mock", "mixed"):
            warnings.add("ДЕМО-данные: не цена, не наличие и не расписание")
        return state.evolve(
            plan=plan,
            constraint_evaluations=evaluations,
            selected_components={role: offer.offer_id for role, (offer, _, _) in selected.items()},
            status="planned" if components else "searching",
            warnings=sorted(warnings),
            history=[
                *state.history,
                self._history(revision=state.revision, request_id=request.request_id, event="plan_built"),
            ],
        )

    def _connection_checks(
        self, selected: dict[str, tuple[Any, list[ConstraintEvaluation], int]], hotel_by_id: dict[str, Any]
    ) -> list[ConnectionCheck]:
        """Стыки CONNECTION-01/02: прибытие → заезд и выезд → обратный путь. Буферы не утверждены (DEC-20)."""
        if "hotel" not in selected:
            return []
        hotel_offer = selected["hotel"][0]
        hotel = hotel_by_id.get(hotel_offer.hotel_id)
        timezone_name = hotel.location.timezone if hotel else None
        checks: list[ConnectionCheck] = []
        if "outbound" in selected:
            outbound = selected["outbound"][0]
            checks.append(
                arrival_checkin_check(
                    check_id="cc-arrival-checkin",
                    from_component="outbound",
                    to_component="hotel",
                    arrival_at=outbound.arrival_at,
                    check_in_date=hotel_offer.check_in,
                    check_in_from=hotel.check_in_from if hotel else None,
                    timezone_name=timezone_name,
                    buffer_minutes=self.limits.buffers_minutes.get("after_train"),
                    buffer_status="proposed_not_approved",
                    sources=[f"{outbound.source.provider}:{outbound.source.tool}"],
                )
            )
        if "return" in selected:
            back = selected["return"][0]
            checks.append(
                checkout_departure_check(
                    check_id="cc-checkout-return",
                    from_component="hotel",
                    to_component="return",
                    check_out_date=hotel_offer.check_out,
                    check_out_until=hotel.check_out_until if hotel else None,
                    departure_at=back.departure_at,
                    timezone_name=timezone_name,
                    buffer_minutes=self.limits.buffers_minutes.get("before_train"),
                    buffer_status="proposed_not_approved",
                    sources=[f"{back.source.provider}:{back.source.tool}"],
                )
            )
        return checks

    # ------------------------------------------------------------------ корзина

    def _locked_head(self, trip_id: str, trip_revision: int) -> dict[str, Any]:
        head = self.storage.get_head(trip_id)
        if int(head["current_revision"]) != trip_revision:
            raise ConflictError(
                f"устаревшая ревизия: текущая {head['current_revision']}, получена {trip_revision}"
            )
        return head

    def open_cart(self, *, trip_id: str, trip_revision: int, request_id: str) -> Cart:
        """Корзина по текущему плану. Новая ревизия поездки фиксирует корзину (REPLAN-02)."""
        session_id = self.storage.get_head(trip_id)["session_id"]
        with (
            self.storage.session_lock(session_id),
            _span_scope(request_id=request_id, trip_id=trip_id, session_id=session_id),
        ):
            self._locked_head(trip_id, trip_revision)
            state = self.storage.get_state(trip_id)
            plan = state.plan
            if plan is None or not plan.components:
                raise CartError("план не собран: корзина недоступна")
            now = self._now()
            new_revision = state.revision + 1
            items = [self._cart_item(state, component) for component in plan.components]
            cart = build_cart(
                trip_id=trip_id,
                trip_revision=new_revision,
                items=items,
                total_price=plan.total_price,
                completeness=plan.completeness,
                significant_unknowns=self._significant_unknowns(state, now),
                created_at=now,
            )
            new_state = state.evolve(
                **{
                    "revision": new_revision,
                    "cart": cart,
                    "status": "cart",
                    "history": [
                        *state.history,
                        self._history(revision=new_revision, request_id=request_id, event="cart_opened"),
                    ],
                }
            )
            self.storage.append_revision(new_state, request_id, agreed=False)
            self.storage.save_cart(cart)
            self.storage.add_event(
                session_id, "cart_opened", {"cart_id": cart.cart_id, "revision": new_revision}
            )
            self.storage.audit(
                "cart_opened", request_id=request_id, trip_id=trip_id, payload={"cart_id": cart.cart_id}
            )
            return cart

    def get_cart(self, trip_id: str) -> Cart:
        self.storage.get_head(trip_id)
        cart = self.storage.get_cart_by_trip(trip_id)
        if cart is None:
            raise NotFoundError(f"корзина для поездки {trip_id} не создана")
        return cart

    def confirm_cart(
        self,
        *,
        cart_id: str,
        confirmation_id: str,
        trip_revision: int,
        purchase_consent: bool,
        risk_acknowledgement: bool,
        request_id: str,
    ) -> tuple[Cart, DemoReceipt, bool]:
        """Демо-подтверждение. Повтор с тем же ``confirmation_id`` возвращает тот же чек (ARCH-05)."""
        cart = self.storage.get_cart(cart_id)
        session_id = self.storage.get_head(cart.trip_id)["session_id"]
        with (
            self.storage.session_lock(session_id),
            _span_scope(request_id=request_id, trip_id=cart.trip_id, session_id=session_id),
        ):
            existing = self.storage.get_receipt_by_confirmation(confirmation_id)
            if existing is not None:
                if existing.cart_id != cart_id:
                    raise CartError("confirmation_id уже использован для другой корзины")
                self.storage.audit(
                    "demo_purchase_replayed",
                    request_id=request_id,
                    trip_id=cart.trip_id,
                    payload={"receipt_id": existing.receipt_id},
                )
                return self.storage.get_cart(cart_id), existing, True
            head = self.storage.get_head(cart.trip_id)
            if int(head["current_revision"]) != trip_revision or cart.trip_revision != trip_revision:
                raise ConflictError(
                    "ревизия поездки изменилась после формирования корзины: подтверждение недоступно (REPLAN-02)"
                )
            now = self._now()
            state = self.storage.get_state(cart.trip_id)
            # DEC-21: устаревшее после формирования корзины предложение не подтверждается молча —
            # нужна перепроверка (новый поиск) или новая корзина, где риск перечислен для согласия.
            unlisted = [
                risk for risk in self._stale_risks(state, now) if risk not in cart.significant_unknowns
            ]
            if unlisted:
                self.storage.audit(
                    "demo_purchase_rejected_stale",
                    request_id=request_id,
                    trip_id=cart.trip_id,
                    payload={"cart_id": cart_id, "stale": unlisted},
                )
                raise CartStaleError(
                    "предложения устарели после формирования корзины: "
                    + "; ".join(unlisted)
                    + ". Выполните перепроверку (новый поиск) или откройте корзину заново и "
                    "подтвердите риск отдельно (DEC-21)"
                )
            new_cart, receipt = confirm_cart(
                cart,
                confirmation_id=confirmation_id,
                trip_revision=trip_revision,
                purchase_consent=purchase_consent,
                risk_acknowledgement=risk_acknowledgement,
                now=now,
            )
            new_revision = state.revision + 1
            new_state = state.evolve(
                **{
                    "revision": new_revision,
                    "cart": new_cart,
                    "status": "purchased_demo",
                    "history": [
                        *state.history,
                        self._history(revision=new_revision, request_id=request_id, event="demo_purchased"),
                    ],
                }
            )
            self.storage.append_revision(new_state, request_id, agreed=True)
            self.storage.save_cart(new_cart)
            self.storage.save_receipt(receipt, confirmation_id)
            self.storage.add_event(session_id, "demo_purchased", {"receipt_id": receipt.receipt_id})
            self.storage.audit(
                "demo_purchase",
                request_id=request_id,
                trip_id=cart.trip_id,
                session_id=session_id,
                payload={"cart_id": cart_id, "receipt_id": receipt.receipt_id, "external_calls": 0},
            )
            return new_cart, receipt, False

    def decline_cart(self, *, cart_id: str, trip_revision: int, request_id: str) -> Cart:
        """Отказ: ничего не оформляется, чек не создаётся (UC-08)."""
        cart = self.storage.get_cart(cart_id)
        session_id = self.storage.get_head(cart.trip_id)["session_id"]
        with (
            self.storage.session_lock(session_id),
            _span_scope(request_id=request_id, trip_id=cart.trip_id, session_id=session_id),
        ):
            self._locked_head(cart.trip_id, trip_revision)
            state = self.storage.get_state(cart.trip_id)
            if cart.trip_revision != trip_revision or state.cart is None or state.cart.cart_id != cart_id:
                raise ConflictError("корзина не относится к текущей ревизии поездки (REPLAN-02)")
            declined = decline_cart(cart, now=self._now())
            new_revision = state.revision + 1
            new_state = state.evolve(
                **{
                    "revision": new_revision,
                    "cart": declined,
                    "status": "planned",
                    "history": [
                        *state.history,
                        self._history(revision=new_revision, request_id=request_id, event="cart_declined"),
                    ],
                }
            )
            self.storage.append_revision(new_state, request_id, agreed=False)
            self.storage.save_cart(declined)
            self.storage.add_event(session_id, "cart_declined", {"cart_id": cart_id})
            self.storage.audit(
                "cart_declined", request_id=request_id, trip_id=cart.trip_id, payload={"cart_id": cart_id}
            )
            return declined

    def get_receipt(self, receipt_id: str) -> DemoReceipt:
        return self.storage.get_receipt(receipt_id)

    # ------------------------------------------------------------------ выбор и пересборка

    def select(
        self, *, trip_id: str, trip_revision: int, selections: dict[str, str], request_id: str
    ) -> TripState:
        """Выбор варианта пользователем: только из найденных офферов, план пересобирается кодом."""
        session_id = self.storage.get_head(trip_id)["session_id"]
        with self.storage.session_lock(session_id):
            self._locked_head(trip_id, trip_revision)
            state = self.storage.get_state(trip_id)
            if state.request is None:
                raise CartError("нет запроса поездки: выбирать нечего")
            known = {offer.offer_id for offer in [*state.hotel_offers, *state.transport_offers]}
            unknown_ids = [offer_id for offer_id in selections.values() if offer_id not in known]
            if unknown_ids:
                raise CartError(f"неизвестные варианты: {', '.join(unknown_ids)}")
            new_state = state.evolve(
                **{
                    "revision": state.revision + 1,
                    "selected_components": dict(selections),
                    "cart": None,
                    "history": [
                        *state.history,
                        self._history(
                            revision=state.revision + 1,
                            request_id=request_id,
                            event=SELECTION_EVENT,
                            note=_selection_note(selections),
                        ),
                    ],
                }
            )
            new_state = self._build_plan(new_state, state.request)
            agreed = new_state.plan is not None and bool(new_state.plan.components)
            self.storage.append_revision(new_state, request_id, agreed=agreed)
            self.storage.add_event(session_id, "selection_applied", {"revision": new_state.revision})
            return new_state

    def replan(
        self, *, trip_id: str, trip_revision: int, change_set: StructuredForm, request_id: str
    ) -> TurnResult:
        """Явная пересборка по ``change_set`` (таблица REPLAN-01). Старая корзина теряет право подтверждения."""
        session_id = self.storage.get_head(trip_id)["session_id"]
        with (
            self.storage.session_lock(session_id),
            self._turn_lock,
            _span_scope(request_id=request_id, trip_id=trip_id, session_id=session_id),
        ):
            self._locked_head(trip_id, trip_revision)
            state = self.storage.get_state(trip_id)
            if state.request is None:
                raise CartError("нет исходного запроса: используйте сообщение")
            requested = change_set.model_dump(exclude_unset=True)
            merged = {**request_to_form(state.request).model_dump(), **requested}
            form = StructuredForm.model_validate(merged)
            self._start_turn(session_id, request_id, trip_id)
            try:
                outcome = interpret_structured_form(
                    form, session_id=session_id, request_id=request_id, demo_places=not self.live_data
                )
                return self._apply_replan(state, outcome, request_id=request_id, requested=requested)
            except Exception as exc:
                self._turn_failed(session_id, request_id, trip_id, exc, time.perf_counter())
                raise
            finally:
                self._end_turn()

    def _apply_replan(
        self,
        state: TripState,
        outcome: InterpretationOutcome,
        *,
        request_id: str,
        requested: dict[str, Any] | None = None,
    ) -> TurnResult:
        """Менеджер пересборки (UC-09, REPLAN-01, REPLAN-02, ARCH-05).

        - инвалидируются только зависимые роли по таблице REPLAN-01;
        - явно выбранные пользователем компоненты прочих ролей переносятся, только если оффер
          не устарел (DEC-21); устаревший оффер как действующий не переносится;
        - без инвалидации и при свежих офферах новый поиск не нужен — пересчитываются оценки и итог;
        - неуспешная пересборка не записывается: остаётся последняя согласованная ревизия.
        """
        assert state.request is not None
        old_form = request_to_form(state.request)
        new_form = request_to_form(outcome.request) if outcome.request is not None else old_form
        changes = detect_changes(old_form, new_form)
        table_changes, _ = split_change_set(changes)
        roles, reasons = invalidated_roles(table_changes)
        now = self._now()

        kept: dict[str, str] = {}
        recheck: dict[str, str] = {}
        for role, offer_id in self._explicit_selections(state).items():
            if role in roles:
                continue
            offer = self._offer_by_id(state, offer_id)
            if offer is None:
                continue
            if self._is_stale(offer, now)[0]:
                # Устаревший оффер не переносится как действующий: только если новый поиск вернёт его снова.
                recheck[role] = offer_id
                continue
            kept[role] = offer_id
        any_stale = bool(self._stale_risks(state, now)) or bool(recheck)
        search = bool(roles) or any_stale or state.plan is None

        groups = changed_groups(changes) if changes else []
        notes = [
            "Пересборка: изменено — " + (", ".join(groups) if groups else "ничего"),
            "заново проверены — "
            + (
                ", ".join(_role_title(role) for role in roles) if roles else "оценки и итог без нового поиска"
            ),
        ]
        if not roles and any_stale:
            notes[1] = "заново проверены — все компоненты: найденные предложения устарели (DEC-21)"

        base = state.evolve(**{"cart": None, "plan": None, "selected_components": {**kept, **recheck}})
        draft = self._compute_turn(base, outcome, request_id, notes=(), search=search, compose=False)
        selected = draft.state.selected_components
        rechecked = [role for role, offer_id in recheck.items() if selected.get(role) == offer_id]
        stale_dropped = [role for role in recheck if role not in rechecked]
        if kept:
            notes.append("сохранены выбранные вами — " + ", ".join(_role_title(r) for r in sorted(kept)))
        if rechecked:
            notes.append(
                "перепроверены и сохранены выбранные вами — " + ", ".join(map(_role_title, rechecked))
            )
        if stale_dropped:
            notes.append(
                "устаревшие предложения не перенесены как действующие — "
                + ", ".join(map(_role_title, stale_dropped))
            )
        if draft.state.plan is not None and draft.state.plan.components and not outcome.questions:
            text_notes = tuple(outcome.notes) if outcome.mode in ("free_text_llm", "free_text_replay") else ()
            text_out, verified = self._answer(
                self._plan_facts(draft.state, notes=(*tuple(notes), *text_notes))
            )
            draft.assistant_text, draft.facts_verified = text_out, verified
        new_request = draft.state.request
        failed = (
            bool(outcome.questions)
            or outcome.request is None
            or (
                new_request is not None
                and new_request.destination is not None
                and (draft.state.plan is None or not draft.state.plan.components)
            )
        )
        change_view = {key: str(value) for key, value in (requested or changes).items()}

        if failed:
            event = ReplanEvent(
                request_id=request_id,
                old_revision=state.revision,
                change_set=change_view,
                invalidated_components=list(roles),
                reused_components=[],
                new_revision=None,
                validation_result="failed",
                note=("; ".join(reasons) or "изменений нет")[:500],
            )
            self.storage.audit(
                "replan",
                request_id=request_id,
                trip_id=state.trip_id,
                session_id=state.session_id,
                payload=event.model_dump(mode="json"),
            )
            self.storage.add_event(state.session_id, "replan_failed", {"kept_revision": state.revision})
            reason = (
                "нужно уточнение"
                if outcome.questions
                else "план с новыми параметрами не собран — ни одного компонента не найдено"
            )
            failure_note = (
                f"Пересборка не выполнена: {reason}. "
                f"Последний согласованный план сохранён без изменений (ревизия {state.revision})"
            )
            termination = draft.state.search_termination
            facts = ComposerFacts(
                notes=(failure_note,),
                questions=tuple(question.question for question in outcome.questions),
                stop_reason=termination.reason if termination and not outcome.questions else None,
            )
            text_out, verified = self._answer(facts)
            self._audit_llm(state, request_id)
            return TurnResult(
                state=state,
                assistant_text=text_out,
                questions=draft.questions,
                stop_reason=facts.stop_reason,
                facts_verified=verified,
            )

        new_state = draft.state
        explicit_now = {
            role: offer_id for role, offer_id in {**kept, **recheck}.items() if selected.get(role) == offer_id
        }
        if search:
            reused = [role for role, offer_id in kept.items() if selected.get(role) == offer_id]
        else:
            reused = [component.role for component in (new_state.plan.components if new_state.plan else [])]
        new_state = new_state.evolve(
            **{
                "history": [
                    *new_state.history,
                    self._history(
                        revision=new_state.revision,
                        request_id=request_id,
                        event="replan",
                        note="; ".join(notes)[:500],
                    ),
                    self._history(
                        revision=new_state.revision,
                        request_id=request_id,
                        event=SELECTION_KEPT_EVENT,
                        note=_selection_note(explicit_now),
                    ),
                ]
            }
        )
        result = self._persist(
            new_state,
            request_id,
            assistant_text=draft.assistant_text,
            agreed=draft.agreed,
            questions=draft.questions,
            facts_verified=draft.facts_verified,
        )
        event = ReplanEvent(
            request_id=request_id,
            old_revision=state.revision,
            change_set=change_view,
            invalidated_components=list(roles),
            reused_components=reused,
            new_revision=new_state.revision,
            validation_result="ok" if draft.agreed else "partial",
            note=("; ".join(reasons) or "изменений нет")[:500],
        )
        self.storage.audit(
            "replan",
            request_id=request_id,
            trip_id=state.trip_id,
            session_id=state.session_id,
            payload=event.model_dump(mode="json"),
        )
        return result

    def _explicit_selections(self, state: TripState) -> dict[str, str]:
        """Явные выборы пользователя — из последней записи истории о выборе (REPLAN-02)."""
        for entry in reversed(state.history):
            if entry.event in (SELECTION_EVENT, SELECTION_KEPT_EVENT):
                return _parse_selection_note(entry.note)
        return {}

    # ------------------------------------------------------------------ служебное

    def _cart_item(self, state: TripState, component: PlanComponent) -> CartItem:
        price = self._component_price(state, component)
        informational = component.kind == "transfer"
        note = None
        if informational:
            note = "стоимость не подтверждена; оплачивается отдельно; в демо-покупку не входит"
        return CartItem(
            item_id=f"{component.component_id}-item",
            component_id=component.component_id,
            role=component.role,
            kind=component.kind,
            title=component.title,
            data_mode=component.data_mode,
            price=price,
            availability_status="unknown",
            informational=informational,
            note=note,
            source=price.source,
        )

    def _significant_unknowns(self, state: TripState, now: datetime) -> list[str]:
        """Существенные неизвестные для отдельного согласия с рисками (UC-08, DEC-21)."""
        assert state.plan is not None
        risks = list(state.plan.unknowns)
        if state.plan.trip_data_mode in ("mock", "mixed"):
            risks.append("ДЕМО-данные: цены, наличие и расписание не подтверждены (DEC-15)")
        elif state.plan.trip_data_mode == "replay":
            risks.append("Replay: цены и наличие на дату снимка, не текущие")
        if state.plan.completeness != "complete":
            risks.append("итоговая стоимость неполная")
        for check in state.plan.connection_checks:
            if check.status != "matched":
                risks.append(f"стык {check.from_component} → {check.to_component}: {check.status}")
        risks.extend(self._stale_risks(state, now))
        return sorted(set(risks))

    def _offer_by_id(self, state: TripState, offer_id: str) -> Any | None:
        for offer in [*state.hotel_offers, *state.transport_offers, *state.transfer_options]:
            if offer.offer_id == offer_id:
                return offer
        return None

    def _is_stale(self, offer: Any, now: datetime) -> tuple[bool, str]:
        validity = offer.validity
        return offer_staleness(validity.observed_at, validity.valid_until, now, self.limits.offer_ttl_minutes)

    def _stale_risks(self, state: TripState, now: datetime) -> list[str]:
        """Устаревшие компоненты плана (DEC-21, provisional TTL): формулировка детерминирована."""
        if state.plan is None:
            return []
        risks: list[str] = []
        for component in state.plan.components:
            offer = self._offer_by_id(state, component.offer_id)
            if offer is None:
                continue
            stale, reason = self._is_stale(offer, now)
            if stale:
                risks.append(
                    f"предложение «{component.title}» ({component.role}) устарело: {reason}; "
                    "требуется перепроверка или согласие с риском"
                )
        return risks

    def _budget_evaluation(
        self,
        request: TripRequest,
        total: Price,
        state: TripState,
        *,
        plan_id: str | None = None,
        component_prices: list[Price] | None = None,
    ) -> ConstraintEvaluation | None:
        """Бюджет поездки — по итогу плана (план §2.10 п. 4, §2.23)."""
        constraint = next((item for item in request.constraints if item.field == "budget_total"), None)
        if constraint is None or request.budget is None:
            return None
        status, deviation, reason = evaluate_budget(
            request.budget.amount,
            request.budget.currency,
            total.amount,
            total.currency,
            total.total_completeness,
        )
        currency_unresolved = total.unknown_reason == "currency_unresolved"
        if currency_unresolved and status == "unknown":
            reason = "стоимости в разных валютах: итог не вычислен без проверенного курса (DEC-17)"
            # MIXED-LOWER-BOUND-02: известные части в валюте бюджета — нижняя граница итога (суммы
            # неотрицательны, роли компонентов уникальны). Превышение доказано и без пересчёта других валют;
            # иначе итог остаётся неизвестным, но не matched.
            lower = known_amount_in_currency(component_prices or [], request.budget.currency)
            if lower is not None and lower > request.budget.amount:
                status = "violated"
                deviation = Deviation(
                    amount=lower - request.budget.amount,
                    unit=request.budget.currency,
                    description="известная часть в валюте бюджета превышает бюджет; другие валюты не учтены",
                )
                reason = (
                    "известная часть стоимости в валюте бюджета больше бюджета; "
                    "компоненты в других валютах не учтены (DEC-17)"
                )
        return ConstraintEvaluation(
            constraint_id=constraint.constraint_id,
            component_id=None,
            plan_id=f"DEMO-P-{state.trip_id}-{state.revision}",
            status=status,
            unknown_reason=(
                ("currency_unresolved" if currency_unresolved else "price_incomplete")
                if status == "unknown"
                else None
            ),
            evidence_refs=["computed:plan_total"],
            reason=reason,
            deviation=deviation,
        )

    def _tool_request(self, *, request_id: str, tool: str, arguments: dict[str, Any]) -> ToolRequest:
        deadline = datetime.now(tz=UTC) + timedelta(seconds=self.limits.tool_timeout_s)
        return ToolRequest(
            request_id=request_id,
            call_id=f"{request_id}:{tool}:{len(getattr(self.registry.call, 'log', []))}",
            tool_name=tool,  # type: ignore[arg-type]
            arguments=arguments,
            deadline=deadline,
            policy_scope="read_only",
        )

    def _invoke(self, request: ToolRequest, collector: _Collector | None = None) -> Any:
        """Вызов через Registry. Отказ, лимит, дедлайн и сбой фиксируются как причина остановки
        и непроверенный источник (SEARCH-STOP-01): это не доказательство отсутствия вариантов."""
        try:
            provider = self.registry.policy.check_logical(request.tool_name)
        except ToolDeniedError:
            provider = "registry"
        source = f"{provider}:{request.tool_name}"

        def unchecked(reason: str, stop: str) -> None:
            if collector is not None:
                collector.unchecked_sources.append({"source": source, "reason": reason})
                collector.note_stop(stop)

        try:
            result = self.registry.invoke(request)
        except ToolDeniedError as exc:
            logger.warning("инструмент запрещён: %s", exc.reason)
            self.storage.audit(
                "tool_denied", request_id=request.request_id, payload={"tool": request.tool_name}
            )
            unchecked(f"запрещено политикой: {exc.reason}", STOP_BLOCKED)
            return None
        except ToolBudgetExceeded:
            self.storage.audit("tool_budget_exceeded", request_id=request.request_id, payload={})
            unchecked("исчерпан бюджет вызовов за ход", STOP_LIMITS)
            return None
        except ToolDeadlineExceeded:
            self.storage.audit("tool_deadline", request_id=request.request_id, payload={})
            unchecked("истёк дедлайн хода", STOP_DEADLINE)
            return None
        if collector is not None:
            if result.status in ("error", "unavailable", "timeout"):
                unchecked(f"источник не ответил: {result.error_code}", STOP_PROVIDER)
            else:
                collector.checked_sources.append(source)
        for warning in result.warnings:
            if ("ДЕМО-вариант" in warning or "недоступен" in warning) and warning not in self._turn_notices:
                self._turn_notices.append(warning[:300])
        return result

    def _component_price(self, state: TripState, component: PlanComponent) -> Price:
        for offer in [*state.hotel_offers]:
            if offer.offer_id == component.offer_id:
                return offer.price_total
        for offer in [*state.transport_offers, *state.transfer_options]:
            if offer.offer_id == component.offer_id:
                return offer.price
        raise CartError(f"цена компонента {component.component_id} неизвестна")

    def _plan_facts(self, state: TripState, *, notes: tuple[str, ...] = ()) -> ComposerFacts:
        plan = state.plan
        request = state.request
        assert plan is not None and request is not None
        dates = None
        if request.date_range:
            dates = f"{request.date_range.start.isoformat()} — {request.date_range.end.isoformat()}"
        party = None
        if request.party.adults:
            children = request.party.children_ages
            party = _adults_text(request.party.adults) + (
                f", дети: {', '.join(str(age) for age in children)}" if children else ""
            )
        amount = plan.total_price.amount
        return ComposerFacts(
            destination=request.destination.name if request.destination else None,
            origin=request.origin.name if request.origin else None,
            dates=dates,
            party=party,
            known_amount=None if amount is None else format(amount, "f"),
            currency=plan.total_price.currency,
            completeness=plan.completeness,
            offers_count=len(state.hotel_offers) + len(state.transport_offers),
            unknowns=tuple(plan.unknowns),
            stop_reason=state.search_termination.reason if state.search_termination else None,
            notes=(*notes, *self._turn_notices[:4]),
            violations=tuple(
                dict.fromkeys(
                    evaluation.reason
                    for evaluation in plan.constraint_evaluations
                    if evaluation.status == "violated"
                )
            ),
            assumptions=tuple(
                f"{assumption.field}: {assumption.value} ({assumption.reason})"
                for assumption in request.assumptions
                if assumption.shown_with_price
            ),
            mode_note=MODE_NOTES.get(plan.trip_data_mode, MODE_NOTES["mock"]),
            selected=self._selected_lines(state),
            conditions=self._condition_lines(state),
        )

    def _selected_lines(self, state: TripState) -> tuple[str, ...]:
        """Состав плана словами: что выбрано, когда и за сколько (только данные предложений)."""
        assert state.plan is not None
        hotels = {hotel.hotel_id: hotel for hotel in state.hotels}
        order = {role: index for index, role in enumerate(ROLE_TITLES)}
        lines: list[str] = []
        for component in sorted(state.plan.components, key=lambda c: order.get(c.role, 99)):
            offer = self._offer_by_id(state, component.offer_id)
            title = _role_title(component.role).capitalize()
            mark = " [ДЕМО]" if component.data_mode == "mock" else ""
            if offer is None:
                lines.append(f"{title}: {component.title}{mark}")
            elif component.role == "hotel":
                hotel = hotels.get(offer.hotel_id)
                name = hotel.name if hotel else component.title
                stars = f" {hotel.star_rating}★" if hotel and hotel.star_rating else ""
                breakfast = {True: "с завтраком", False: "без завтрака"}.get(
                    offer.terms.breakfast_included, "завтрак не подтверждён"
                )
                lines.append(
                    f"{title}: {name}{stars}{mark}; {offer.check_in:%d.%m}–{offer.check_out:%d.%m}; "
                    f"{breakfast}; {_price_text(offer.price_total)}"
                )
            elif component.role in ("outbound", "return"):
                kind = _KIND_TITLES.get(offer.kind, offer.kind)
                when = ""
                if offer.departure_at and offer.arrival_at:
                    when = (
                        f"; отправление {offer.departure_at:%d.%m} в {offer.departure_at:%H:%M}, "
                        f"прибытие {offer.arrival_at:%d.%m} в {offer.arrival_at:%H:%M} (местное время)"
                    )
                lines.append(f"{title}: {kind} {offer.title}{mark}{when}; {_price_text(offer.price)}")
            else:
                minutes = f"; около {offer.duration_minutes} мин (оценка)" if offer.duration_minutes else ""
                lines.append(f"{title}: {offer.title}{mark}{minutes}; стоимость не оценивается")
        return tuple(lines)

    def _condition_lines(self, state: TripState) -> tuple[str, ...]:
        """Статус каждого условия пользователя по выбранным компонентам: худший из применимых."""
        assert state.plan is not None and state.request is not None
        worst: dict[str, str] = {}
        for evaluation in state.plan.constraint_evaluations:
            if evaluation.status not in _EVAL_RANK:
                continue
            current = worst.get(evaluation.constraint_id)
            if current is None or _EVAL_RANK[evaluation.status] > _EVAL_RANK[current]:
                worst[evaluation.constraint_id] = evaluation.status
        currency = state.request.budget.currency if state.request.budget else None
        lines = []
        for constraint in state.request.constraints:
            status = worst.get(constraint.constraint_id)
            if status is None:
                continue
            text = _EVAL_TEXT[status]
            if constraint.field == "budget_total" and status == "unknown":
                text = "не подтверждено: известна только часть стоимости"
            lines.append(f"{_constraint_label(constraint, currency)} — {text}")
        lines.extend(
            f"«{group.raw_text}» — не проверялось (не распознано)"
            for group in state.request.constraint_groups
            if group.operator == "unparsed" and group.raw_text
        )
        return tuple(lines)

    def _history(self, *, revision: int, request_id: str, event: str, note: str | None = None):
        from odin.contracts.plan import HistoryEntry

        return HistoryEntry(
            revision=revision, request_id=request_id, event=event, at=datetime.now(tz=UTC), note=note
        )

    def _persist(
        self,
        state: TripState,
        request_id: str,
        *,
        assistant_text: str,
        agreed: bool,
        questions: list[dict[str, Any]] | None = None,
        facts_verified: bool = True,
    ) -> TurnResult:
        self.storage.append_revision(state, request_id, agreed=agreed)
        self._audit_llm(state, request_id)
        self.storage.add_event(
            state.session_id,
            "turn_finished",
            {"revision": state.revision, "status": state.status, "request_id": request_id},
        )
        self.storage.audit(
            "turn_finished",
            request_id=request_id,
            trip_id=state.trip_id,
            session_id=state.session_id,
            payload={
                "revision": state.revision,
                "status": state.status,
                "stop_reason": state.search_termination.reason if state.search_termination else None,
                "facts_verified": facts_verified,
            },
        )
        agent = AgentResult(
            agent="orchestrator",
            status="ok" if facts_verified else "partial",
            structured_output={"revision": state.revision, "status": state.status},
            uncertainties=[] if facts_verified else ["ответ заменён шаблоном: проверка фактов не прошла"],
        )
        logger.info(
            "ход завершён",
            extra={"request_id": request_id, "trip_id": state.trip_id, "status": state.status},
        )
        return TurnResult(
            state=state,
            assistant_text=assistant_text or (agent.uncertainties[0] if agent.uncertainties else ""),
            questions=questions or [],
            stop_reason=state.search_termination.reason if state.search_termination else None,
            facts_verified=facts_verified,
        )

    # ------------------------------------------------------------------ диагностика

    def describe(self) -> dict[str, Any]:
        return {
            "data_mode": self.settings.data_mode,
            "llm": self.llm.describe(),
            "limits": {
                "turn_deadline_s": self.limits.turn_deadline_s,
                "calls_per_turn": self.limits.calls_per_turn,
                "waves": self.limits.waves,
                "top_k": self.limits.top_k,
            },
        }


# Stage 57: low-overhead, non-invasive phase timings; payload never includes user inputs.
# OBS-SPAN-FAIL-01: диагностика не меняет бизнес-результат и не маскирует исходное исключение.
# OBS-CONTEXT-03: атрибуция берётся из контекста конкретного запроса, а не из полей экземпляра.
_SPAN_CONTEXT: ContextVar[dict[str, str | None] | None] = ContextVar("odin_span_context", default=None)
_SPAN_KEYS = ("request_id", "trip_id", "session_id")


@contextmanager
def _span_scope(*, request_id: str | None, trip_id: str | None, session_id: str | None) -> Iterator[None]:
    """Привязка этапных записей к конкретному запросу; сбрасывается после операции."""
    token = _SPAN_CONTEXT.set({"request_id": request_id, "trip_id": trip_id, "session_id": session_id})
    try:
        yield
    finally:
        _SPAN_CONTEXT.reset(token)


def _span_ids(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, str | None]:
    """Идентификаторы из контекста и аргументов вызова: аргументы точнее и перекрывают контекст."""
    ids = dict(_SPAN_CONTEXT.get() or {})
    for key in _SPAN_KEYS:
        value = kwargs.get(key)
        if isinstance(value, str):
            ids[key] = value
    for value in args[1:]:
        if isinstance(value, ToolRequest):
            ids.setdefault("request_id", value.request_id)
        for key in _SPAN_KEYS:
            found = getattr(value, key, None)
            if isinstance(found, str) and not ids.get(key):
                ids[key] = found
    return ids


def _observed_stage(name: str, method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def timed(self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        status, error = "ok", None
        try:
            return method(self, *args, **kwargs)
        except BaseException as exc:
            status, error = "error", type(exc).__name__
            raise
        finally:
            ids = _span_ids(args, kwargs)
            for key in _SPAN_KEYS:  # запасной путь: контекст прошлых версий и прямые вызовы в тестах
                if not ids.get(key):
                    fallback = getattr(self, f"_observed_{key}", None)
                    if isinstance(fallback, str):
                        ids[key] = fallback
            if ids.get("request_id"):
                try:
                    self.storage.audit(
                        "pipeline_span",
                        request_id=ids["request_id"],
                        trip_id=ids.get("trip_id"),
                        session_id=ids.get("session_id"),
                        payload={
                            "stage": name,
                            "status": status,
                            "error_code": error,
                            "duration_ms": int((time.perf_counter() - started) * 1000),
                        },
                    )
                except Exception as exc:  # телеметрия не меняет результат и не маскирует ошибку
                    logger.warning("pipeline_span not recorded (%s): %s", name, type(exc).__name__)

    return timed


for _stage in (
    "_interpret",
    "_compute_turn",
    "_search",
    "_build_plan",
    "_connection_checks",
    "_invoke",
    "_persist",
    "_apply_replan",
    "open_cart",
    "confirm_cart",
    "decline_cart",
):
    if hasattr(Orchestrator, _stage):
        setattr(Orchestrator, _stage, _observed_stage(_stage, getattr(Orchestrator, _stage)))
