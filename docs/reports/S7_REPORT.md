# Отчёт: Live-срез и подготовка к сдаче (25.10.2026)

**Основание:** задание руководителя 25.10 (LLM, Live-адаптеры, правила организаторов, порт 8080, Docker) и
решения DEC-04, DEC-09 (частично), DEC-11, DEC-13, DEC-23, DEC-32 (частично) — `docs/decision_log.md`.

## Сделано
| Блок | Что | Где |
|---|---|---|
| LLM | OpenAI-совместимый клиент Chat Completions: allowlist хоста, HTTPS (http — только локальная модель), повторы 429/5xx/тайм-аут, совместимый повтор при 400, без журналирования текстов и ключа | `src/odin/llm/openai_client.py` |
| LLM | Gateway `live`: промпт роли → JSON → Pydantic, один повтор по формату, бюджет вызовов за ход, журнал вызовов без текстов (`audit: llm_call`), запись ответов для Replay (`ODIN_LLM_RECORD`) | `src/odin/llm/gateway.py`, `prompts.py` |
| LLM | Интерпретатор свободного текста: значения только с дословными цитатами (иначе отклоняются), даты считает календарь (диапазоны «с … по …», «N ночей»; «N дней» → вопрос), слияние с прежними условиями, `clear` только с цитатой, неразобранное — `unparsed` | `src/odin/llm/free_text.py`, `core/calendar.py` |
| LLM | Composer: переформулировка по таблице фактов; отказ при новых числах/обещаниях («забронировано», «гарантирую»), потере вопросов; пометка режима не теряется | `src/odin/llm/composer.py`, оркестратор `_answer` |
| Конфигурация | `.env` читается при локальном запуске; запасные имена `OPENAI_*`; режим LLM включает только оператор | `src/odin/config.py` |
| MCP | Собственный клиент Streamable HTTP (JSON и SSE, без сессии) | `src/odin/tools/mcp_client.py` |
| Live | Tutu: `search_rail` (за место → «от» за группу), `search_avia` (за группу), `search_hotels` (`stay_total`); закрепление схем, политика до сети, запрет оформления | `src/odin/tools/tutu.py` |
| Live | Open-Meteo геокодинг (часовой пояс, координаты), OSRM матрица времени трансфера; редакционная таблица вокзалов/аэропортов | `rest_adapters.py`, `places.py` |
| Режимы | `mock` / `mixed` / `live` / `replay`: Live → строгий Replay → (mixed) ДЕМО по компоненту → честный `unknown`; запись Replay при успешном Live | `live_handlers.py`, `live_common.py` |
| Оркестратор | Геокодинг мест, трансфер «выбранная точка ↔ выбранный отель», режим и пометки плана по факту компонентов | `src/odin/orchestrator.py` |
| Поставка | Multi-stage `Dockerfile` (не root, без uv в рантайме), `docker-compose.yml` (`8080:8080`, healthcheck, `env_file`), README по шаблону, `.env.example`, Makefile, CI | корень, `docs/DOCKER_CHECK.md` |

## Проба провайдеров (DEC-11)
Tutu: `initialize` 0,38 с, `tools/list` 17 инструментов, `search_rail` 2,2 с, `search_avia` 0,8 с,
`search_hotels` 1,8 с, `get_offer_details` 5,1 с; Open-Meteo геокодинг 0,4 с; OSRM маршрут 0,4 с. Итого 8 запросов.
Сырые ответы в git не попадали; фикстуры `tests/live_fixtures.py` — синтетические.

## Результаты
- `pytest`: **555 passed, 2 skipped** (явные Live-тесты); `ruff check` и `ruff format --check` — чисто;
  снимки JSON Schema и OpenAPI актуальны.
- Локальный запуск `python -m odin.api` (mock): `/health` 200, `/` 200, сценарий план → корзина → подтверждение → чек — OK.

