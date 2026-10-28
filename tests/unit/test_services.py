"""Хранилище (ARCH-05, план §2.13), конфигурация (SECURITY-01), LLM Gateway и Composer (ARCH-04, ARCH-06, DEC-08),
корзина (UC-08), интерпретатор формы."""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from odin.config import ConfigError, LlmSettings, Settings
from odin.contracts.api import StructuredForm
from odin.core.cart import CartError, build_cart, confirm_cart, decline_cart
from odin.llm.composer import ComposerFacts, compose_answer, safe_answer, verify_facts
from odin.llm.gateway import LlmGateway, replay_key
from odin.llm.interpreter import interpret_structured_form, refuse_free_text, request_to_form
from odin.storage import ConflictError, NotFoundError, Storage
from tests.samples import cart as sample_cart
from tests.samples import price, trip_state

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- хранилище


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "odin.sqlite3")
    store.create_session("s-1", "t-1", llm_mode="off", data_mode="mock")
    return store


def _state(revision: int):
    return trip_state().evolve(trip_id="t-1", session_id="s-1", revision=revision)


@pytest.mark.req("ARCH-05")
def test_storage_uses_wal_and_appends_revisions(storage: Storage) -> None:
    assert storage.journal_mode() == "wal"
    storage.append_revision(_state(1), "req-1", agreed=False)
    storage.append_revision(_state(2), "req-2", agreed=True)
    head = storage.get_head("t-1")
    assert head["current_revision"] == 2 and head["last_agreed_revision"] == 2
    assert storage.list_revisions("t-1") == [1, 2]
    assert storage.get_state("t-1", 1).revision == 1  # старые ревизии неизменны


@pytest.mark.negative
@pytest.mark.req("ARCH-05", "T-UNIT-REV-01")
def test_revision_gap_or_duplicate_is_conflict_and_does_not_move_head(storage: Storage) -> None:
    storage.append_revision(_state(1), "req-1", agreed=True)
    with pytest.raises(ConflictError):
        storage.append_revision(_state(1), "req-dup", agreed=False)
    with pytest.raises(ConflictError):
        storage.append_revision(_state(3), "req-gap", agreed=False)
    assert storage.get_head("t-1")["current_revision"] == 1
    assert storage.list_revisions("t-1") == [1]


def test_failed_turn_does_not_change_last_agreed_revision(storage: Storage) -> None:
    storage.append_revision(_state(1), "req-1", agreed=True)
    storage.append_revision(_state(2), "req-2", agreed=False)
    head = storage.get_head("t-1")
    assert head["current_revision"] == 2 and head["last_agreed_revision"] == 1


def test_storage_not_found_and_events(storage: Storage) -> None:
    with pytest.raises(NotFoundError):
        storage.get_session("s-missing")
    with pytest.raises(NotFoundError):
        storage.get_receipt("DEMO-R-missing")
    first = storage.add_event("s-1", "turn_started", {"request_id": "r"})
    storage.add_event("s-1", "turn_finished", {})
    assert [event["event"] for event in storage.list_events("s-1", after_seq=first)] == ["turn_finished"]


def test_session_lock_serializes_turns(storage: Storage) -> None:
    order: list[str] = []

    def worker(name: str) -> None:
        with storage.session_lock("s-1"):
            order.append(f"{name}-in")
            order.append(f"{name}-out")

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])


# --------------------------------------------------------------------- конфигурация


