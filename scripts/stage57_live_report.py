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
    "SELECT event,request_id,payload_json FROM audit WHERE event IN ('turn_started','turn_failed','turn_finished','llm_call','llm_http_attempt','pipeline_span')"
).fetchall()
by_event = collections.Counter(row[0] for row in rows)
attempts = [json.loads(payload) for event, _, payload in rows if event == "llm_http_attempt"]
spans = collections.defaultdict(list)
for event, _, payload in rows:
    if event == "pipeline_span":
        d = json.loads(payload)
        spans[d["stage"]].append(d["duration_ms"])
known_prompt = sum(a["prompt_tokens"] for a in attempts if a.get("prompt_tokens") is not None)
known_completion = sum(a["completion_tokens"] for a in attempts if a.get("completion_tokens") is not None)
report = {
    "turns_started": by_event["turn_started"],
    "turns_finished": by_event["turn_finished"],
    "turns_failed": by_event["turn_failed"],
    "logical_llm_calls": by_event["llm_call"],
    "http_attempts": len(attempts),
    "reported_prompt_tokens": known_prompt,
    "reported_completion_tokens": known_completion,
    "usage_unknown_attempts": sum(a.get("usage_status") != "reported" for a in attempts),
    "cost": "unknown",
    "cost_status": "unknown",
    "http_errors": sum(a.get("http_status") is None or a.get("http_status", 0) >= 400 for a in attempts),
    "stage_ms": {k: {"count": len(v), "total": sum(v), "max": max(v)} for k, v in spans.items()},
}
print(json.dumps(report, indent=2, ensure_ascii=False))
