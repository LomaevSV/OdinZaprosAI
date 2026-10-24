"""Оркестратор — детерминированный автомат хода (ARCH-01, ARCH-02, план §2.4, §2.23).

Единственный владелец шагов: политика уточнений, волны поиска, лимиты, ``stop_reason``, атомарная
запись ревизии, аудит и события SSE. Домены и LLM не вызывают инструменты напрямую.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
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
from odin.contracts.api import StructuredForm
from odin.contracts.plan import CartItem
from odin.core.cart import CartError, CartStaleError, build_cart, confirm_cart, decline_cart
from odin.core.clarify import detect_changes, merge_answer
from odin.core.constraints import (
    OfferFacts,
    aggregate_statuses,
    evaluate_constraints,
    rank_key,
    sort_by_rank,
)
from odin.core.money import sum_prices, total_price
from odin.core.replan import changed_groups, invalidated_roles, split_change_set
from odin.core.tree import coverage, tradeoffs
from odin.core.trip import (
    arrival_checkin_check,
    checkout_departure_check,
    evaluate_budget,
    judge_availability,
    offer_staleness,
)
from odin.llm.composer import ComposerFacts, money_text, safe_answer
from odin.llm.gateway import LlmGateway
from odin.llm.interpreter import (
    InterpretationOutcome,
    interpret_structured_form,
    refuse_free_text,
    request_to_form,
)
from odin.logging_setup import get_logger
from odin.storage import ConflictError, NotFoundError, Storage
from odin.tools.mock_data import mock_source, normalize_city
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


@dataclass(slots=True)
class TurnResult:
    state: TripState
    assistant_text: str
    questions: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    facts_verified: bool = True


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

    def _now(self) -> datetime:
        return self.clock()

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
    ) -> TurnResult:
        session = self.storage.get_session(session_id)
        trip_id = session["trip_id"]
        with self.storage.session_lock(session_id):
            self._locked_head(trip_id, trip_revision)
            current = self.storage.get_state(trip_id)
            self._start_turn(session_id, request_id, trip_id)
            outcome = self._interpret(
                session_id=session_id, request_id=request_id, text=text, form=form, previous=current
            )
            # Детектор изменений: новое сообщение при собранном плане — пересборка (UC-09, REPLAN-01).
            has_plan = current.plan is not None and bool(current.plan.components)
            if has_plan and current.request is not None and not outcome.free_text_refused:
                assert current.request is not None
                if outcome.questions or (
                    outcome.request is not None
                    and detect_changes(request_to_form(current.request), request_to_form(outcome.request))
                ):
                    return self._apply_replan(current, outcome, request_id=request_id)
            return self._run_turn(current, outcome, request_id)

    def _start_turn(self, session_id: str, request_id: str, trip_id: str) -> None:
        self.registry.start_turn(datetime.now(tz=UTC) + timedelta(seconds=self.limits.turn_deadline_s))
        self.llm.start_turn()
        self.storage.add_event(session_id, "turn_started", {"request_id": request_id})
        self.storage.audit("turn_started", request_id=request_id, trip_id=trip_id, session_id=session_id)

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
    ) -> _TurnDraft:
        """Вычисление хода без записи: запись выполняет вызывающий (неудачная пересборка не пишется)."""
        state = current.evolve(**{"revision": current.revision + 1})
        if outcome.request is not None:
            state = state.evolve(**{"request": outcome.request})

        if outcome.free_text_refused:
            state = state.evolve(**{"status": "awaiting_clarification"})
            return _TurnDraft(state, " ".join(outcome.notes))

        if outcome.questions:
            state = state.evolve(**{"status": "awaiting_clarification"})
            facts = ComposerFacts(questions=tuple(question.question for question in outcome.questions))
            text_out, verified = safe_answer(facts)
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
            text_out, verified = safe_answer(ComposerFacts(candidates=names))
            return _TurnDraft(state, text_out, facts_verified=verified)

        if search:
            state = state.evolve(**{"status": "searching"})
            state = self._search(state, request)
        state = self._build_plan(state, request)
        text_out, verified = safe_answer(self._plan_facts(state, notes=notes))
        agreed = state.plan is not None and bool(state.plan.components)
        return _TurnDraft(state, text_out, agreed=agreed, facts_verified=verified)

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
        answering = (
            previous is not None
            and previous.status == "awaiting_clarification"
            and previous.request is not None
        )

        def interpret(value: StructuredForm) -> InterpretationOutcome:
            if answering:
                assert previous is not None and previous.request is not None
                value = merge_answer(request_to_form(previous.request), value)
            return interpret_structured_form(value, session_id=session_id, request_id=request_id)

        if form is not None:
            return interpret(form)
        if text:
            result = self.llm.complete_structured(
                role="interpreter",
                text=text,
                reference_date=datetime.now(tz=UTC).date().isoformat(),
                schema_version="1.0.0",
            )
            if result.status == "ok" and isinstance(result.structured_output, dict):
                try:
                    form_from_replay = StructuredForm.model_validate(result.structured_output)
                except ValueError:
                    return refuse_free_text(session_id=session_id, request_id=request_id)
                outcome = interpret(form_from_replay)
                return InterpretationOutcome(
                    request=outcome.request,
                    mode="free_text_replay",
                    questions=outcome.questions,
                    notes=[*outcome.notes, *result.uncertainties],
                    decisions=outcome.decisions,
                )
            return refuse_free_text(session_id=session_id, request_id=request_id)
        return refuse_free_text(session_id=session_id, request_id=request_id)

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
        origin = normalize_city(request.origin.name if request.origin else None)
        if date_range is None or not adults:
            return candidates
        if origin is None:
            return [
                candidate.evolve(
                    **{"cost_note": "стоимость не оценена: город отправления вне демо-справочника"}
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
        totals = sum_prices([price for _, price in known], [role for role, _ in known])
        # Трансфер не оценивается (DEC-18): итог не может быть полным.
        totals = replace(
            totals,
            completeness="partial",
            unknown_reason=totals.unknown_reason or "price_incomplete",
            partial_components=(*totals.partial_components, *missing, "transfer"),
        )
        cost = total_price(totals, source, as_of)
        duration = outbound.duration_minutes if outbound else None
        note = "ДЕМО-оценка за группу и период: дорога и отель; трансфер не оценён (DEC-18)"
        if missing:
            note += "; не найдено: " + ", ".join(missing)
        if duration is not None:
            note += f"; в пути туда {duration // 60} ч {duration % 60:02d} мин"
        return candidate.evolve(**{"estimated_cost": cost, "cost_note": note})

    # ------------------------------------------------------------------ поиск

    def _search(self, state: TripState, request: TripRequest) -> TripState:
        collector = _Collector()
        destination = normalize_city(request.destination.name if request.destination else None)
        origin = normalize_city(request.origin.name if request.origin else None)
        if destination is None or origin is None:
            collector.unchecked_sources.append(
                {
                    "source": "odin_demo_places",
                    "reason": "город вне демо-справочника: Live-геокодинг в S1 недоступен",
                }
            )
            collector.note_stop(STOP_PROVIDER)
            return self._finish_search(state, collector, request)

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
        transport_request = self._tool_request(
            request_id=request.request_id,
            tool="search_transport",
            arguments={
                "origin": origin.name,
                "destination": destination.name,
                "date": date_range.start.isoformat(),
                "return_date": date_range.end.isoformat() if request.return_trip is not False else None,
                "adults": adults,
                "children_ages": children,
                "modes": modes_flat,
                "ttl_minutes": self.limits.offer_ttl_minutes,
            },
        )
        hotels_request = self._tool_request(
            request_id=request.request_id,
            tool="search_hotels",
            arguments={
                "city": destination.name,
                "check_in": date_range.start.isoformat(),
                "check_out": date_range.end.isoformat(),
                "adults": adults,
                "children_ages": children,
                "rooms": request.party.rooms or 1,
                "pets": bool(request.party.pets),
                "ttl_minutes": self.limits.offer_ttl_minutes,
            },
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
                        "source": "odin_mock:search_hotels",
                        "reason": "источник не вернул вариантов по этим параметрам",
                    }
                )
        collector.candidates_seen = len(collector.transport) + len(collector.hotel_offers)

        # Волна 2: трансфер для выбранного направления.
        if collector.hotel_offers:
            transfer_request = self._tool_request(
                request_id=request.request_id,
                tool="get_transfer_options",
                arguments={
                    "city": destination.name,
                    "directions": ["transfer_in", "transfer_out"],
                    "ttl_minutes": self.limits.offer_ttl_minutes,
                },
            )
            transfer_result = self._invoke(transfer_request, collector)
            if transfer_result is not None and isinstance(transfer_result.data, dict):
                collector.transfers = list(transfer_result.data.get("offers") or [])
        return self._finish_search(state, collector, request)

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
            facts = OfferFacts(
                component_id=offer.offer_id,
                role="hotel",
                total_amount=offer.price_total.amount,
                currency=offer.currency,
                price_completeness=offer.price_total.total_completeness,
                pets_allowed=hotel.pets_allowed if hotel else None,
                breakfast_included=offer.terms.breakfast_included,
                star_rating=hotel.star_rating if hotel else None,
                free_cancellation=offer.terms.free_cancellation,
                evidence=(f"{offer.source.provider}:{offer.source.tool}",),
            )
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
                transport_mode=offer.kind,
                pets_allowed=offer.pets_allowed,
                evidence=(f"{offer.source.provider}:{offer.source.tool}",),
            )
            candidates.setdefault(offer.direction, []).append(
                (offer.offer_id, offer, evaluate_constraints(request.constraints, facts))
            )

        selected: dict[str, tuple[Any, list[ConstraintEvaluation], int]] = {}
        for role, items in candidates.items():
            keys = {cid: rank_key(cid, aggregate_statuses(evals), required_ids) for cid, _, evals in items}
            chosen_id = state.selected_components.get(role)
            if chosen_id not in keys:
                chosen_id = sort_by_rank(keys.values())[0].canonical_id
            offer, evals = next((o, e) for cid, o, e in items if cid == chosen_id)
            selected[role] = (offer, evals, keys[chosen_id].rank_class)

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
        if "hotel" in selected:
            for transfer in state.transfer_options:
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
        plan_source = mock_source(
            "build_plan", f"plan-{state.trip_id}-{state.revision}", as_of, source_type="computed"
        )
        totals = sum_prices(prices, component_ids)
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
        budget_evaluation = self._budget_evaluation(request, total, state)
        if budget_evaluation is not None:
            evaluations.append(budget_evaluation)

        unknowns: list[str] = []
        if "hotel" in selected and state.transfer_options:
            unknowns.append("стоимость трансфера не подтверждена")
        for role, (offer, _, _) in selected.items():
            if judge_availability(offer.availability).status != "available":
                unknowns.append(f"наличие ({role}) не подтверждено источником")
        for role in ("hotel", "outbound"):
            if role not in selected:
                unknowns.append(f"компонент «{role}» не найден")
        if request.return_trip and "return" not in selected:
            unknowns.append("компонент «return» не найден")

        # COVERAGE-01: у каждого применимого условия есть оценка; потерянные условия видимы пользователю.
        report = coverage(request.constraints, evaluations)
        unknowns.extend(f"условие {cid} не оценено" for cid in report.missing)
        unknowns.extend(f"обязательное условие {cid} не подтверждено" for cid in report.unknown_required)
        constraint_ids = [c.constraint_id for c in request.constraints]
        tradeoff_rows = [
            row.text()
            for row in tradeoffs(
                {cid: evals for cid, _, evals in candidates.get("hotel", [])}, constraint_ids
            )
            if row.better or row.worse or row.unknown
        ]

        modes = {component.component_id: component.data_mode for component in components}
        mode_set = set(modes.values())
        trip_mode = next(iter(mode_set)) if len(mode_set) == 1 else "mixed"
        rank_classes = [rank for _, _, rank in selected.values()]
        plan = TripPlan(
            plan_id=f"DEMO-P-{state.trip_id}-{state.revision}",
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
            trip_data_mode=trip_mode if components else "mock",  # type: ignore[arg-type]
            data_mode_breakdown=modes,
            fully_agreed=False,
            relevance_note="ДЕМО-данные: наличие и часть цен не подтверждены",
        )
        return state.evolve(
            plan=plan,
            constraint_evaluations=evaluations,
            selected_components={role: offer.offer_id for role, (offer, _, _) in selected.items()},
            status="planned" if components else "searching",
            warnings=sorted({*state.warnings, "ДЕМО-данные: не цена, не наличие и не расписание"}),
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
        with self.storage.session_lock(session_id):
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
        with self.storage.session_lock(session_id):
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
        with self.storage.session_lock(session_id):
            self._locked_head(cart.trip_id, trip_revision)
            declined = decline_cart(cart, now=self._now())
            state = self.storage.get_state(cart.trip_id)
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
        with self.storage.session_lock(session_id):
            self._locked_head(trip_id, trip_revision)
            state = self.storage.get_state(trip_id)
            if state.request is None:
                raise CartError("нет исходного запроса: используйте сообщение")
            requested = change_set.model_dump(exclude_unset=True)
            merged = {**request_to_form(state.request).model_dump(), **requested}
            form = StructuredForm.model_validate(merged)
            self._start_turn(session_id, request_id, trip_id)
            outcome = interpret_structured_form(form, session_id=session_id, request_id=request_id)
            return self._apply_replan(state, outcome, request_id=request_id, requested=requested)

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
            "заново проверены — " + (", ".join(roles) if roles else "оценки и итог без нового поиска"),
        ]
        if not roles and any_stale:
            notes[1] = "заново проверены — все компоненты: найденные предложения устарели (DEC-21)"

        base = state.evolve(**{"cart": None, "plan": None, "selected_components": {**kept, **recheck}})
        draft = self._compute_turn(base, outcome, request_id, notes=(), search=search)
        selected = draft.state.selected_components
        rechecked = [role for role, offer_id in recheck.items() if selected.get(role) == offer_id]
        stale_dropped = [role for role in recheck if role not in rechecked]
        if kept:
            notes.append("сохранены выбранные вами — " + ", ".join(sorted(kept)))
        if rechecked:
            notes.append("перепроверены и сохранены выбранные вами — " + ", ".join(rechecked))
        if stale_dropped:
            notes.append("устаревшие предложения не перенесены как действующие — " + ", ".join(stale_dropped))
        if draft.state.plan is not None and draft.state.plan.components and not outcome.questions:
            text_out, verified = safe_answer(self._plan_facts(draft.state, notes=tuple(notes)))
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
            text_out, verified = safe_answer(facts)
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
        if state.plan.trip_data_mode != "live":
            risks.append("ДЕМО-данные: цены, наличие и расписание не подтверждены (DEC-15)")
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
        self, request: TripRequest, total: Price, state: TripState
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
        return ConstraintEvaluation(
            constraint_id=constraint.constraint_id,
            component_id=None,
            plan_id=f"DEMO-P-{state.trip_id}-{state.revision}",
            status=status,
            unknown_reason="price_incomplete" if status == "unknown" else None,
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
        source = f"odin_mock:{request.tool_name}"

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
        return result

    def _component_price(self, state: TripState, component: PlanComponent) -> Price:
        for offer in [*state.hotel_offers]:
            if offer.offer_id == component.offer_id:
                return offer.price_total
        for offer in [*state.transport_offers, *state.transfer_options]:
            if offer.offer_id == component.offer_id:
                return offer.price
        assert state.plan is not None
        if state.plan.total_price is not None:
            return state.plan.total_price
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
            party = f"{request.party.adults} взрослых" + (
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
            notes=notes,
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
        )

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
        self.storage.add_event(
            state.session_id, "turn_finished", {"revision": state.revision, "status": state.status}
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