@pytest.mark.req("SECURITY-01", "DEC-23")
def test_config_defaults_and_restrictions() -> None:
    settings = Settings.from_env({})
    assert settings.data_mode == "mock" and settings.llm.mode == "off" and settings.port == 8080
    assert settings.data_mode_auto is True  # auto: без ключа LLM — резервный mock
    for mode in ("mock", "mixed", "live", "replay"):
        explicit = Settings.from_env({"ODIN_DATA_MODE": mode})
        assert explicit.data_mode == mode and explicit.data_mode_auto is False
    configured = Settings.from_env(
        {
            "ODIN_LLM_MODE": "live",
            "ODIN_LLM_BASE_URL": "https://llm.example.test/v1",
            "ODIN_LLM_MODEL": "m",
            "ODIN_LLM_API_KEY": "k",
        }
    )
    assert configured.data_mode == "live"  # при настроенной LLM — строгий live, без подмены на Mock
    assert Settings.from_env({"ODIN_LLM_API_KEY": "k"}).data_mode == "mock"  # ключ без режима live — mock
    with pytest.raises(ConfigError):
        Settings.from_env({"ODIN_DATA_MODE": "demo"})
    with pytest.raises(ConfigError):
        Settings.from_env({"ODIN_LLM_MODE": "live"})  # без ключа live запрещён
    with pytest.raises(ConfigError):
        Settings.from_env({"ODIN_PORT": "abc"})
    with pytest.raises(ConfigError):
        Settings.from_env({"ODIN_CALLS_PER_TURN": "0"})
    keyed = Settings.from_env({"ODIN_LLM_API_KEY": "secret-value"})
    assert keyed.llm.describe_key() == "present"
    assert "secret-value" not in repr(keyed.llm.describe_key())


# --------------------------------------------------------------------- LLM


@pytest.mark.req("T-LLM-MODE-01", "DEC-08")
def test_llm_off_is_explicitly_unavailable() -> None:
    result = LlmGateway(settings=LlmSettings(mode="off")).complete_structured(
        role="interpreter", text="в Казань", reference_date="2026-10-24", schema_version="1.0.0"
    )
    assert result.status == "unavailable" and result.structured_output is None
    assert "DEC-08" in result.uncertainties[0]


@pytest.mark.req("T-LLM-MODE-03", "ARCH-06")
def test_replay_requires_exact_key_and_valid_record(tmp_path: Path) -> None:
    gateway = LlmGateway(settings=LlmSettings(mode="replay", replay_dir=tmp_path))
    key = replay_key(text="в Казань", reference_date="2026-10-24", schema_version="1.0.0", role="interpreter")
    (tmp_path / f"{key}.json").write_text(
        json.dumps({"key": key, "structured_output": {"destination": "Казань"}}), encoding="utf-8"
    )
    ok = gateway.complete_structured(
        role="interpreter", text="в Казань", reference_date="2026-10-24", schema_version="1.0.0"
    )
    assert ok.status == "ok" and ok.structured_output == {"destination": "Казань"}
    other_date = gateway.complete_structured(
        role="interpreter", text="в Казань", reference_date="2026-10-31", schema_version="1.0.0"
    )
    assert other_date.status == "unavailable"  # другие даты не подставляются
    broken_key = replay_key(
        text="битая", reference_date="2026-10-24", schema_version="1.0.0", role="interpreter"
    )
    (tmp_path / f"{broken_key}.json").write_text("{not json", encoding="utf-8")
    broken = gateway.complete_structured(
        role="interpreter", text="битая", reference_date="2026-10-24", schema_version="1.0.0"
    )
    assert broken.status == "error"


def test_replay_without_directory_is_unavailable() -> None:
    result = LlmGateway(settings=LlmSettings(mode="replay")).complete_structured(
        role="interpreter", text="x", reference_date="2026-10-24", schema_version="1.0.0"
    )
    assert result.status == "unavailable"


@pytest.mark.req("ARCH-04", "TEST-04", "GOAL-03")
def test_composer_uses_only_fact_slots_and_fact_check_rejects_invented_numbers() -> None:
    facts = ComposerFacts(
        destination="Казань",
        origin="Москва",
        dates="2026-11-20 — 2026-11-22",
        party="2 взрослых",
        known_amount="27000.00",
        currency="RUB",
        completeness="partial",
        offers_count=7,
        unknowns=("стоимость трансфера",),
        stop_reason="sources_exhausted",
    )
    text = compose_answer(facts)
    assert "27 000 ₽" in text and "известна частично" in text and "не означает, что вариантов нет" in text
    assert verify_facts(text, facts) == (True, [])
    ok, offenders = verify_facts(text + "\nСкидка 15%, всего 22950 ₽", facts)
    assert not ok and {"15", "22950"} <= set(offenders)


def test_safe_answer_falls_back_to_template_on_failed_fact_check() -> None:
    facts = ComposerFacts(destination="Казань", mode_note="акция до 31.12")
    text, verified = safe_answer(facts)
    assert verified  # число из слота допустимо
    poisoned = ComposerFacts(destination="Казань", known_amount="100.00", currency="RUB")
    text, verified = safe_answer(poisoned)
    assert verified and "100" in text


