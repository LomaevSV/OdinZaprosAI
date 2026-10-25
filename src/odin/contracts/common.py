"""Общие типы нормативных контрактов (ТЗ 0.7 §7, CONTRACT-01..03).

Единственный нормативный набор моделей: JSON Schema и OpenAPI публикуются только из этих классов.
Неизвестные значения — ``null`` или типизированный ``unknown``; выдуманных значений по умолчанию нет.
Деньги — ``Decimal``, на границе JSON — строка. Даты-время — только с часовым поясом.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------- базовые классы


class Contract(BaseModel):
    """Строгая модель: лишние поля запрещены."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    def evolve(self, **changes: Any) -> Any:
        """Копия с изменениями и полной повторной валидацией (в отличие от ``model_copy``).

        Все инварианты модели проверяются заново; некорректное состояние не может появиться.
        """
        data = {name: getattr(self, name) for name in type(self).model_fields}
        data.update(changes)
        return type(self).model_validate(data)


class VersionedContract(Contract):
    """Контракт с обязательной версией схемы (ТЗ §7: «Все контракты содержат schema_version»)."""

    schema_version: Literal["1.0.0"] = SCHEMA_VERSION


# ---------------------------------------------------------------- деньги


def _money_before(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("денежное значение не может быть логическим")
    if isinstance(value, float):
        raise ValueError("денежное значение нельзя передавать как float; используйте строку (PRICE-01)")
    if isinstance(value, (str, int, Decimal)):
        # MONEY-001: явная граница 12 целых разрядов (max_digits=14, decimal_places=2) — не зависит от того,
        # как версия pydantic считает разряды у «1000000000000.00» / «1E+12».
        try:
            number = Decimal(str(value))
            if number.is_finite() and abs(number) >= Decimal("1000000000000"):
                raise ValueError("денежное значение превышает 12 целых разрядов")
        except InvalidOperation:
            pass  # некорректную строку отклонит схема Decimal
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(_money_before),
    Field(max_digits=14, decimal_places=2, ge=Decimal("0")),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": r"^\d+(\.\d{1,2})?$", "description": "Decimal строкой"}),
]

SignedMoney = Annotated[
    Decimal,
    BeforeValidator(_money_before),
    Field(max_digits=14, decimal_places=2),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d{1,2})?$", "description": "Decimal строкой"}),
]

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="Код ISO 4217")]
HttpUrlStr = Annotated[str, Field(pattern=r"^https?://[^\s]{1,2000}$", max_length=2048)]


def _check_tz(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"неизвестный часовой пояс IANA: {value}") from exc
    return value


TimeZoneName = Annotated[str, BeforeValidator(_check_tz)]

# ---------------------------------------------------------------- перечисления

DataMode = Literal["live", "replay", "mock"]
VerificationStatus = Literal["verified", "unverified", "conflicting", "stale"]
Completeness = Literal["complete", "partial", "unknown"]
EvaluationStatus = Literal["matched", "violated", "unknown", "not_applicable"]
AvailabilityStatus = Literal["available", "unavailable", "unknown"]
EvidenceLevel = Literal["search_listing", "offer_details", "provider_checked", "historical", "mock", "none"]
UnknownReason = Literal[
    "not_provided_by_source",
    "source_error",
    "timeout",
    "budget_exhausted",
    "not_requested",
    "estimate_only",
    "not_supported_field",
    "mock_data",
    "currency_unresolved",
    "price_incomplete",
    "buffer_not_approved",
    "decision_pending",
    "data_missing",
    "stale",
]
PriceScope = Literal[
    "per_person",
    "per_seat",
    "party_total",
    "per_night",
    "stay_total",
    "per_room_stay",
    "trip_total",
    "unknown",
]
SourceType = Literal[
    "mcp",
    "rest_api",
    "local_dataset",
    "mock_generator",
    "editorial",
    "user_input",
    "computed",
    "llm",
]

DEMO_PURCHASE_NOTICE = "Демонстрационная покупка. Реального бронирования и списания средств нет"


# ---------------------------------------------------------------- SourceReference


class SourceReference(VersionedContract):
    """Происхождение существенного факта (DATA-MODE-02, CONTRACT-01).

    ``verification_status=verified`` означает подтверждение конкретным источником, а не объективную истину.
    """

    source_type: SourceType
    provider: str = Field(min_length=1, max_length=120)
    tool: str | None
    url: HttpUrlStr | None
    retrieved_at: AwareDatetime | None
    generated_at: AwareDatetime | None
    published_at: AwareDatetime | None
    external_id: str | None
    verification_status: VerificationStatus
    data_mode: DataMode
    limitations: list[str] = Field(default_factory=list)
    request_parameters: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _mode_rules(self) -> SourceReference:
        if self.data_mode in ("live", "replay") and self.retrieved_at is None:
            raise ValueError("для Live/Replay обязателен retrieved_at (DATA-MODE-02)")
        if self.data_mode == "mock":
            if self.generated_at is None:
                raise ValueError("для Mock обязателен generated_at (DATA-MODE-02)")
            if self.verification_status == "verified":
                raise ValueError("Mock-данные не могут иметь verification_status=verified")
            if self.source_type in ("mcp", "rest_api"):
                raise ValueError("Mock не может выдаваться за внешний API/MCP")
        if self.data_mode == "live" and self.source_type in ("mock_generator", "editorial"):
            raise ValueError("data_mode=live допустим только для реального внешнего источника")
        return self


