"""Целостность корпуса K-01..K-30 (TEST-01, AUDIT-01): доказательства существуют, Live не PASS без них."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from tests.corpus.run_corpus import CORPUS, evaluate_corpus

pytestmark = [pytest.mark.delivery, pytest.mark.req("TEST-01", "AUDIT-01", "TEST-02")]

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DATA = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))


def _functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_corpus_has_k01_to_k30_with_modes() -> None:
    ids = [scenario["id"] for scenario in CORPUS_DATA["scenarios"]]
    assert ids == [f"K-{index:02d}" for index in range(1, 31)]
    for scenario in CORPUS_DATA["scenarios"]:
        assert scenario["modes"], scenario["id"]
        assert set(scenario["modes"]) <= {"mock", "replay", "live", "mixed"}
        assert scenario["requirements"] and scenario["test01"]


def test_every_evidence_node_exists() -> None:
    for scenario in CORPUS_DATA["scenarios"]:
        for mode, spec in scenario["modes"].items():
            for node in spec.get("evidence") or []:
                file_name, _, function = node.partition("::")
                path = ROOT / file_name
                assert path.is_file(), f"{scenario['id']}/{mode}: нет файла {file_name}"
                assert function in _functions(path), f"{scenario['id']}/{mode}: нет теста {node}"


def test_preset_statuses_are_only_blocked_or_not_run_with_reason() -> None:
    for scenario in CORPUS_DATA["scenarios"]:
        for mode, spec in scenario["modes"].items():
            if spec.get("evidence"):
                assert "status" not in spec, "статус по доказательствам ставит прогонщик"
            else:
                assert spec["status"] in ("BLOCKED", "NOT_RUN") and spec["reason"], (scenario["id"], mode)
        live = scenario["modes"].get("live")
        if live is not None:
            assert not live.get("evidence"), "Live запрещён: доказательств Live пока быть не может"


def test_runner_never_gives_pass_without_evidence() -> None:
    corpus = {
        "scenarios": [
            {"id": "K-X", "title": "t", "requirements": ["R"], "modes": {"live": {"status": "PASS"}}},
            {
                "id": "K-Y",
                "title": "t",
                "requirements": ["R"],
                "modes": {"mock": {"evidence": ["tests/a.py::ok", "tests/a.py::gone"]}},
            },
            {
                "id": "K-Z",
                "title": "t",
                "requirements": ["R"],
                "modes": {"mock": {"evidence": ["tests/a.py::bad"]}},
            },
        ]
    }
    meta = {"version": "v", "environment": "e", "tested_at": "now"}
    records = evaluate_corpus(corpus, {"tests/a.py::ok": "passed", "tests/a.py::bad": "failed"}, meta)
    assert [record["status"] for record in records] == ["NOT_RUN", "NOT_RUN", "FAIL"]
    assert all(record["data_mode"] for record in records)
