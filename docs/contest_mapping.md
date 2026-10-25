# Матрица соответствия конкурсному заданию (`contest_mapping.md`)

**Статус:** 25.10.2026. «Правила по технике» организаторов получены (DEC-32, раздел ниже); официальный текст задания 2026 у разработчика отсутствует (OPEN-01).
Строки ниже построены только по положениям ТЗ 0.7 с меткой `[ORG-2026]` («положение, следующее из опубликованного задания в доступном команде изложении»).
После получения официального текста каждая строка сверяется дословно; расхождения фиксируются как поправка, а не исправляются молча.

Статусы: `PASS` — есть доказательство; `FAIL`; `BLOCKED` — нет условия для проверки; `NOT_RUN` — не выполнялось.
Доказательства по корпусу — `docs/reports/test_report.md` (AUDIT-01); Mock не засчитывается за Live (TEST-02).

| Положение задания (по ТЗ 0.7) | Источник в ТЗ | Модуль / артефакт | Тест / доказательство | Статус (S6) |
|---|---|---|---|---|
| Прототип AI-турагента: вдохновение → направление → размещение, транспорт, трансфер → сравнение → план и корзина → имитация покупки → пересборка | GOAL-01 [ORG-2026] | оркестратор, домены, ядро, корзина, пересборка, UI | T-E2E-MOCK-01..03, T-E2E-MOCK-10..14, K-01, K-22..K-26 — PASS (Mock); T-E2E-LIVE-01/02 | Mock — PASS; Live — адаптеры PASS на фикстурах, сквозной Live-прогон — явный тест `tests/live` (NOT_RUN) |
| Уточняющие вопросы без длинной анкеты | GOAL-01 [ORG-2026], UC-02 | `core/clarify.py` | K-05..K-07, T-UNIT-CLARIFY-01..03 — PASS (форма); свободный диалог | форма — PASS; конвейер LLM-диалога — PASS на двойнике (`tests/e2e/test_llm_dialog.py`); реальная модель — ждёт ключа (DEC-07) |
| Заранее неизвестные запросы на защите | GOAL-01 [ORG-2026], UC-10 | оркестратор, матрица UC-10 | K-30, `tests/e2e/test_uc10_matrix.py` — PASS (Mock) | Mock — PASS; Live — BLOCKED |
| Пересборка без потери условий; отрицательные случаи по датам, бюджету, животному | ACCEPT-01 п. 3, UC-09 | менеджер пересборки, REPLAN-01 | K-22..K-25, T-E2E-MOCK-10..14 — PASS (Mock) | Mock — PASS; на LLM-диалоге — BLOCKED (DEC-07) |
| API, MCP, данные Островка не предоставляются; внешние источники, открытые и синтетические данные — с раскрытием происхождения | GOAL-02 [ORG-2026] | `config/provider_registry.yaml`, `docs/data_manifest.yaml`, маркировка `data_mode` | T-DLV-MANIFEST-01, K-29 — PASS | Mock-маркировка — PASS; Live-адаптеры Tutu/Open-Meteo/OSRM — PASS на синтетических фикстурах (`tests/adapter/test_live_adapters.py`); живой прогон — явный тест `tests/live` |
| Реальная покупка не требуется, достаточно имитации | GOAL-02 [ORG-2026], UC-08 | корзина, демо-чек | K-26, T-E2E-MOCK-01..03, перехват checkout — PASS | PASS (Mock) |
| Live / Replay / Mock допустимы только с видимой маркировкой | DATA-MODE-01 [ORG-2026]/[OWN] | маршрутизатор режимов, UI | K-28 (Replay) и K-29 (Mock) — PASS; T-UI-01 (структура) | Mock/Replay — PASS; Live — BLOCKED |
| Метрики, отчёт о тестировании, манифесты | METRIC-01, AUDIT-01, §12 | `tests/corpus/run_corpus.py` | `docs/reports/metrics.json`, `docs/reports/test_report.md`, `docs/release_manifest.yaml` | PASS для Mock; Live/Replay-метрики — BLOCKED |
| Чистый запуск в Docker | ACCEPT-01 п. 1, DELIVERY-01 | `Dockerfile`, `docker-compose.yml`, `docs/DOCKER_CHECK.md` | T-DLV-DOCKER-01 | готово к проверке; BLOCKED до журнала запуска (DEC-06) |
| Формат сдачи, видео, Docker, порт, сеть, модель жюри, срок | OPEN-01 | см. раздел «Правила по технике» ниже | — | частично (DEC-32) |
| Права на результаты, п. 7.3 | LEGAL-01, OPEN-04 | — | — | BLOCKED (DEC-33) |

## «Правила по технике» организаторов (получены 25.10.2026, DEC-32)

Источник: https://docs.ostrovok.tech/s/b9e6ec5e-5ad6-4091-9c85-a48eee9b21cc

| Правило | Где выполнено | Проверка | Статус |
|---|---|---|---|
| Обязательные файлы `README.md`, `docker-compose.yml`, `Dockerfile`, `.env.example` | корень репозитория | `test_docker_files_follow_organizer_rules` | PASS (структурно) |
| README: описание, видео, быстрый старт до `localhost:8080`, ресурсы, переменные с умолчаниями, сидирование, маршруты `/`, `/health` | `README.md` | `test_readme_explains_start_checks_and_demo_limits` | PASS; ссылка на видео — TODO |
| Контейнер слушает 8080, Compose `8080:8080` | `Dockerfile`, `docker-compose.yml` | тест поставки; локальный запуск 25.10: `/health` 200, `/` 200 | PASS локально; Docker — BLOCKED |
| Multi-stage, без dev-инструментов в рантайме, не root | `Dockerfile` (builder/runtime, `USER odin`) | тест поставки | PASS (структурно) |
| Секреты только в `.env`/env_file | `.env.example` без значений секретов, `.env` в `.gitignore` | T-DLV-SECRETS-01 | PASS |
| `/health` и healthcheck в Compose | `src/odin/api.py`, `docker-compose.yml` | тест поставки, T-E2E-API-01 | PASS |
| `docker compose up --build` без аргументов; `env_file` из `.env`/`.env.example` | `docker-compose.yml` | — | готово; BLOCKED до запуска |
| `depends_on` + healthcheck | зависимых сервисов нет (SQLite в томе) | — | не применимо |
| Фиксированные версии образов и зависимостей | `python:3.12.7-slim-bookworm`, `uv:0.11.6`, `uv.lock` | тест поставки | PASS |
| Логи в stdout/stderr | `logging_setup.py` | — | PASS |
| Сидирование/миграции на старте | схема SQLite создаётся при старте | — | PASS |
| Модели — через OpenAI-совместимое API | `src/odin/llm/openai_client.py` | `tests/unit/test_llm_live.py` | PASS (на двойнике); Live — ключ у руководителя |
| Без ручных доустановок | только Docker/Compose и копирование `.env` | — | готово |
| Рекомендовано: Makefile, CI | `Makefile`, `.github/workflows/ci.yml` | — | готово |
| Тег `submission`, репозиторий организации, видео | — | — | вне разработки (руководитель) |
