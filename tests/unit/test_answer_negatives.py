"""TEST-04: негативные проверки ответа — отклонение или безопасное исключение неверного поля (ARCH-04).

Без второго универсального LLM-валидатора: проверки структурные (контракты, ядро, Composer).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from pydantic import ValidationError

from odin.core.money import CurrencyMixError, sum_prices
from odin.core.trip import arrival_checkin_check
from odin.llm.composer import ComposerFacts, safe_answer, verify_facts
from odin.tools.untrusted import safe_phone, safe_url
from tests.samples import availability, hotel_offer, price

pytestmark = [pytest.mark.negative, pytest.mark.req("TEST-04", "ARCH-04")]


def test_unknown_cannot_become_available_without_evidence() -> None:
    with pytest.raises(ValidationError):
        availability(availability_status="available", unknown_reason=None)  # Mock
    with pytest.raises(ValidationError):
        availability(
            data_mode="live",
            availability_status="available",
            evidence_level="search_listing",
            verification_status="verified",
            checked_at=datetime.now(tz=UTC),
            unknown_reason=None,
        )


def test_wrong_group_price_scope_cannot_be_complete_total() -> None:
    with pytest.raises(ValidationError):
        price(
            price_scope="per_person",
            total_completeness="complete",
            mandatory_fees_status="included",
            unknown_reason=None,
        )
    with pytest.raises(ValidationError):
        price(
            price_scope="per_night",
            total_completeness="complete",
            mandatory_fees_status="included",
            unknown_reason=None,
        )


def test_wrong_buffer_never_yields_matched() -> None:
    check = arrival_checkin_check(
        check_id="b",
        from_component="outbound",
        to_component="hotel",
        arrival_at=datetime(2026, 11, 20, 13, 50, tzinfo=UTC),
        check_in_date=date(2026, 11, 20),
        check_in_from=time(14, 0),
        timezone_name="UTC",
        buffer_minutes=40,
        buffer_status="proposed_not_approved",  # буфер не утверждён (DEC-20)
        transfer_minutes=30,
    )
    assert check.status == "unknown" and check.unknown_reason == "buffer_not_approved"
    with pytest.raises(ValidationError):
        check.evolve(
            status="matched", unknown_reason=None
        )  # matched без утверждённого буфера запрещён контрактом


def test_wrong_currency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        price(currency="rub")
    with pytest.raises(CurrencyMixError):
        sum_prices([price(currency="RUB"), price(currency="EUR")])
    offer = hotel_offer()
    with pytest.raises(ValidationError):
        offer.evolve(currency="EUR")  # валюта оффера ≠ валюте цены


def test_unnoticed_date_change_is_rejected() -> None:
    offer = hotel_offer()
    with pytest.raises(ValidationError):
        offer.evolve(check_out=offer.check_in)
    period_mismatch = price(period=None)
    assert period_mismatch.period is None  # цена без периода не выдаётся за цену на даты поездки


def test_invented_numbers_contact_and_link_are_excluded() -> None:
    facts = ComposerFacts(
        destination="Казань", known_amount="27000.00", currency="RUB", completeness="partial"
    )
    text, verified = safe_answer(facts)
    assert verified
    ok, offenders = verify_facts(text + " Звоните +7 999 123-45-67, скидка 30%.", facts)
    assert not ok and offenders
    assert safe_url("https://booking-fake.test/pay", frozenset({"tutu.ru"})) is None
    assert safe_phone("свяжитесь в мессенджере") is None


def test_missing_required_condition_is_reported_not_dropped() -> None:
    from odin.contracts import Constraint
    from odin.core.tree import coverage

    required = Constraint(
        constraint_id="c-pets",
        field="pets",
        operator="eq",
        value=True,
        requiredness="required",
        source_span="с собакой",
        source_kind="form_field",
        scope="trip",
        confidence=None,
        ambiguity=None,
    )
    report = coverage([required], [])
    assert report.missing == ("c-pets",) and not report.complete
