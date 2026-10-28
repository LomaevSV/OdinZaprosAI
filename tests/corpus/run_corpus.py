"""Прогонщик корпуса и метрик: ``python -m tests.corpus.run_corpus [--out docs/reports] [--skip-pytest]``.

1. Один прогон всего pytest с JUnit-отчётом (сеть заблокирована фикстурой, Live не вызывается).
2. Статусы корпуса по режимам: PASS — только если все доказательства прошли (AUDIT-01);
   FAIL — если хоть одно упало; NOT_RUN — если доказательство не найдено; BLOCKED — предустановлено
   в корпусе с причиной. Mock/Replay не засчитываются за Live (TEST-02).
3. Метрики METRIC-01 на сценариях Mock (без порогов — OPEN-05).
Результат: ``test_report.md`` и ``metrics.json``.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).with_name("test_corpus.yaml")
LEVELS = ("unit", "contract", "adapter", "e2e", "delivery", "corpus")


# --------------------------------------------------------------------- pytest и JUnit


def run_pytest(junit: Path) -> int:
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return completed.returncode


def parse_junit(junit: Path) -> dict[str, str]:
    """``путь::функция`` → passed/failed/skipped; параметризованные варианты сводятся строго."""
    outcomes: dict[str, list[str]] = defaultdict(list)
    for case in ET.parse(junit).getroot().iter("testcase"):
        classname = case.get("classname", "")
        module = classname.split(".")
        node = "/".join(module) + ".py::" + case.get("name", "").split("[")[0]
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes[node].append("failed")
        elif case.find("skipped") is not None:
            outcomes[node].append("skipped")
        else:
            outcomes[node].append("passed")
    result = {}
    for node, values in outcomes.items():
        if "failed" in values:
            result[node] = "failed"
        elif all(value == "passed" for value in values):
            result[node] = "passed"
        else:
            result[node] = "skipped"
    return result


def level_summary(outcomes: dict[str, str]) -> dict[str, Counter[str]]:
    summary: dict[str, Counter[str]] = {level: Counter() for level in LEVELS}
    for node, outcome in outcomes.items():
        parts = node.split("/")
        level = parts[1] if len(parts) > 2 and parts[1] in LEVELS else "unit"
        summary[level][outcome] += 1
    return summary


# --------------------------------------------------------------------- корпус


def git_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", ".", ":!docs/reports"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def evaluate_corpus(
    corpus: dict[str, Any], outcomes: dict[str, str], meta: dict[str, str]
) -> list[dict[str, Any]]:
    """Записи AUDIT-01: по одной на сценарий и режим."""
    records = []
    for scenario in corpus["scenarios"]:
        for mode, spec in scenario["modes"].items():
            evidence = list(spec.get("evidence") or [])
            if evidence:
                results = {node: outcomes.get(node, "missing") for node in evidence}
                if any(value == "failed" for value in results.values()):
                    status = "FAIL"
                elif any(value in ("missing", "skipped") for value in results.values()):
                    status = "NOT_RUN"
                else:
                    status = "PASS"
                passed = sum(value == "passed" for value in results.values())
                actual = f"{passed}/{len(evidence)} доказательств прошли"
                missing = [node for node, value in results.items() if value != "passed"]
                if missing:
                    actual += "; не прошли/не найдены: " + ", ".join(missing)
            else:
                status = spec.get("status", "NOT_RUN")
                if status == "PASS":
                    status = "NOT_RUN"  # PASS без доказательств запрещён (AUDIT-01)
                actual = spec.get("reason", "доказательств нет")
            records.append(
                {
                    "requirement_id": ", ".join(scenario.get("requirements", [])),
                    "test_id": scenario["id"],
                    "title": scenario["title"],
                    "expected": spec.get("expected", "см. сценарий"),
                    "actual": actual,
                    "scope": spec.get("scope"),
                    "evidence": evidence,
                    "status": status,
                    "version": meta["version"],
                    "environment": meta["environment"],
                    "data_mode": mode,
                    "tested_at": meta["tested_at"],
                }
            )
    return records


# --------------------------------------------------------------------- метрики METRIC-01 (Mock)


def collect_metrics() -> dict[str, Any]:
    """Сценарии Mock через JSON API; считаются только фактически измеренные величины."""
    from fastapi.testclient import TestClient

    from odin.api import create_app
    from odin.app import build_application
    from odin.config import Settings
    from tests.conftest import TRIP_END, TRIP_START, trip_form

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings.from_env({"ODIN_DB_PATH": str(Path(tmp) / "metrics.sqlite3")})
        application = build_application(settings)
        tool_results: list[str] = []
        original = application.registry.invoke

        def spy(request: Any) -> Any:
            result = original(request)
            tool_results.append(getattr(result, "status", "error"))
            return result

        application.registry.invoke = spy  # type: ignore[method-assign]
        counter = iter(range(1, 10_000))

        def rid() -> str:
            return f"req-metric-{next(counter):05d}"

        with TestClient(create_app(settings, application=application)) as client:
            full_paths, latencies, first_useful, rounds, questions = 0, [], [], [], []
            completed = 0
            components: list[dict[str, Any]] = []
            connection_violations = 0
            plans = 0
            for overrides in ({"adults": None}, {"origin": None}, {}):
                full_paths += 1
                started = time.perf_counter()
                session = client.post("/api/v1/sessions").json()
                revision = session["trip_revision"]
                turn = client.post(
                    f"/api/v1/sessions/{session['session_id']}/messages",
                    json={"request_id": rid(), "trip_revision": revision, "form": trip_form(**overrides)},
                ).json()
                scenario_rounds, scenario_questions = 0, 0
                while turn["questions"] and scenario_rounds < 3:
                    scenario_rounds += 1
                    scenario_questions += len(turn["questions"])
                    turn = client.post(
                        f"/api/v1/sessions/{session['session_id']}/messages",
                        json={
                            "request_id": rid(),
                            "trip_revision": turn["trip_revision"],
                            "form": trip_form(),
                        },
                    ).json()
                rounds.append(scenario_rounds)
                questions.append(scenario_questions)
                if turn["state"]["plan"]:
                    first_useful.append((time.perf_counter() - started) * 1000)
                    plans += 1
                    plan = turn["state"]["plan"]
                    components.extend(plan["components"])
                    connection_violations += sum(c["status"] == "violated" for c in plan["connection_checks"])
                cart = client.post(
                    f"/api/v1/trips/{turn['trip_id']}/cart",
                    json={"request_id": rid(), "trip_revision": turn["trip_revision"]},
                )
                if cart.status_code == 201:
                    body = cart.json()
                    confirm = client.post(
                        f"/api/v1/carts/{body['cart']['cart_id']}/confirm",
                        json={
                            "request_id": rid(),
                            "trip_revision": body["trip_revision"],
                            "confirmation_id": f"conf-{rid()}",
                            "purchase_consent": True,
                            "risk_acknowledgement": True,
                        },
                    )
                    completed += confirm.status_code == 200
                latencies.append((time.perf_counter() - started) * 1000)

            replans, replans_ok = 0, 0
            changes = [
                {
                    "date_from": (TRIP_START + timedelta(days=7)).isoformat(),
                    "date_to": (TRIP_END + timedelta(days=7)).isoformat(),
                },
                {"budget_amount": "20000", "budget_currency": "RUB"},
                {"pets": [{"kind": "dog", "count": 1}]},
                {"adults": 3},
            ]
            for change in changes:
                session = client.post("/api/v1/sessions").json()
                form = trip_form(
                    breakfast=True, budget_amount="300000", budget_currency="RUB", budget_required=True
                )
                turn = client.post(
                    f"/api/v1/sessions/{session['session_id']}/messages",
                    json={"request_id": rid(), "trip_revision": session["trip_revision"], "form": form},
                ).json()
                before = {
                    c["constraint_id"]: c["requiredness"] for c in turn["state"]["request"]["constraints"]
                }
                replans += 1
                response = client.post(
                    f"/api/v1/trips/{turn['trip_id']}/replan",
                    json={"request_id": rid(), "trip_revision": turn["trip_revision"], "change_set": change},
                )
                if response.status_code == 200 and response.json()["state"]["plan"]:
                    after = {
                        c["constraint_id"]: c["requiredness"]
                        for c in response.json()["state"]["request"]["constraints"]
                    }
                    replans_ok += all(after.get(cid) == req for cid, req in before.items())

    sourced = sum(
        1 for c in components if c.get("data_mode") in ("mock", "replay", "live") and c.get("offer_id")
    )
    modes = Counter(c["data_mode"] for c in components)
    errors = sum(status in ("error", "unavailable", "timeout") for status in tool_results)

    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    return {
        "schema_version": "1.0.0",
        "data_mode": "mock",
        "thresholds": "не установлены: согласуются после первых измерений (METRIC-01, OPEN-05)",
        "not_measured": {
            "live": "BLOCKED: Live запрещён до DEC-04, DEC-09, DEC-11",
            "replay": "BLOCKED: Replay-снимков нет (DEC-24)",
            "mixed": "BLOCKED: требует Live",
        },
        "metrics": {
            "e2e_completion_rate": {
                "value": round(completed / full_paths, 3),
                "method": "демо-чек получен / сценарии полного пути",
                "n": full_paths,
            },
            "clarification_rounds_avg": {
                "value": avg(rounds),
                "method": "раунды уточнения до плана",
                "n": len(rounds),
            },
            "clarification_questions_avg": {
                "value": avg(questions),
                "method": "вопросы за сценарий",
                "n": len(questions),
            },
            "time_to_first_useful_recommendation_ms": {
                "value": avg(first_useful),
                "method": "от первого сообщения до плана, локально, Mock",
                "n": len(first_useful),
            },
            "full_scenario_latency_ms": {
                "value": avg(latencies),
                "method": "от сессии до демо-чека, локально, Mock",
                "n": len(latencies),
            },
            "connection_violations": {
                "value": connection_violations,
                "method": "violated-стыки в построенных планах",
                "n": plans,
            },
            "replan_correct_share": {
                "value": round(replans_ok / replans, 3),
                "method": "пересборка с планом и сохранёнными условиями / все пересборки",
                "n": replans,
            },
            "facts_with_source_share": {
                "value": round(sourced / len(components), 3) if components else None,
                "method": "компоненты плана с оффером и режимом данных / все компоненты",
                "n": len(components),
            },
            "tool_error_share": {
                "value": round(errors / len(tool_results), 3) if tool_results else None,
                "method": "ошибочные результаты инструментов / все вызовы",
                "n": len(tool_results),
            },
            "api_calls": {"value": len(tool_results), "method": "вызовы инструментов через Registry"},
            "llm_calls": {"value": 0, "method": "LLM off (DEC-07)"},
            "llm_cost": {"value": 0, "method": "LLM off (DEC-07)"},
            "fallback_frequency": {
                "value": 0,
                "method": "Live→Replay и FALLBACK-активации; в Mock без LLM не возникают",
            },
            "data_mode_distribution": {"value": dict(modes), "method": "компоненты планов по режимам"},
        },
    }


# --------------------------------------------------------------------- отчёт


def write_report(
    records: list[dict[str, Any]], summary: dict[str, Counter[str]], meta: dict[str, str], out: Path
) -> None:
    by_status = Counter(record["status"] for record in records)
    lines = [
        "# Отчёт о тестировании (AUDIT-01)",
        "",
        f"- **Версия:** `{meta['version']}` · **Дата:** {meta['tested_at']}",
        f"- **Среда:** {meta['environment']}",
        "- **Правило:** PASS — только при прохождении всех доказательств; Mock и Replay не засчитываются за Live (TEST-02).",
        "",
        "## Уровни тестов (ACCEPT-01 п. 8)",
        "",
        "Счёт по тестовым функциям; параметризованные варианты сведены строго (одно падение — failed).",
        "",
        "| Уровень | passed | failed | skipped |",
        "|---|---|---|---|",
    ]
    for level, counts in summary.items():
        if counts:
            lines.append(f"| {level} | {counts['passed']} | {counts['failed']} | {counts['skipped']} |")
    lines += [
        "",
        "## Корпус K-01..K-30 по режимам",
        "",
        "Итого: " + ", ".join(f"{status} — {count}" for status, count in sorted(by_status.items())),
        "",
        "| test_id | data_mode | status | requirement_id | expected | actual | evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in records:
        evidence = "<br>".join(f"`{node.split('::')[-1]}`" for node in record["evidence"]) or "—"
        actual = record["actual"] + (f" ({record['scope']})" if record.get("scope") else "")
        lines.append(
            f"| {record['test_id']} | {record['data_mode']} | {record['status']} | {record['requirement_id']} "
            f"| {record['expected']} | {actual} | {evidence} |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "reports")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    corpus = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    meta = {
        "version": git_version(),
        "environment": f"локально, Python {platform.python_version()}, {platform.system()}; внешняя сеть заблокирована",
        "tested_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
    }
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        code = run_pytest(junit)
        outcomes = parse_junit(junit)
    records = evaluate_corpus(corpus, outcomes, meta)
    write_report(records, level_summary(outcomes), meta, args.out / "test_report.md")
    metrics = {**collect_metrics(), "version": meta["version"], "measured_at": meta["tested_at"]}
    (args.out / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    statuses = Counter(record["status"] for record in records)
    print(f"pytest exit={code}; корпус: {dict(statuses)}")
    return 0 if code == 0 and statuses.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
