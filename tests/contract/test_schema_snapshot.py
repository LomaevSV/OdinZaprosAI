"""T-CON-SCHEMA-01: схемы и OpenAPI совпадают со снимком (CONTRACT-03, API-01).

Снимки: `schemas/*.schema.json` (19 нормативных типов) и `schemas/openapi.json` (пути §2.14).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from odin.contracts import NORMATIVE_MODELS, SCHEMA_VERSION, schema_export
from odin.contracts.common import VersionedContract
from tests.samples import NORMATIVE_SAMPLES

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"

# 17 типов ТЗ §7 + нормативные Price и Availability (CONTRACT-02; план S1, замечание F-07).
EXPECTED_NORMATIVE = {
    "ToolRequest",
    "ToolResult",
    "AgentResult",
    "TripRequest",
    "TripState",
    "TravelOffer",
    "TripPlan",
    "Cart",
    "ReplanEvent",
    "SourceReference",
    "Constraint",
    "ConstraintGroup",
    "FallbackActivation",
    "ConstraintEvaluation",
    "Hotel",
    "HotelOffer",
    "ReviewEvidence",
    "Price",
    "Availability",
}

pytestmark = [pytest.mark.contract, pytest.mark.req("CONTRACT-03", "API-01", "T-CON-SCHEMA-01")]


def test_normative_set_is_exactly_19_types() -> None:
    assert len(EXPECTED_NORMATIVE) == 19
    assert set(NORMATIVE_MODELS) == EXPECTED_NORMATIVE
    assert set(NORMATIVE_SAMPLES) == EXPECTED_NORMATIVE
    for name, model in NORMATIVE_MODELS.items():
        assert model.__name__ == name


@pytest.mark.parametrize("name", sorted(EXPECTED_NORMATIVE))
def test_schema_declares_version_and_forbids_extra_fields(name: str) -> None:
    assert issubclass(NORMATIVE_MODELS[name], VersionedContract)
    schema = schema_export.build_schema(name)
    assert schema["$schema"] == schema_export.JSON_SCHEMA_DIALECT
    assert schema["$id"] == f"urn:odin:contracts:{SCHEMA_VERSION}:{name}"
    assert schema["title"] == name
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("name", sorted(EXPECTED_NORMATIVE))
def test_serialized_contract_carries_schema_version(name: str) -> None:
    payload = json.loads(NORMATIVE_SAMPLES[name]().model_dump_json())
    assert payload["schema_version"] == SCHEMA_VERSION


def test_committed_snapshot_matches_code() -> None:
    problems = schema_export.diff_schemas(SCHEMA_DIR)
    assert problems == [], (
        "снимок schemas/ расходится с кодом (FAIL по CONTRACT-03). "
        "Изменение нормативных схем — только по согласованию; затем: python -m odin.contracts.schema_export"
    )


def test_snapshot_files_are_utf8_lf_without_bom() -> None:
    paths = sorted(SCHEMA_DIR.glob(f"*{schema_export.SCHEMA_FILE_SUFFIX}"))
    assert len(paths) == 19
    for path in paths:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), path.name
        assert b"\r\n" not in raw, path.name
        assert json.loads(raw.decode("utf-8"))["title"] == path.name.removesuffix(
            schema_export.SCHEMA_FILE_SUFFIX
        )


def test_export_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    schema_export.write_schemas(first)
    schema_export.write_schemas(second)
    names = sorted(p.name for p in first.iterdir())
    assert len(names) == 19
    assert names == sorted(p.name for p in second.iterdir())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_diff_detects_changed_missing_and_extra_files(tmp_path: Path) -> None:
    schema_export.write_schemas(tmp_path)
    assert schema_export.diff_schemas(tmp_path) == []
    (tmp_path / "Price.schema.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "Cart.schema.json").unlink()
    (tmp_path / "Legacy.schema.json").write_text("{}\n", encoding="utf-8")
    assert schema_export.diff_schemas(tmp_path) == [
        "нет файла снимка: Cart.schema.json",
        "лишний файл снимка: Legacy.schema.json",
        "снимок расходится с кодом: Price.schema.json",
    ]


def test_write_removes_stale_schema_files(tmp_path: Path) -> None:
    (tmp_path / "Legacy.schema.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("не схема\n", encoding="utf-8")
    schema_export.write_schemas(tmp_path)
    assert not (tmp_path / "Legacy.schema.json").exists()
    assert (tmp_path / "notes.txt").exists()
    assert schema_export.diff_schemas(tmp_path) == []


def test_cli_check_exit_codes(tmp_path: Path) -> None:
    assert schema_export.main(["--dir", str(tmp_path)]) == 0
    assert schema_export.main(["--dir", str(tmp_path), "--check"]) == 0
    (tmp_path / "Price.schema.json").write_text("{}\n", encoding="utf-8")
    assert schema_export.main(["--dir", str(tmp_path), "--check"]) == 1
    assert schema_export.main(["--dir", str(tmp_path / "missing"), "--check"]) == 1


def test_openapi_snapshot_matches() -> None:
    from odin.openapi_snapshot import diff_openapi

    problems = diff_openapi(SCHEMA_DIR / "openapi.json")
    assert problems == [], (
        "снимок OpenAPI расходится с кодом: python -m odin.openapi_snapshot (только по согласованию)"
    )


def test_openapi_covers_all_api_paths_of_plan() -> None:
    """План §2.14: пути и методы закреплены; вторая версия API не создаётся."""
    document = json.loads((SCHEMA_DIR / "openapi.json").read_text(encoding="utf-8"))
    expected = {
        ("get", "/health"),
        ("post", "/api/v1/sessions"),
        ("get", "/api/v1/sessions/{session_id}"),
        ("post", "/api/v1/sessions/{session_id}/messages"),
        ("get", "/api/v1/sessions/{session_id}/events"),
        ("get", "/api/v1/trips/{trip_id}"),
        ("post", "/api/v1/trips/{trip_id}/selections"),
        ("post", "/api/v1/trips/{trip_id}/replan"),
        ("post", "/api/v1/trips/{trip_id}/cart"),
        ("get", "/api/v1/trips/{trip_id}/cart"),
        ("post", "/api/v1/carts/{cart_id}/confirm"),
        ("post", "/api/v1/carts/{cart_id}/decline"),
        ("get", "/api/v1/receipts/{receipt_id}"),
        ("get", "/api/v1/schemas/{name}"),
    }
    actual = {(method, path) for path, item in document["paths"].items() for method in item}
    assert actual == expected
    assert not any(path.startswith("/api/v2") for path in document["paths"])
    components = document["components"]["schemas"]
    for name in ("TripState", "Cart", "DemoReceipt", "ErrorResponse", "HealthResponse"):
        assert name in components or any(key.startswith(name) for key in components)
