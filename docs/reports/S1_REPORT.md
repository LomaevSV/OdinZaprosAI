# Отчёт по срезу S1 — «Сквозной каркас на Mock и Docker»

**Дата:** 24.10.2026 · **План:** `ODIN_IMPLEMENTATION_PLAN_v0.5.2.md` §4 S1 · **Решения:** DEC-01..03, -05, -06, -08, -30, -36
**Окружение:** Windows, Python 3.12.7 (`.venv`, uv 0.11.6), pydantic 2.14, FastAPI 0.162, pytest 9.1, ruff 0.17. Сеть в тестах заблокирована.
**Режим данных всех проверок:** Mock. Live-проверок нет: ни один провайдер не утверждён (DEC-09).

## Реализовано
| Компонент плана S1 | Где |
|---|---|
| Проект на uv, конфигурация из окружения, `.env.example` | `pyproject.toml`, `src/odin/config.py`, `.env.example` |
| 19 нормативных типов (17 §7 + `Price` + `Availability`) с `schema_version`; экспорт JSON Schema; снимок OpenAPI | `src/odin/contracts/*`, `schemas/*.schema.json`, `schemas/openapi.json` |
| SQLite (WAL): сессии, ревизии (только добавление), `trip_heads` (current/last_agreed), корзины, чеки, аудит, события | `src/odin/storage.py` |
| JSON API §2.14 (все 14 путей), 409 на устаревшую ревизию, SSE-события | `src/odin/api.py` |
| Оркестратор: автомат хода, 2 волны, лимиты/дедлайн, `stop_reason`, атомарная запись ревизии, аудит | `src/odin/orchestrator.py` |
| Tool Registry: deny-by-default, `register_checkout_passengers` запрещён всегда, бюджет вызовов, дедлайн, 1 повтор, circuit breaker, семафоры; Mock-адаптеры с маркировкой «ДЕМО» | `src/odin/tools/*` |
| Ядро: `Decimal` и полнота итога, валюты не смешиваются, оценщики 6 полей закрытого словаря, AND/OR/FALLBACK/unparsed, классы RANK-01, доступность по строгой AVAIL-01, стыки «прибытие → заезд» и «выезд → обратно», TTL, таблица REPLAN-01 | `src/odin/core/*` |
| Корзина: два раздельных согласия, идемпотентный чек по `confirmation_id`, отказ без оформления, потеря права подтверждения после новой ревизии | `src/odin/core/cart.py`, оркестратор |
| LLM Gateway `off`/`replay` (строгий ключ), путь без ключа DEC-08: форма; свободный текст честно отклоняется | `src/odin/llm/*` |
| Composer по таблице фактов + проверка чисел, шаблон при нарушении | `src/odin/llm/composer.py` |
| Заготовка вдохновения: 3 кандидата из демо-каталога, без цены, визы «не проверено» | `src/odin/tools/mock_data.py` |
| Минимальный интерфейс (HTML/CSS/JS без сборки): чат, форма, «Моя поездка», карточки со значком режима/источником/«актуально на», условия, корзина, диалог с двумя согласиями, демо-чек, прогресс | `src/odin/static/*` |
| `Dockerfile`, `compose.yaml`, `.dockerignore`, `docs/DOCKER_CHECK.md`, README | корень, `docs/` |

## Результаты проверок
- `pytest`: **398 passed**, 0 skipped, 0 xfail, предупреждения — ошибки (кроме стороннего совета Starlette про `httpx2`).
- `ruff check .` и `ruff format --check .`: без замечаний. Снимки схем и OpenAPI актуальны.
- `/health` локально: **HTTP 200** (uvicorn на 127.0.0.1:18080, интерфейс `/` — 200).

| Тест плана | Статус | Доказательство |
|---|---|---|
| T-CON-SCHEMA-01 (схемы + OpenAPI) | PASS | `tests/contract/test_schema_snapshot.py` |
| T-UNIT-MONEY-01 | PASS | `tests/unit/test_money.py` |
| T-CON-TZ-01 | PASS | `tests/contract/test_timezone.py` |
| T-CON-NULL-01 | PASS | `tests/contract/test_null_semantics.py` |
| T-E2E-MOCK-01 сообщение → план → корзина → подтверждение → чек | PASS (Mock) | `tests/e2e/test_mock_flow.py` |
| T-E2E-MOCK-02 повтор подтверждения → тот же чек, без дубля | PASS (Mock) | там же |
| T-E2E-MOCK-03 отказ → ничего не оформлено | PASS (Mock) | там же |
| T-UNIT-ARCH-01, T-NEG-TOOL-BUDGET/-DEADLINE/-CB, T-SEARCH-STOP-01, T-CON-STATUS-01, T-CON-MOCK-01, T-LLM-MODE-01/-03, T-DLV-REG-01 | PASS | `tests/unit/test_tools.py`, `test_services.py`, e2e |
| T-DLV-SECRETS-01, T-DLV-README-01, T-DLV-MANIFEST-01 | PASS | `tests/delivery/test_delivery.py` |
| T-UI-01 структурная часть (DEC-36) | PASS | там же |
| T-UI-01 визуальный ручной чек-лист | NOT_RUN | по DEC-36 проходит руководитель/команда |
| T-DLV-DOCKER-01 | **BLOCKED** | DEC-06: нет внешней среды; инструкция `docs/DOCKER_CHECK.md` |
| Свободный диалог (ACCEPT-01 п. 3) | **BLOCKED** | DEC-07/DEC-08: ключа LLM нет |

Критерии завершения S1: pytest зелёный, T-E2E-MOCK-01..03 PASS, `/health` 200 локально, у каждого компонента значок Mock — **выполнены**; Docker — BLOCKED с причиной (допустимо по критерию «PASS только по журналу внешнего запуска, иначе BLOCKED»).

## Известные ограничения
- Только Mock: цены, наличие, расписание синтетические; доступность всегда `unknown`; итог с трансфером — `partial`.
- Стыки — `unknown` (буферы не утверждены, DEC-20); `matched` невозможен до DEC-20/DEC-19.
- Повторное использование компонентов при пересборке не выполняется (всё ищется заново) — допустимо, оптимизация S4.
- Демо-справочник: 8 городов; вне списка — честный отказ с `stop_reason=provider_unavailable`.
- Замечания из блока контрактов (не меняли нормативные схемы): деньги принимают целое число JSON; `ConnectionCheck.times_used` — строки; `Party.children_ages` по умолчанию `[]`.

## Открытые решения, влияющие на следующий срез
- **S2 заблокирован решениями руководителя:** DEC-04 (MCP-клиент, spike), DEC-07 (LLM: провайдер, ключ, передача текстов), DEC-09 (утверждение Live-провайдеров P0), DEC-10, DEC-11, DEC-15, DEC-22, DEC-23.
- DEC-06 — среда для Docker (эскалация по §2.24).
- Независимая от решений часть S3 (ядро, календарь, оценщики) может выполняться на Mock.
