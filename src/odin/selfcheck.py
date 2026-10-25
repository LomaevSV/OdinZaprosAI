"""Сквозная самопроверка запущенного сервиса: ``python -m odin.selfcheck [base_url]``.

Форма (без LLM и без платных вызовов) → план → корзина → два согласия → демо-чек → повтор подтверждения
(тот же чек). Только стандартная библиотека: работает и внутри контейнера
(``docker compose exec app python -m odin.selfcheck``), и на хосте. Код возврата 0 — PASS, 1 — FAIL.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8080"


def _call(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 — адрес задаёт оператор
        return json.load(response)


def run(base: str = DEFAULT_BASE) -> dict[str, Any]:
    api = base.rstrip("/") + "/api/v1"
    health = _call(base.rstrip("/"), "GET", "/health")
    session = _call(api, "POST", "/sessions")
    start = date.today() + timedelta(days=30)
    form = {
        "origin": "Москва",
        "destination": "Казань",
        "date_from": start.isoformat(),
        "date_to": (start + timedelta(days=2)).isoformat(),
        "adults": 2,
        "return_trip": True,
    }
    turn = _call(
        api,
        "POST",
        f"/sessions/{session['session_id']}/messages",
        {"request_id": "req-selfcheck-0001", "trip_revision": session["trip_revision"], "form": form},
    )
    plan = turn["state"]["plan"]
    if turn["status"] != "planned" or not plan or not plan["components"]:
        raise AssertionError(f"план не построен: status={turn['status']}")
    cart = _call(
        api,
        "POST",
        f"/trips/{turn['trip_id']}/cart",
        {"request_id": "req-selfcheck-0002", "trip_revision": turn["trip_revision"]},
    )
    body = {
        "request_id": "req-selfcheck-0003",
        "trip_revision": cart["trip_revision"],
        "confirmation_id": "conf-selfcheck-0001",
        "purchase_consent": True,
        "risk_acknowledgement": True,
    }
    first = _call(api, "POST", f"/carts/{cart['cart']['cart_id']}/confirm", body)
    again = _call(
        api, "POST", f"/carts/{cart['cart']['cart_id']}/confirm", {**body, "request_id": "req-selfcheck-0004"}
    )
    if first["receipt"]["receipt_id"] != again["receipt"]["receipt_id"]:
        raise AssertionError("повтор подтверждения создал новый чек")
    return {
        "health": {key: health.get(key) for key in ("status", "data_mode", "llm_mode")},
        "plan_mode": plan["trip_data_mode"],
        "components": {c["role"]: c["data_mode"] for c in plan["components"]},
        "completeness": plan["completeness"],
        "receipt_id": first["receipt"]["receipt_id"],
        "notice": first["receipt"]["notice"],
        "idempotent_replay": again.get("idempotent_replay"),
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = args[0] if args else DEFAULT_BASE
    try:
        result = run(base)
    except (AssertionError, KeyError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(json.dumps({"result": "FAIL", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps({"result": "PASS", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
