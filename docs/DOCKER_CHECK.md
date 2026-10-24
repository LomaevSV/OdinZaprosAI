# Проверка Docker во внешней среде (T-DLV-DOCKER-01, DEC-06)

**Статус на 24.10.2026: BLOCKED.** Внешней среды нет (DEC-06). Локально Docker недоступен:
Docker Toolbox 19.03 без ВМ, VirtualBox не установлен, виртуализация в BIOS выключена (план §1.3).
PASS ставится **только** по журналу фактического запуска.

## Что нужно от среды
- Docker Engine ≥ 24 с Compose v2, доступ к `ghcr.io` и `pypi.org` во время сборки.
- Платный ключ не нужен: Mock-путь работает с `ODIN_DATA_MODE=mock`, `ODIN_LLM_MODE=off`.

## Шаги (выполнять из корня репозитория)
```sh
docker --version && docker compose version
cp .env.example .env              # значения по умолчанию: mock + off
docker compose build 2>&1 | tee docker_build.log
docker image ls odin-zapros:s1    # ориентир: ≤ 300 МБ (план §2.16)
docker compose up -d
sleep 10
curl -fsS http://127.0.0.1:8080/health | tee health.json
docker compose ps                 # STATUS должен быть healthy
```

## E2E на Mock внутри контейнера
```sh
docker compose exec odin python - <<'PY'
import json, urllib.request
base = "http://127.0.0.1:8080/api/v1"
def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))
s = call("POST", "/sessions")
form = {"origin": "Москва", "destination": "Казань", "date_from": "2026-11-20",
        "date_to": "2026-11-22", "adults": 2, "return_trip": True}
t = call("POST", f"/sessions/{s['session_id']}/messages",
         {"request_id": "req-docker-0001", "trip_revision": 1, "form": form})
c = call("POST", f"/trips/{t['trip_id']}/cart", {"request_id": "req-docker-0002", "trip_revision": t["trip_revision"]})
body = {"request_id": "req-docker-0003", "trip_revision": c["trip_revision"], "confirmation_id": "conf-docker-0001",
        "purchase_consent": True, "risk_acknowledgement": True}
r1 = call("POST", f"/carts/{c['cart']['cart_id']}/confirm", body)
r2 = call("POST", f"/carts/{c['cart']['cart_id']}/confirm", dict(body, request_id="req-docker-0004"))
assert r1["receipt"]["receipt_id"] == r2["receipt"]["receipt_id"] and r2["idempotent_replay"]
print("E2E MOCK OK", r1["receipt"]["receipt_id"], r1["receipt"]["notice"])
PY
docker compose logs --no-color > docker_run.log
docker compose down
```

## Что приложить к отчёту
`docker_build.log`, `health.json`, `docker_run.log`, вывод `docker image ls`, версия Docker и ОС среды,
хеш коммита. Без этих журналов статус T-DLV-DOCKER-01 остаётся **BLOCKED**.
