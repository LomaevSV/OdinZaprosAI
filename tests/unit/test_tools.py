"""Tool Registry, политика deny-by-default, Mock-адаптеры (ARCH-01..03, SECURITY-01, DATA-MODE-02, T-NEG-TOOL-*)."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from odin.config import Limits
from odin.contracts import ToolRequest
from odin.tools import MOCK_HANDLERS, ToolDeniedError, ToolPolicy, ToolRegistry, load_provider_policies
from odin.tools.registry import ToolBudgetExceeded, ToolDeadlineExceeded

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "odin"


def request(tool: str, arguments: dict[str, Any] | None = None, call_id: str = "c1") -> ToolRequest:
    return ToolRequest(
        request_id="req-tools-1",
        call_id=call_id,
        tool_name=tool,  # type: ignore[arg-type]
        arguments=arguments or {},
        deadline=datetime.now(tz=UTC) + timedelta(seconds=20),
        policy_scope="read_only",
    )


def registry(handlers: dict[str, Any] | None = None, **limit_overrides: Any) -> ToolRegistry:
    limits = Limits(**limit_overrides)
    policy = ToolPolicy(data_mode="mock", policies=load_provider_policies())
    return ToolRegistry(policy=policy, handlers=handlers or MOCK_HANDLERS, limits=limits)


HOTEL_ARGS = {"city": "Казань", "check_in": "2026-11-06", "check_out": "2026-11-08", "adults": 2}


# --------------------------------------------------------------------- политика


@pytest.mark.negative
@pytest.mark.req("SECURITY-01", "T-NEG-SEC-01", "UC-08")
def test_checkout_tool_is_always_denied_before_network() -> None:
    policy = ToolPolicy(data_mode="mock", policies=load_provider_policies())
    with pytest.raises(ToolDeniedError) as err:
        policy.check_provider_tool("tutu_mcp", "register_checkout_passengers")
    assert "реальный заказ" in err.value.reason


APPROVED = {"tutu_mcp", "open_meteo", "osrm_demo"}  # DEC-09, 25.10.2026


@pytest.mark.negative
@pytest.mark.req("PROVIDER-01", "T-DLV-REG-01", "DEC-09")
def test_only_owner_approved_providers_are_live_and_allowlists_hold() -> None:
    policies = load_provider_policies()
    assert policies, "реестр провайдеров должен читаться"
    assert {pid for pid, item in policies.items() if item.approval_status == "approved"} == APPROVED
    policy = ToolPolicy(data_mode="live", policies=policies)
    for provider in set(policies) - APPROVED:
        with pytest.raises(ToolDeniedError):
            policy.check_provider_tool(provider, "search_hotels")
    for tool in ("search_rail", "search_avia", "search_hotels", "get_offer_details"):
        policy.check_provider_tool("tutu_mcp", tool)  # allowlist утверждённого провайдера
    for tool in ("create_checkout_link", "get_rail_seatmap", "get_hotels_instructions", "search_bus"):
        with pytest.raises(ToolDeniedError):
            policy.check_provider_tool("tutu_mcp", tool)  # DEC-10 / P1
    with pytest.raises(ToolDeniedError):
        policy.check_provider_tool("open_meteo", "forecast")  # прогноз — P1, не утверждён
    with pytest.raises(ToolDeniedError):
        policy.check_provider_tool("unknown_provider", "anything")
    with pytest.raises(ToolDeniedError):
        policy.check_resource_uri("tutu_mcp", "tutu://help/secret")


@pytest.mark.negative
def test_unknown_or_unimplemented_logical_tools_are_denied() -> None:
    policy = ToolPolicy(data_mode="mock")
    with pytest.raises(ToolDeniedError):
        policy.check_logical("delete_everything")
    with pytest.raises(ToolDeniedError):
        policy.check_logical("get_reviews")  # P1, не реализован в Mock
    assert policy.check_logical("search_hotels") == "odin_mock"


@pytest.mark.negative
@pytest.mark.req("DEC-09", "DEC-23")
def test_live_and_mixed_routing_follows_approvals() -> None:
    policies = load_provider_policies()
    live = ToolPolicy(data_mode="live", policies=policies)
    assert live.check_logical("search_hotels") == "tutu_mcp"
    assert live.check_logical("geocode") == "open_meteo"
    assert live.check_logical("get_transfer_options") == "osrm_demo"
    assert live.check_logical("suggest_destinations") == "odin_mock"  # редакционный каталог
    with pytest.raises(ToolDeniedError):
        live.check_logical("get_reviews")
    unapproved = {pid: replace(item, approval_status="proposed") for pid, item in policies.items()}
    with pytest.raises(ToolDeniedError) as err:
        ToolPolicy(data_mode="live", policies=unapproved).check_logical("search_hotels")
    assert err.value.decision == "DEC-09"
    mixed = ToolPolicy(data_mode="mixed", policies=unapproved)
    assert mixed.check_logical("search_hotels") == "odin_mock"  # явный Mock по компоненту


# --------------------------------------------------------------------- реестр


@pytest.mark.req("CONTRACT-01", "T-CON-STATUS-01")
def test_mock_results_are_normative_tool_results() -> None:
    reg = registry()
    hotels = reg.invoke(request("search_hotels", HOTEL_ARGS))
    assert hotels.status == "ok" and hotels.sources and hotels.sources[0].data_mode == "mock"
    transfer = reg.invoke(request("get_transfer_options", {"city": "Казань"}, call_id="c2"))
    assert transfer.status == "partial" and transfer.missing == ["стоимость трансфера"]
    bad = reg.invoke(request("search_hotels", {**HOTEL_ARGS, "city": "Атлантида"}, call_id="c3"))
    assert bad.status == "error" and bad.error_code == "city_not_in_demo_directory"
    details = reg.invoke(request("get_offer_details", {"offer_id": "x"}, call_id="c4"))
    assert details.status == "empty"


@pytest.mark.negative
@pytest.mark.req("ARCH-02", "T-NEG-TOOL-BUDGET")
def test_call_budget_per_turn_is_enforced() -> None:
    reg = registry(calls_per_turn=2)
    reg.invoke(request("search_hotels", HOTEL_ARGS, call_id="a"))
    reg.invoke(request("search_hotels", HOTEL_ARGS, call_id="b"))
    with pytest.raises(ToolBudgetExceeded):
        reg.invoke(request("search_hotels", HOTEL_ARGS, call_id="c"))
    reg.start_turn()
    assert reg.invoke(request("search_hotels", HOTEL_ARGS, call_id="d")).status == "ok"


@pytest.mark.negative
@pytest.mark.req("ARCH-02", "T-NEG-TOOL-DEADLINE")
def test_expired_turn_deadline_stops_calls() -> None:
    reg = registry()
    reg.start_turn(datetime.now(tz=UTC) - timedelta(seconds=1))
    with pytest.raises(ToolDeadlineExceeded):
        reg.invoke(request("search_hotels", HOTEL_ARGS))


@pytest.mark.negative
@pytest.mark.req("ARCH-02", "T-NEG-TOOL-CB")
def test_adapter_failure_retries_once_then_circuit_breaker_opens() -> None:
    calls = {"n": 0}

    def broken(_: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        raise RuntimeError("boom")

    reg = registry({"search_hotels": broken}, tool_retries=1, circuit_breaker_threshold=3, calls_per_turn=10)
    first = reg.invoke(request("search_hotels", call_id="1"))
    assert first.status == "error" and first.error_code == "adapter_error:RuntimeError"
    assert calls["n"] == 2  # один повтор только для чтения
    reg.invoke(request("search_hotels", call_id="2"))
    opened = reg.invoke(request("search_hotels", call_id="3"))
    assert opened.status == "unavailable" and opened.error_code == "circuit_open"
    assert calls["n"] == 4  # при открытом предохранителе адаптер не вызывается


@pytest.mark.req("DATA-MODE-02", "T-CON-MOCK-01")
def test_mock_offers_are_marked_demo_and_never_available() -> None:
    result = registry().invoke(request("search_hotels", HOTEL_ARGS))
    for offer in result.data["offers"]:
        assert offer.source.data_mode == "mock"
        assert offer.source.verification_status == "unverified"
        assert offer.availability.availability_status == "unknown"
        assert offer.source.generated_at is not None and offer.source.retrieved_at is None
    for hotel in result.data["hotels"]:
        assert hotel.name.startswith("ДЕМО-")


def test_mock_generator_is_deterministic() -> None:
    first = registry().invoke(request("search_hotels", HOTEL_ARGS))
    second = registry().invoke(request("search_hotels", HOTEL_ARGS))
    ids = [offer.offer_id for offer in first.data["offers"]]
    assert ids == [offer.offer_id for offer in second.data["offers"]]
    amounts = [offer.price_total.amount for offer in first.data["offers"]]
    assert amounts == [offer.price_total.amount for offer in second.data["offers"]]


def test_mock_hotel_price_is_for_whole_stay_and_party() -> None:
    result = registry().invoke(request("search_hotels", {**HOTEL_ARGS, "rooms": 2}))
    offer = result.data["offers"][0]
    assert offer.price_scope == "stay_total"
    assert offer.price_total.period is not None
    assert offer.price_total.rooms == 2
    assert offer.price_total.amount == offer.price_total.amount.quantize(offer.price_total.amount)


# --------------------------------------------------------------------- архитектура


@pytest.mark.req("ARCH-01", "T-UNIT-ARCH-01")
def test_domains_core_and_llm_do_not_import_registry_or_adapters() -> None:
    """Звезда: ядро и LLM-роли не вызывают инструменты в обход оркестратора."""
    forbidden = {"odin.tools.registry", "odin.tools.mock_adapters", "odin.orchestrator", "odin.storage"}
    offenders: list[str] = []
    for path in [*(SRC / "core").glob("*.py"), *(SRC / "llm").glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            offenders.extend(f"{path.name}: {name}" for name in names if name in forbidden)
    assert offenders == []


@pytest.mark.req("ARCH-06", "SECURITY-01")
def test_llm_has_no_tools_or_write_access() -> None:
    """LLM не имеет инструментов и записи: сеть — только клиент Chat Completions, без tools/function calling."""
    for path in (SRC / "llm").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "Storage" not in source and "ToolRegistry" not in source, path.name
        if path.name not in ("openai_client.py", "gateway.py"):
            assert "httpx" not in source, path.name
    client_source = (SRC / "llm" / "openai_client.py").read_text(encoding="utf-8")
    assert '"tools"' not in client_source and "function_call" not in client_source
    assert "follow_redirects=False" in client_source
