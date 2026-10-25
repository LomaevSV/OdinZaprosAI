# Проверка Docker в отдельной среде (T-DLV-DOCKER-01, DEC-06, «Правила по технике» — DEC-32)

**Статус на 25.10.2026: готово к проверке, результат — BLOCKED до журнала запуска.** В среде разработки Docker
недоступен (Docker Toolbox без ВМ, виртуализация выключена — план §1.3). PASS ставится **только** по журналу
фактического запуска в отдельной среде. Сама сквозная самопроверка (`python -m odin.selfcheck`) проверена на хосте
без Docker (локальный сервер, Mock) — это не доказательство работы образа.

## Требования к среде
- Docker Engine ≥ 24 и Docker Compose ≥ 2.24 (используется `env_file` с `required: false`; на старых версиях
  Compose — ошибка разбора `env_file`). Docker Desktop на Windows/macOS — с включённой виртуализацией (WSL2 / VM).
- Свободный порт 8080; ≈ 1 ГБ свободного диска на сборку, 512 МБ RAM.
- Сеть при сборке: `ghcr.io` (образ uv), `pypi.org`, Docker Hub (`python:3.12.7-slim-bookworm`).
- Сеть при работе — только в режиме `live`/`mixed`: `mcp.tutu.ru`, `geocoding-api.open-meteo.com`,
  `router.project-osrm.org` (и адрес LLM, если задан ключ).
- Ключи не нужны. Без `.env` режим данных `auto` → `mock` (ДЕМО, без сети), LLM выключен.
- Apple Silicon: образ собирается под arm64; при проблемах — раскомментировать `platform: linux/amd64` в Compose.

## Шаги (из корня репозитория, как у жюри)
Linux/macOS (bash) и Windows (PowerShell) — одинаковые команды, кроме отмеченных.
```sh
docker --version
docker compose version                       # ≥ 2.24
git rev-parse HEAD
docker compose up --build -d                 # журнал: docker compose logs > docker_up.log
docker compose ps                            # STATUS: (healthy) — ожидание до ~30 с
docker compose exec app python -m odin.diagnose        # режимы без сети; ключ не выводится
docker compose exec app python -m odin.selfcheck       # форма → план → корзина → демо-чек → повтор
curl -fsS http://localhost:8080/health                 # Windows PowerShell: curl.exe -fsS ...
docker image ls odin-zapros:0.2.0                      # ориентир размера: ≤ 300 МБ
docker compose logs --no-color app > docker_run.log
docker compose down
```
Ожидаемо:
- `docker compose ps` — `healthy`;
- `diagnose` — `"ok": true`, `"data_mode": "mock"`, `"data_mode_auto": true`, `"llm": {"mode": "off", ...}`
  (с ключом LLM в `.env`: `"data_mode": "live"`, `"mode": "live"`, `"key": "present"`);
- `selfcheck` — `"result": "PASS"`, `"plan_mode": "mock"` (без ключа), `"idempotent_replay": true`,
  `"notice": "Демонстрационная покупка. Реального бронирования и списания средств нет"`;
- `/health` — `"status": "ok"`; `http://localhost:8080/` открывается в браузере.

## Проверка Live-данных в контейнере (по желанию, нужна сеть)
В `.env`: `ODIN_DATA_MODE=live`, затем `docker compose up -d` и `docker compose exec app python -m odin.selfcheck`.
Ожидаемо `"plan_mode": "live"`; при недоступности источников план честно неполный (`unknown`), без подмены
на ДЕМО. Каждый прогон — порядка 10 бесплатных запросов только чтения к Tutu/Open-Meteo/OSRM (поиск в обе
стороны, гостиницы, детали лучших гостиниц, трансфер).

## Типовые ошибки
| Симптом | Действие |
|---|---|
| `env_file ... required` не распознан | обновить Docker Compose до ≥ 2.24 |
| `port is already allocated` | освободить 8080 или поменять левую часть `ports` в `docker-compose.yml` |
| `unhealthy` | `docker compose logs app`; частая причина — `ConfigError` в `.env` (текст называет переменную) |
| Сборка падает на загрузке пакетов | нет доступа к `ghcr.io`/`pypi.org` — проверить прокси/сеть |
| Windows: `curl` выводит таблицу PowerShell | использовать `curl.exe` или открыть адрес в браузере |

## Что приложить к отчёту
`docker_up.log`, `docker_run.log`, вывод `docker compose ps`, `diagnose`, `selfcheck`, `docker image ls`,
версии Docker и ОС, хеш коммита. Без этих журналов статус T-DLV-DOCKER-01 остаётся **BLOCKED**.
