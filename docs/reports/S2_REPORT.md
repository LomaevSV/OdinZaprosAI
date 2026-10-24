# Отчёт по срезу S2 — «Первый Live-срез» (частично; основная часть BLOCKED)

**Дата:** 24.10.2026 · **План:** §4 S2 · **Режим проверок:** синтетические фикстуры (`httpx.MockTransport`), сеть заблокирована.

## Статус: BLOCKED (решения руководителя), выполнена независимая часть
По плану (§4 S2, «Если провайдер не утверждён…») Live-функции получают статус `blocked`, путь на Mock
сохраняется, работа продолжается по S3. В `/health` у каждого провайдера `live_enabled=false` с причиной.

| Компонент S2 | Статус | Причина / где |
|---|---|---|
| MCP-клиент (Streamable HTTP, `initialize`, `tools/list`) | BLOCKED | DEC-04 открыт (нужен spike SDK vs свой клиент; нельзя объявлять выбор самостоятельно) |
| Адаптер Tutu (`search_rail/avia/hotels`, `get_offer_details`) | BLOCKED | DEC-09 (провайдер `proposed`), DEC-10, DEC-15 |
| Геокодинг Open-Meteo/Nominatim/Photon, OSRM, OurAirports | BLOCKED | DEC-09, DEC-12 (загрузка OurAirports), DEC-33 |
| Live-интерпретатор, Composer на LLM | BLOCKED | DEC-07 (нет ключа и решения о передаче текстов) |
| Минимальные пробы провайдеров в среде запуска | BLOCKED | DEC-11 |
| T-E2E-LIVE-01, T-INT-LIVE-01..05 | BLOCKED | зависят от всех пунктов выше |
| Устойчивость HTTP: тайм-ауты, 1 повтор, `Retry-After` для 429, 5xx, ошибки авторизации без повтора, проверка content-type/JSON, allowlist хостов до сети | **Готово** | `src/odin/tools/http_client.py` |
| Replay: строгий ключ, локальная запись только успешных Live-ответов, переход Live → Replay с меткой «цена на дату, не текущая» | **Готово** | `src/odin/tools/replay.py`, `src/odin/tools/router.py` |
| Закрепление хешей схем, закрытие инструмента при изменении (`schema_changed`) | **Готово** | `src/odin/tools/untrusted.py` |
| Недоверенные данные: URL только из доменов провайдера, телефоны по формату, тексты как данные | **Готово** | там же |
| Composer v1 (слоты, проверка фактов, шаблон) | Готово в S1 | `src/odin/llm/composer.py` |

## Результаты тестов
`pytest`: **419 passed** (из них новых 21 в `tests/adapter/test_resilience.py`). `ruff check`/`format --check` — чисто.

| Тест | Статус |
|---|---|
| T-NEG-TOOL-01..13 (401, 403, 429, 5xx, тайм-аут, пусто, невалидный JSON, смена схемы, кириллица, повтор, дедлайн, allowlist/отмена до сети, параллелизм) | PASS (фикстуры) |
| T-NEG-SEC-01 (`register_checkout_passengers` до сети) | PASS |
| T-NEG-INJ-01 | PASS |
| T-NEG-REPLAY-02, T-UNIT-REPLAY-01, T-NEG-LIVEFAIL-01 | PASS |
| T-NEG-SCHEMA-01 | PASS |
| T-ADP-TUTU-01..04 | NOT_RUN — нет адаптера Tutu (DEC-09/DEC-04) |
| T-E2E-LIVE-01, T-INT-LIVE-01..05 | BLOCKED |

Критерий «0 вызовов вне allowlist»: в S1–S2 внешних вызовов нет вовсе (сеть заблокирована в тестах, Live выключен конфигурацией).

## Что нужно от руководителя для завершения S2
DEC-04 (разрешить spike MCP), DEC-07 (LLM: провайдер, модель, ключ, лимиты), DEC-09 (утверждение P0-провайдеров),
DEC-10, DEC-11 (пробы в среде запуска), DEC-12, DEC-15, DEC-22, DEC-23.