## Не выполнено / ограничения
| Пункт | Статус | Причина |
|---|---|---|
| Реальный прогон LLM | NOT_RUN | ключ у руководителя (DEC-07); тест `tests/live/test_llm_live_smoke.py` |
| Сквозной Live-прогон Tutu/Open-Meteo/OSRM | PASS (см. дополнение) | выполнен по разрешению руководителя, 5 запросов данных |
| Docker в отдельной среде | BLOCKED | DEC-06; инструкция `docs/DOCKER_CHECK.md` |
| Детали отеля (`get_offer_details`) → `available` | реализовано (волна 2, лучшие K = 3), проверено на фикстурах | Live-прогоном не проверено: для 6 запросов — `ODIN_TOP_K=1` в `tests/live/test_tutu_live_smoke.py` |
| 2ГИС, климатические нормы, OurAirports | не подключены | DEC-13 (нет ключа), DEC-12 |
| Видео, тег `submission` | вне разработки | правила организаторов |

## Отклонения
- Тест `test_cart_is_repriced_after_replan` зависел от даты (порядок по каноническому ID при равных классах
  RANK-01 выбирал разный вид транспорта): в тесте зафиксирован вид транспорта. Логика не менялась.
- В проверке секретов добавлено исключение для UUID в публичной ссылке на правила организаторов.

## Дополнение: решения руководителя 25.10 (второе сообщение)
| Решение | Реализация | Проверка |
|---|---|---|
| Без ключа LLM — резервный `mock`; при настроенной LLM — `live`; незаметная подмена Live на Mock запрещена | `ODIN_DATA_MODE=auto` (по умолчанию); в `live` демо-справочник мест и поясов не используется; сбои Live и замены на ДЕМО (только явный `mixed`) называются в ответе | `test_config_defaults_and_restrictions`, `test_mixed_falls_back_to_marked_mock_and_live_stays_honest` |
| Передача текстов LLM только при `ODIN_LLM_MODE=live`, без секретов, чужих данных и истории, без логов, с уведомлением | `src/odin/llm/privacy.py`: ключи, e-mail, длинные номера → `[скрыто]` до отправки; только текущее сообщение и краткие условия поездки; `privacy_notice` в сессии и UI, пометка в ответе; в состоянии — очищенный текст | `test_redaction_hides_secrets_and_personal_numbers_but_keeps_trip_facts`, `test_llm_receives_only_redacted_current_message_and_user_is_notified` |
| Видео — TODO как обязательный блокер сдачи | README, `docs/release_manifest.yaml` (`submission_blockers`), `contest_mapping.md` | `test_submission_blockers_are_tracked_until_resolved` |
| До 6 бесплатных запросов, только чтение | `tests/live/test_tutu_live_smoke.py` с транспортом-ограничителем (лишний запрос не уходит в сеть) | выполнено, см. ниже |

## Live-прогон T-E2E-LIVE-01 (25.10.2026)
- Запросы данных: **5 из 6 разрешённых** — геокодинг Open-Meteo ×2, Tutu `search_rail` ×1, Tutu `search_hotels` ×1,
  OSRM ×1; служебные сообщения MCP: `initialize`, `notifications/initialized`, `tools/list`. Бронирований,
  оплаты и передачи секретов не было; ответы провайдеров в репозиторий не сохранялись.
- Итог: **PASS** за 5,3 с. План целиком Live: поезд Москва → Казань, отель, трансфер (оценка OSRM), 9 вариантов
  транспорта и 10 гостиниц; итог `partial` (наличие и сборы не подтверждены, стоимость трансфера не оценивается);
  корзина создаётся (201). Остановка — `sources_exhausted`, непроверенных источников нет.

## Итог тестов после дополнения
`pytest`: **558 passed, 2 skipped** (явные Live-тесты; Live-тест Tutu выполнен отдельно — PASS); Ruff — чисто;
снимок OpenAPI обновлён (`privacy_notice` в ответе сессии).