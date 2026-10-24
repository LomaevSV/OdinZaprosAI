"""Корзина демо-покупки: автомат, два раздельных согласия, идемпотентный чек (UC-07, UC-08, ARCH-05).

Правила:
- ``draft`` → ``awaiting_confirmation`` → ``demo_purchased`` (или ``declined`` вне контракта статусов —
  отказ фиксируется полем ``declined_at`` и возвратом корзины без чека);
- подтверждение требует обоих пунктов согласия, если есть существенные неизвестные — риск отдельно;
- ``confirmation_id`` — ключ идемпотентности: повтор возвращает тот же чек;
- корзина помнит ``trip_revision``: после новой ревизии поездки право подтверждения теряется.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from odin.contracts import Cart, CartItem, DemoReceipt, Price, SourceReference

DEMO_NOTICE = "Демонстрационная покупка. Реального бронирования и списания средств нет"


class CartError(ValueError):
    """Недопустимая операция с корзиной: причина возвращается клиенту, действие не выполняется."""


class CartStaleError(CartError):
    """Предложения корзины устарели после её формирования (DEC-21): перепроверка или новое согласие."""


def _seed(*parts: str) -> str:
    return hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()[:16]


def new_cart_id(trip_id: str, trip_revision: int) -> str:
    return f"DEMO-C-{_seed(trip_id, str(trip_revision))}"


def new_receipt_id(cart_id: str, confirmation_id: str) -> str:
    return f"DEMO-R-{_seed(cart_id, confirmation_id)}"


def build_cart(
    *,
    trip_id: str,
    trip_revision: int,
    items: list[CartItem],
    total_price: Price,
    completeness: str,
    significant_unknowns: list[str],
    created_at: datetime,
) -> Cart:
    """Корзина сразу в состоянии ``awaiting_confirmation``: черновик без позиций пользователю не нужен."""
    return Cart(
        cart_id=new_cart_id(trip_id, trip_revision),
        trip_id=trip_id,
        trip_revision=trip_revision,
        items=items,
        total_price=total_price,
        completeness=completeness,  # type: ignore[arg-type]
        status="awaiting_confirmation",
        confirmed_at=None,
        demo_receipt_id=None,
        risk_acknowledged_at=None,
        confirmation_id=None,
        significant_unknowns=significant_unknowns,
        created_at=created_at,
    )


def confirm_cart(
    cart: Cart,
    *,
    confirmation_id: str,
    trip_revision: int,
    purchase_consent: bool,
    risk_acknowledgement: bool,
    now: datetime,
    total_price: Price | None = None,
    completeness: str | None = None,
) -> tuple[Cart, DemoReceipt]:
    """Подтверждение демо-покупки. Возвращает обновлённую корзину и новый чек."""
    if cart.status == "demo_purchased":
        raise CartError("корзина уже подтверждена: используйте существующий чек (идемпотентность)")
    if cart.status != "awaiting_confirmation":
        raise CartError(f"подтверждение недоступно в статусе {cart.status}")
    if cart.trip_revision != trip_revision:
        raise CartError("ревизия поездки изменилась: корзина потеряла право подтверждения (REPLAN-02)")
    if not purchase_consent:
        raise CartError("требуется явное согласие на демо-покупку")
    if cart.significant_unknowns and not risk_acknowledgement:
        raise CartError("требуется отдельное согласие с перечисленными рисками неизвестных условий")

    price = total_price or cart.total_price
    cart_state = cart.evolve(
        **{
            "status": "demo_purchased",
            "confirmed_at": now,
            "confirmation_id": confirmation_id,
            "risk_acknowledged_at": now if risk_acknowledgement else None,
            "demo_receipt_id": new_receipt_id(cart.cart_id, confirmation_id),
        }
    )
    receipt = DemoReceipt(
        receipt_id=cart_state.demo_receipt_id or new_receipt_id(cart.cart_id, confirmation_id),
        cart_id=cart.cart_id,
        trip_id=cart.trip_id,
        trip_revision=cart.trip_revision,
        confirmation_id=confirmation_id,
        created_at=now,
        items=cart.items,
        total_price=price,
        completeness=(completeness or cart.completeness),  # type: ignore[arg-type]
        purchase_consent_at=now,
        risk_acknowledged_at=cart_state.risk_acknowledged_at,
        significant_unknowns=cart.significant_unknowns,
    )
    return cart_state, receipt


def decline_cart(cart: Cart, *, now: datetime) -> Cart:
    """Отказ: ничего не оформляется, чек не создаётся. Отметка времени сохраняется."""
    if cart.status == "demo_purchased":
        raise CartError("корзина уже подтверждена: отказ невозможен")
    return cart.evolve(**{"declined_at": now})


def cart_item_from_offer(
    *,
    item_id: str,
    component_id: str,
    role: str,
    kind: str,
    title: str,
    data_mode: str,
    price: Price,
    availability_status: str,
    source: SourceReference,
    informational: bool = False,
    note: str | None = None,
) -> CartItem:
    return CartItem(
        item_id=item_id,
        component_id=component_id,
        role=role,  # type: ignore[arg-type]
        kind=kind,
        title=title,
        data_mode=data_mode,  # type: ignore[arg-type]
        price=price,
        availability_status=availability_status,  # type: ignore[arg-type]
        informational=informational,
        note=note,
        source=source,
    )
