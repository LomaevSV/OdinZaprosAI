"""Модели JSON API (API-01, CONTRACT-03). Пути и методы закрепляются снимком OpenAPI."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from odin.contracts.common import Contract, Currency, Money
from odin.contracts.plan import Cart, ComponentRole, DemoReceipt, TripState, TripStatus
from odin.contracts.trip import Ambiguity, PetInfo

RequestId = str


class StructuredForm(Contract):
    """Структурированный ввод (режим без LLM, DEC-08 вариант «а»).

    Флаги ``*_required`` — явный выбор пользователя; по умолчанию условие ``preferred`` (CONSTRAINT-01).
    """

    origin: str | None = Field(default=None, max_length=120)
    destination: str | None = Field(default=None, max_length=120)
    date_from: date | None = None
    date_to: date | None = None
    adults: int | None = Field(default=None, ge=1, le=9)
    children_ages: list[int] = Field(default_factory=list, max_length=8)
    return_trip: bool | None = None
    transport_modes: list[Literal["rail", "flight"]] = Field(default_factory=list)
    transport_modes_required: bool = False
    budget_amount: Money | None = None
    budget_currency: Currency | None = None
    budget_required: bool = False
    pets: list[PetInfo] = Field(default_factory=list, max_length=3)
    pets_required: bool = False
    breakfast: bool | None = None
    breakfast_required: bool = False
    stars_min: int | None = Field(default=None, ge=1, le=5)
    stars_required: bool = False
    free_cancellation: bool | None = None
    free_cancellation_required: bool = False
    # INT-007: мягкое пожелание к типу номера (не фильтр).
    room_type: Literal["family"] | None = None
    wishes_tags: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _rules(self) -> StructuredForm:
        if self.budget_amount is not None and self.budget_currency is None:
            raise ValueError("для бюджета укажите валюту")
        if any(a < 0 or a > 17 for a in self.children_ages):
            raise ValueError("возраст ребёнка должен быть от 0 до 17")
        return self


class ChoiceAnswer(Contract):
    """Выбор предложенного варианта нажатием (DATE-003). Сервер сверяет вариант с состоянием поездки."""

    ambiguity_id: str = Field(min_length=1, max_length=64)
    option_id: str = Field(min_length=1, max_length=64)
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def _order(self) -> ChoiceAnswer:
        if self.date_to <= self.date_from:
            raise ValueError("дата возвращения должна быть позже даты отъезда")
        return self


class MessageRequest(Contract):
    request_id: RequestId = Field(min_length=8, max_length=64)
    trip_revision: int = Field(ge=0)
    text: str | None = Field(default=None, max_length=4000)
    form: StructuredForm | None = None
    choice: ChoiceAnswer | None = None

    @model_validator(mode="after")
    def _one(self) -> MessageRequest:
        given = [self.form is not None, bool(self.text and self.text.strip()), self.choice is not None]
        if not any(given):
            raise ValueError("нужен текст сообщения, структурированная форма или выбор варианта")
        if self.choice is not None and sum(given) > 1:
            raise ValueError("выбор варианта передаётся без текста и формы")
        return self


class SessionResponse(Contract):
    session_id: str
    trip_id: str
    trip_revision: int
    created_at: AwareDatetime
    llm_mode: str
    data_mode: str
    state: TripState
    # Уведомление о передаче текстов LLM-провайдеру (только при llm_mode=live).
    privacy_notice: str | None = None


class TurnResponse(Contract):
    request_id: str
    session_id: str
    trip_id: str
    trip_revision: int
    status: TripStatus
    input_mode: Literal["structured_form", "free_text_llm", "free_text_replay", "llm_unavailable", "choice"]
    assistant_text: str
    questions: list[Ambiguity] = Field(default_factory=list)
    state: TripState


class SelectionRequest(Contract):
    request_id: RequestId = Field(min_length=8, max_length=64)
    trip_revision: int = Field(ge=0)
    selections: dict[ComponentRole, str]


class ReplanRequest(Contract):
    request_id: RequestId = Field(min_length=8, max_length=64)
    trip_revision: int = Field(ge=0)
    change_set: StructuredForm


class CartRequest(Contract):
    request_id: RequestId = Field(min_length=8, max_length=64)
    trip_revision: int = Field(ge=0)


class ConfirmRequest(Contract):
    """Два раздельных однозначных пункта согласия (UC-08)."""

    request_id: RequestId = Field(min_length=8, max_length=64)
    trip_revision: int = Field(ge=0)
    confirmation_id: str = Field(min_length=8, max_length=64)
    purchase_consent: bool
    risk_acknowledgement: bool


class DeclineRequest(Contract):
    request_id: RequestId = Field(min_length=8, max_length=64)
    trip_revision: int = Field(ge=0)


class CartResponse(Contract):
    trip_id: str
    trip_revision: int
    cart: Cart


class ConfirmResponse(Contract):
    receipt: DemoReceipt
    cart: Cart
    idempotent_replay: bool


class ProviderHealth(Contract):
    provider_id: str
    approval_status: str
    live_enabled: bool
    reason: str


class HealthResponse(Contract):
    status: Literal["ok", "degraded"]
    version: str
    schema_version: str
    data_mode: str
    llm_mode: str
    database: Literal["ok", "error"]
    providers: list[ProviderHealth]


class ErrorResponse(Contract):
    error_code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SessionEvent(Contract):
    seq: int
    at: AwareDatetime
    event: str
    payload: dict[str, Any]
