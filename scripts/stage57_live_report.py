#!/usr/bin/env python3
"""Read-only Stage57 audit summary. Does not display prompts, responses or secrets."""

import argparse
import collections
import json
import sqlite3

parser = argparse.ArgumentParser()
parser.add_argument("db", help="SQLite path (opened read-only)")
args = parser.parse_args()
con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
rows = con.execute(
    "SELECT event,request_id,payload_json FROM audit WHERE event IN "
    "('turn_started','turn_failed','turn_finished','llm_call','llm_http_attempt','pipeline_span')"
).fetchall()
by_event = collections.Counter(row[0] for row in rows)
spans = collections.defaultdict(list)
attempt_rows = []
for event, _request_id, payload in rows:
    data = json.loads(payload)
    if event == "pipeline_span":
        spans[data["stage"]].append(data["duration_ms"])
    elif event == "llm_http_attempt":
        attempt_rows.append(data)

# OBS-REPORT-PHASE-05: одна физическая HTTP-попытка — один attempt_id, сколько бы событий её ни описывало.
phases: dict[str, dict[str, dict]] = collections.defaultdict(dict)
anonymous = 0
for row in attempt_rows:
    attempt_id = row.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        anonymous += 1  # старая запись без id: считаем самостоятельной попыткой, не склеиваем
        attempt_id = f"__anonymous_{anonymous}"
    phase = row.get("phase") or "started"
    phases[attempt_id][phase] = row

attempts = list(phases.values())
finished = [item["finished"] for item in attempts if "finished" in item]
unfinished = [item for item in attempts if "finished" not in item]
http_errors = [
    row for row in finished if not isinstance(row.get("http_status"), int) or row["http_status"] >= 400
]
known_prompt = sum(row["prompt_tokens"] for row in finished if isinstance(row.get("prompt_tokens"), int))
known_completion = sum(
    row["completion_tokens"] for row in finished if isinstance(row.get("completion_tokens"), int)
)
report = {
    "turns_started": by_event["turn_started"],
    "turns_finished": by_event["turn_finished"],
    "turns_failed": by_event["turn_failed"],
    "logical_llm_calls": by_event["llm_call"],
    "http_attempts": len(attempts),
    "http_attempts_finished": len(finished),
    "http_attempts_unfinished": len(unfinished),
    "reported_prompt_tokens": known_prompt,
    "reported_completion_tokens": known_completion,
    "usage_unknown_attempts": sum(1 for row in finished if row.get("usage_status") != "reported"),
    "unfinished_usage_unknown": len(unfinished),
    "cost": "unknown",
    "cost_status": "unknown",
    # Незавершённая попытка — потенциальный расход, а не подтверждённая HTTP-ошибка.
    "http_errors": len(http_errors),
    "stage_ms": {
        key: {"count": len(value), "total": sum(value), "max": max(value)} for key, value in spans.items()
    },
}
print(json.dumps(report, indent=2, ensure_ascii=False))
