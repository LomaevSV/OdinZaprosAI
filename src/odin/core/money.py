"""Деньги и полнота итога (PRICE-01, план §2.23).

``Decimal`` без неявной конвертации валют: разные валюты не суммируются. Итог ``complete`` только если
все слагаемые ``complete`` и валюта одна. Известная часть отделяется от полного итога явно.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from odin.contracts import Price, PricePeriod, SourceReference
from odin.contracts.common import Completeness, PriceScope

ZERO = Decimal("0.00")


class CurrencyMixError(ValueError):
    """Попытка сложить разные валюты без утверждённого курса (DEC-17)."""


@dataclass(frozen=True, slots=True)
class PriceTotals:
    """Итог по набору цен: известная часть, валюта (если одна) и полнота."""

    amount: Decimal | None
    currency: str | None
    completeness: Completeness
    unknown_reason: str | None
    partial_components: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "known_amount": None if self.amount is None else format(self.amount, "f"),
            "currency": self.currency,
            "completeness": self.completeness,
            "unknown_reason": self.unknown_reason,
            "partial_components": list(self.partial_components),
        }


def sum_prices(prices: Sequence[Price], component_ids: Sequence[str] | None = None) -> PriceTotals:
    """Складывает известные части.

    ``complete`` — только когда все слагаемые полны и валюта одна. Пропущенное слагаемое делает
    итог ``partial`` (известная нижняя граница), а не ``complete`` (PRICE-01, план §2.23).
    """
    ids = list(component_ids) if component_ids is not None else [str(i) for i in range(len(prices))]
    if len(ids) != len(prices):
        raise ValueError("число идентификаторов компонентов не совпадает с числом цен")
    if not prices:
        return PriceTotals(amount=None, currency=None, completeness="unknown", unknown_reason="not_requested")

    currencies = {price.currency for price in prices if price.currency is not None}
    if len(currencies) > 1:
        raise CurrencyMixError(f"разные валюты без утверждённого курса: {sorted(currencies)} (DEC-17)")
    currency = next(iter(currencies), None)

    known = [
        (price, component_id)
        for price, component_id in zip(prices, ids, strict=True)
        if price.amount is not None
    ]
    if not known:
        return PriceTotals(
            amount=None,
            currency=currency,
            completeness="unknown",
            unknown_reason=prices[0].unknown_reason or "not_provided_by_source",
            partial_components=tuple(ids),
        )

    total = ZERO
    incomplete: list[str] = []
    for price, component_id in known:
        assert price.amount is not None
        total += price.amount
        if price.total_completeness != "complete":
            incomplete.append(component_id)
    missing = [component_id for price, component_id in zip(prices, ids, strict=True) if price.amount is None]
    if incomplete or missing:
        return PriceTotals(
            amount=total,
            currency=currency,
            completeness="partial",
            unknown_reason="price_incomplete",
            partial_components=tuple(missing + incomplete),
        )
    return PriceTotals(amount=total, currency=currency, completeness="complete", unknown_reason=None)


def sum_prices_or_unresolved(
    prices: Sequence[Price], component_ids: Sequence[str] | None = None
) -> PriceTotals:
    """Как ``sum_prices``, но разные валюты дают неизвестный итог, а не исключение (MIXED-CURRENCY-PLAN-01).

    DEC-17: без проверенного курса суммы в разных валютах не складываются — итог ``unknown`` с причиной
    ``currency_unresolved``, без суммы и без валюты. Цены компонентов не меняются. ``partial_components``
    сохраняет смысл ``sum_prices``: компоненты без известной полной цены (MIXED-PARTIAL-COMPONENTS-03).
    """
    try:
        return sum_prices(prices, component_ids)
    except CurrencyMixError:
        ids = list(component_ids) if component_ids is not None else [str(i) for i in range(len(prices))]
        incomplete = tuple(
            component_id
            for price, component_id in zip(prices, ids, strict=True)
            if price.amount is None or price.total_completeness != "complete"
        )
        return PriceTotals(
            amount=None,
            currency=None,
            completeness="unknown",
            unknown_reason="currency_unresolved",
            partial_components=incomplete,
        )


def price_comparability(prices: Iterable[Price]) -> str:
    """Сопоставимость цен (план §2.23): подписанное поле, не заменяет ``completeness``."""
    items = list(prices)
    if not items:
        return "not_comparable"
    if len({price.price_scope for price in items}) > 1:
        return "not_comparable"
    if len({price.currency for price in items if price.currency}) > 1:
        return "currency_unresolved"
    if any(price.amount is None for price in items):
        return "not_comparable"
    if all(price.total_completeness == "complete" for price in items):
        return "comparable_complete"
    if any(price.unknown_reason == "estimate_only" for price in items):
        return "estimated_component"
    return "known_lower_bound"


def total_price(
    totals: PriceTotals,
    source: SourceReference,
    as_of: datetime,
    *,
    price_scope: PriceScope = "trip_total",
    period: PricePeriod | None = None,
) -> Price:
    """Собирает нормативный ``Price`` для плана и корзины из вычисленных итогов."""
    return Price(
        amount=totals.amount,
        currency=totals.currency,
        price_scope=price_scope,
        guests=None,
        rooms=None,
        period=period,
        inclusions=[],
        known_fees=[],
        mandatory_fees_status="included",
        total_completeness=totals.completeness,
        unknown_reason=totals.unknown_reason,
        source=source,
        as_of=as_of,
    )
