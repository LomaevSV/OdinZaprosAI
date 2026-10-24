# О!дин запрос

Прототип AI-турагента (трек «О!дин запрос»): запрос → интерпретация → оркестратор → данные →
проверка условий → поездка → интерфейс → корзина → демо-покупка.

> **Статус: срез S1, только ДЕМО-данные (Mock).** Цены, наличие и расписание не настоящие.
> Демонстрационная покупка. Реального бронирования и списания средств нет.
> Свободный диалог требует LLM (DEC-07) и в S1 недоступен — используется структурированная форма (DEC-08).

## Быстрый старт (локально)
Требуется Python 3.12 и [uv](https://docs.astral.sh/uv/).

```sh
uv sync                       # зависимости из uv.lock в .venv
cp .env.example .env          # ODIN_DATA_MODE=mock, ODIN_LLM_MODE=off
uv run python -m odin.api     # http://127.0.0.1:8080
```

Откройте `http://127.0.0.1:8080/` — интерфейс, или `http://127.0.0.1:8080/docs` — JSON API.

## Docker
```sh
docker compose up --build     # порт задаётся ODIN_PORT (по умолчанию 8080)
```
Проверка во внешней среде описана в `docs/DOCKER_CHECK.md`. Статус проверки: **BLOCKED** (DEC-06).

## Проверки
```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python -m odin.contracts.schema_export --check   # снимок JSON Schema (19 типов)
uv run python -m odin.openapi_snapshot --check          # снимок OpenAPI
```
Сеть в тестах заблокирована, кроме тестов с маркером `live` (план §6.1).

## Переменные окружения
Полный список — `.env.example`. Секреты хранятся только в `.env` (исключён из git).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ODIN_DATA_MODE` | `mock` | В S1 разрешён только `mock` (DEC-23) |
| `ODIN_LLM_MODE` | `off` | `off` / `replay`; `live` — после DEC-07 |
| `ODIN_DB_PATH` | `var/odin.sqlite3` | SQLite (WAL) |
| `ODIN_PORT` | `8080` | Порт HTTP |

## Структура
- `src/odin/contracts` — единый набор контрактов (ТЗ §7), экспорт JSON Schema в `schemas/`;
- `src/odin/core` — детерминированное ядро: деньги, условия, ранжирование, стыки, корзина, пересборка;
- `src/odin/tools` — Tool Registry, политика deny-by-default, Mock-адаптеры;
- `src/odin/llm` — LLM Gateway (`off`/`replay`), интерпретатор формы, Composer по таблице фактов;
- `src/odin/orchestrator.py` — автомат хода; `src/odin/storage.py` — SQLite; `src/odin/api.py` — JSON API;
- `src/odin/static` — интерфейс без сборки;
- `docs/` — план, журнал решений, отчёты срезов (`docs/reports/`).

## Ограничения S1
- Live-провайдеры не подключены: все карточки `approval_status=proposed` (DEC-09).
- Доступность Mock всегда `unknown`; итог с трансфером — `partial` (стоимость трансфера не подтверждена).
- Стыки «прибытие → заезд» — `unknown`, пока буферы не утверждены (DEC-20).