# ---------------------------------------------------------------- Price (PRICE-01, CONTRACT-02)


class Fee(Contract):
    name: str
    amount: Money | None
    currency: Currency | None
    included: bool | None


class PricePeriod(Contract):
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self) -> PricePeriod:
        if self.end < self.start:
            raise ValueError("конец периода раньше начала")
        return self


class Price(VersionedContract):
    """Нормативные поля PRICE-01. Полная цена — за всех путешественников и весь период."""

    amount: Money | None
    currency: Currency | None
    price_scope: PriceScope
    guests: int | None = Field(ge=1, le=20)
    rooms: int | None = Field(ge=1, le=10)
    period: PricePeriod | None
    inclusions: list[str] = Field(default_factory=list)
    known_fees: list[Fee] = Field(default_factory=list)
    mandatory_fees_status: Literal["included", "excluded_known", "unknown", "not_applicable"]
    total_completeness: Completeness
    unknown_reason: UnknownReason | None
    source: SourceReference
    as_of: AwareDatetime

    @model_validator(mode="after")
    def _rules(self) -> Price:
        if self.amount is None:
            if self.total_completeness == "complete":
                raise ValueError("цена без суммы не может быть complete")
            if self.unknown_reason is None:
                raise ValueError("для неизвестной суммы обязателен unknown_reason")
        else:
            if self.currency is None:
                raise ValueError("сумма без валюты недопустима (PRICE-01)")
        if self.total_completeness == "complete":
            if self.mandatory_fees_status == "unknown":
                raise ValueError("при неизвестных обязательных сборах итог не может быть complete")
            if self.price_scope in ("per_person", "per_seat", "per_night", "unknown"):
                raise ValueError("цена за человека/место/ночь не является полной ценой группы и периода")
        if self.total_completeness != "complete" and self.unknown_reason is None:
            raise ValueError("неполная цена должна иметь unknown_reason")
        return self


# ---------------------------------------------------------------- Availability (AVAIL-01, CONTRACT-02)


class Availability(VersionedContract):
    """Наличие: независимые оси availability_status, verification_status, data_mode (DATA-MODE-02).

    ``available`` — подтверждение источника на конкретные даты/состав/тариф в пределах его семантики,
    не гарантия покупки. Поисковая выдача и Mock не доказывают наличие (план §2.19).
    """

    subject: Literal["hotel", "transport", "transfer"]
    availability_status: AvailabilityStatus
    evidence_level: EvidenceLevel
    verification_status: VerificationStatus
    data_mode: DataMode
    checked_at: AwareDatetime | None
    parameters: dict[str, Any] = Field(default_factory=dict)
    unknown_reason: UnknownReason | None
    seats_left: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _rules(self) -> Availability:
        if self.availability_status == "unknown" and self.unknown_reason is None:
            raise ValueError("для unknown обязателен unknown_reason")
        if self.data_mode == "replay" and self.subject == "hotel" and self.availability_status != "unknown":
            raise ValueError("доступность номеров в Replay всегда unknown (DATA-MODE-02)")
        if self.data_mode == "mock":
            if self.availability_status != "unknown":
                raise ValueError("Mock не подтверждает наличие: только unknown (DEC-15, строгая трактовка)")
            if self.verification_status == "verified":
                raise ValueError("Mock не может быть verified")
        if self.availability_status == "available":
            if self.data_mode != "live":
                raise ValueError("available допустим только по Live-данным")
            if self.evidence_level not in ("offer_details", "provider_checked"):
                raise ValueError(
                    "available требует подтверждения деталями или проверкой провайдера (план §2.19)"
                )
            if self.checked_at is None:
                raise ValueError("available требует checked_at")
        if self.seats_left is not None and self.subject == "hotel":
            raise ValueError("seats_left применим только к транспорту")
        return self


# ---------------------------------------------------------------- Validity (актуальность)


class Validity(Contract):
    """Срок актуальности оффера. TTL до DEC-21 — provisional, только в сторону осторожности."""

    observed_at: AwareDatetime
    valid_until: AwareDatetime | None
    basis: Literal["provider", "provisional_ttl", "none"]
    note: str | None = None


class Deviation(Contract):
    """Величина отклонения при нарушении числового условия (CONSTRAINT-01)."""

    amount: SignedMoney
    unit: str
    description: str
