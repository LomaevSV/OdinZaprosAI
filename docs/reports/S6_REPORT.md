# Отчёт по срезу S6 — «Корпус, метрики, поставка»

**Дата:** 24.10.2026 · **План:** §4 S6 · **Режим:** Mock (+ Replay на уровне маршрутизатора); Live недоступен.

## Сделано
| Компонент | Где |
|---|---|
| Корпус K-01..K-30 по режимам с доказательствами (pytest node id) и предустановленными BLOCKED с причиной | `tests/corpus/test_corpus.yaml` |
| Прогонщик: один прогон pytest с JUnit → статусы AUDIT-01 (PASS только при прохождении всех доказательств) | `tests/corpus/run_corpus.py` |
| Метрики METRIC-01 на сценариях Mock через JSON API; пороги не установлены (OPEN-05); Live/Replay/mixed — BLOCKED | `docs/reports/metrics.json` |
| Отчёт AUDIT-01: уровни тестов и записи корпуса (requirement_id/test_id/expected/actual/evidence/status/version/environment/data_mode/tested_at) | `docs/reports/test_report.md` |
| Проверки целостности корпуса: K-01..K-30, существование доказательств, Live без PASS | `tests/delivery/test_corpus.py` |
| Новые сценарные тесты: K-05 (сквозной), K-12 (ребёнок 6 лет), K-29 (Mock в каждой карточке) | `tests/e2e/test_corpus_extra.py` |
| README на русском (запуск, инициализация БД, режимы, провайдеры, демо-сценарий, ограничения, тесты, лицензии) | `README.md` |
| `release_manifest`, обновлённые `data_manifest` и `contest_mapping.md` | `docs/` |

## Результаты
- `pytest`: **502 passed**; `ruff check` / `ruff format --check` — чисто; снимки схем (19) и OpenAPI актуальны.
- Корпус (версия `5bfc0a1`): **PASS — 30, BLOCKED — 17, FAIL — 0, NOT_RUN — 0**. PASS — Mock (29) и Replay (K-28); все 17 BLOCKED — Live.
- Метрики Mock: e2e completion 1.0 (n=3), раунды уточнения 0.7, пересборка без потери условий 1.0 (n=4),
  факты с источником 1.0, ошибки инструментов 0.0, LLM-вызовы 0. Это измерения Mock, не обещания для Live.

## Не выполнено (BLOCKED)
| Пункт | Причина |
|---|---|
| Чистый запуск в Docker во внешней среде (ACCEPT-01 п. 1) | DEC-06: внешней среды нет |
| Прогон корпуса на Live и смешанном режиме; Live/Replay-метрики | DEC-04, DEC-09, DEC-11; снимков Replay нет (DEC-24) |
| Свободный диалог K-05..K-07 на LLM (ACCEPT-01 п. 3 в полном объёме) | DEC-07 (LLM off) |
| LEGAL-01, юридическая часть поставки | DEC-33 |
| Формат сдачи | DEC-32 |

## Ограничение трактовки
Сценарии со свободной фразой (K-01, K-03, K-04, K-08, K-09, K-11) на Mock доказаны на уровне ядра и формы;
в отчёте это указано в столбце `actual`. Разбор фраз LLM не проверялся.

## Тег
Локальный тег `s6-2026-10-24` (DEC-05). Push не выполнялся.
