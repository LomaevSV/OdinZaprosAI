# Проверка Docker в отдельной среде (T-DLV-DOCKER-01, DEC-06, «Правила по технике» — DEC-32)

**Статус на 25.10.2026: готово к проверке, результат — BLOCKED до журнала запуска.** В среде разработки Docker
недоступен (Docker Toolbox без ВМ, виртуализация выключена — план §1.3). PASS ставится **только** по журналу
фактического запуска в отдельной среде.

## Требования к среде
- Docker Engine ≥ 24 и Docker Compose ≥ 2.24 (используется `env_file` с `required: false`).
- Доступ к `ghcr.io` и `pypi.org` во время сборки; для режима `mixed` — к `mcp.tutu.ru`,
  `geocoding-api.open-meteo.com`, `router.project-osrm.org` во время работы (без сети — ДЕМО с пометкой).
- Платные ключи не нужны. Ключ LLM — по желанию, в `.env`.

## Шаги (из корня репозитория, как у жюри)
```sh
docker --version && docker compose version
git rev-parse HEAD
cp .env.example .env                       # необязательно: compose читает .env.example сам
docker compose up --build -d 2>&1 | tee docker_up.log
docker compose ps                           # STATUS: healthy (ожидание до ~30 с)
curl -fsS http://localhost:8080/health | tee health.json
curl -fsS -o /dev/null -w "%{http_code}\n" http://localhost:8080/   # 200 — интерфейс
docker image ls odin-zapros:0.2.0           # ориентир размера: ≤ 300 МБ
```
Ожидаемо: `health.json` содержит `"status":"ok"`, `"data_mode":"mixed"`, `"llm_mode":"off"`
(или `live`, если в `.env` задан ключ).

## Сквозной сценарий внутри контейнера
```sh
docker compose exec app python - <<'PY'
import json, urllib.request
from datetime import date, timedelta
base = "http://127.0.0.1:8080/api/v1"
def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))
start = date.today() + timedelta(days=30)
s = call("POST", "/sessions")
form = {"origin": "Москва", "destination": "Казань", "date_from": start.isoformat(),
        "date_to": (start + timedelta(days=2)).isoformat(), "adults": 2, "return_trip": True}
t = call("POST", f"/sessions/{s['session_id']}/messages",
         {"request_id": "req-docker-0001", "trip_revision": 1, "form": form})
print("plan mode:", t["state"]["plan"]["trip_data_mode"], t["state"]["plan"]["data_mode_breakdown"])
c = call("POST", f"/trips/{t['trip_id']}/cart", {"request_id": "req-docker-0002", "trip_revision": t["trip_revision"]})
body = {"request_id": "req-docker-0003", "trip_revision": c["trip_revision"], "confirmation_id": "conf-docker-0001",
        "purchase_consent": True, "risk_acknowledgement": True}
r1 = call("POST", f"/carts/{c['cart']['cart_id']}/confirm", body)
r2 = call("POST", f"/carts/{c['cart']['cart_id']}/confirm", dict(body, request_id="req-docker-0004"))
assert r1["receipt"]["receipt_id"] == r2["receipt"]["receipt_id"] and r2["idempotent_replay"]
print("E2E OK", r1["receipt"]["receipt_id"], r1["receipt"]["notice"])
PY
docker compose logs --no-color app > docker_run.log
docker compose down
```
Без сети Live-компоненты заменяются ДЕМО по компоненту (`plan mode: mock`/`mixed`) — это ожидаемое поведение
(DEC-23), сценарий всё равно проходит. Строго Mock: `ODIN_DATA_MODE=mock` в `.env`.

## Что приложить к отчёту
`docker_up.log`, `health.json`, `docker_run.log`, вывод `docker compose ps` и `docker image ls`, версии Docker
и ОС, хеш коммита. Без этих журналов статус T-DLV-DOCKER-01 остаётся **BLOCKED**.