# --------------------------------------------------------------------- интерпретатор


@pytest.mark.req("DEC-08", "UC-02", "UC-03")
def test_structured_form_interpretation() -> None:
    form = StructuredForm(
        origin="Москва",
        destination="Казань",
        date_from=date(2026, 11, 20),
        date_to=date(2026, 11, 22),
        adults=2,
        budget_amount=Decimal("40000"),
        budget_currency="RUB",
        budget_required=True,
        breakfast=True,
        transport_modes=["rail"],
    )
    outcome = interpret_structured_form(form, session_id="s", request_id="req-1")
    assert outcome.questions == [] and outcome.mode == "structured_fallback"
    request = outcome.request
    assert request is not None and request.input_mode == "structured_form"
    by_id = {c.constraint_id: c for c in request.constraints}
    assert by_id["c-budget_total"].requiredness == "required"
    assert by_id["c-hotel_breakfast"].requiredness == "preferred"
    assert all(c.source_kind == "form_field" for c in request.constraints)
    assert request.destination is not None and request.destination.timezone == "Europe/Moscow"
    assert request_to_form(request).model_dump() == form.model_dump()


def test_interpreter_asks_only_blocking_questions_and_does_not_invent() -> None:
    outcome = interpret_structured_form(StructuredForm(destination="Казань"), session_id="s", request_id="r")
    # UC-02/UC-03 (S4): город отправления и даты не предполагаются — blocking_now;
    # состав группы — assumable_only_if_authorized, но DEC-25 не разрешает допущений → вопрос.
    classes = {q.field: q.blocking_class for q in outcome.questions}
    assert classes == {
        "origin": "blocking_now",
        "date_from": "blocking_now",
        "adults": "assumable_only_if_authorized",
    }
    unknown_city = interpret_structured_form(
        StructuredForm(origin="Москва", destination="Урюпинск", adults=1), session_id="s", request_id="r"
    )
    place = unknown_city.request.destination  # type: ignore[union-attr]
    assert place is not None and place.timezone is None and place.lat is None  # координаты не выдуманы
    refused = refuse_free_text(session_id="s", request_id="r")
    assert refused.free_text_refused and refused.request is None


# --------------------------------------------------------------------- корзина


@pytest.mark.req("UC-08", "ARCH-05")
def test_cart_state_machine_and_consents() -> None:
    now = datetime.now(tz=UTC)
    base = sample_cart()
    cart = build_cart(
        trip_id="t-1",
        trip_revision=3,
        items=base.items,
        total_price=price(price_scope="trip_total"),
        completeness="partial",
        significant_unknowns=["доступность не подтверждена"],
        created_at=now,
    )
    assert cart.status == "awaiting_confirmation" and cart.cart_id.startswith("DEMO-C-")
    with pytest.raises(CartError):
        confirm_cart(
            cart,
            confirmation_id="c1",
            trip_revision=3,
            purchase_consent=False,
            risk_acknowledgement=True,
            now=now,
        )
    with pytest.raises(CartError):
        confirm_cart(
            cart,
            confirmation_id="c1",
            trip_revision=3,
            purchase_consent=True,
            risk_acknowledgement=False,
            now=now,
        )
    with pytest.raises(CartError):
        confirm_cart(
            cart,
            confirmation_id="c1",
            trip_revision=4,
            purchase_consent=True,
            risk_acknowledgement=True,
            now=now,
        )
    purchased, receipt = confirm_cart(
        cart, confirmation_id="c1", trip_revision=3, purchase_consent=True, risk_acknowledgement=True, now=now
    )
    assert purchased.status == "demo_purchased" and receipt.receipt_id == purchased.demo_receipt_id
    assert receipt.notice == "Демонстрационная покупка. Реального бронирования и списания средств нет"
    with pytest.raises(CartError):
        confirm_cart(
            purchased,
            confirmation_id="c2",
            trip_revision=3,
            purchase_consent=True,
            risk_acknowledgement=True,
            now=now,
        )
    with pytest.raises(CartError):
        decline_cart(purchased, now=now)
    declined = decline_cart(cart, now=now)
    assert declined.declined_at == now and declined.demo_receipt_id is None
