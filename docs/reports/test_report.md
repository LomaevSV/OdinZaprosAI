# Отчёт о тестировании (AUDIT-01)

- **Версия:** `5bfc0a1` · **Дата:** 2026-10-24T19:16:51+00:00
- **Среда:** локально, Python 3.12.7, Windows; внешняя сеть заблокирована
- **Правило:** PASS — только при прохождении всех доказательств; Mock и Replay не засчитываются за Live (TEST-02).

## Уровни тестов (ACCEPT-01 п. 8)

Счёт по тестовым функциям; параметризованные варианты сведены строго (одно падение — failed).

| Уровень | passed | failed | skipped |
|---|---|---|---|
| unit | 96 | 0 | 0 |
| contract | 26 | 0 | 0 |
| adapter | 18 | 0 | 0 |
| e2e | 36 | 0 | 0 |
| delivery | 13 | 0 | 0 |

## Корпус K-01..K-30 по режимам

Итого: BLOCKED — 17, PASS — 30

| test_id | data_mode | status | requirement_id | expected | actual | evidence |
|---|---|---|---|---|---|---|
| K-01 | mock | PASS | UC-04, UC-03, DEC-28 | кандидаты из каталога с пометкой ДЕМО; без дат — «стоимость требует дат»; с датами — ДЕМО-оценка partial; нет визовых и климатических утверждений | 4/4 доказательств прошли (фраза на естественном языке требует LLM (DEC-07, LLM off): на Mock проверены ядро и структурированная форма) | `test_inspiration_without_dates_does_not_price_or_invent_dates`<br>`test_inspiration_with_origin_and_dates_estimates_top_candidates`<br>`test_no_climate_forecast_and_no_visa_claims`<br>`test_inspiration_without_destination_returns_three_marked_candidates` |
| K-01 | live | BLOCKED | UC-04, UC-03, DEC-28 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-02 | mock | PASS | UC-04, UC-10 | названное направление не ограничено каталогом; город вне справочника — честный ограниченный ответ | 2/2 доказательств прошли | `test_named_destination_is_not_limited_to_catalog`<br>`test_city_outside_demo_directory_is_not_invented` |
| K-02 | live | BLOCKED | UC-04, UC-10 | см. сценарий | геокодинг на Live запрещён до DEC-09, DEC-11, DEC-12 | — |
| K-03 | mock | PASS | UC-01, UC-05, UC-06, ACCEPT-01 | все условия формы стали условиями закрытого словаря; план с отелем, поездом и трансфером; неполнота видна | 3/3 доказательств прошли (фраза на естественном языке требует LLM (DEC-07, LLM off): на Mock проверены ядро и структурированная форма) | `test_structured_form_interpretation`<br>`test_message_plan_cart_confirm_receipt`<br>`test_one_more_adult_reprices_and_keeps_conditions` |
| K-03 | live | BLOCKED | UC-01, UC-05, UC-06, ACCEPT-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-04 | mock | PASS | INTERPRET-01, CONSTRAINT-01 | оператор отрицания (not_in) оценивается кодом; unknown не равен matched | 1/1 доказательств прошли (фраза на естественном языке требует LLM (DEC-07, LLM off): на Mock проверены ядро и структурированная форма) | `test_each_evaluator_status` |
| K-04 | live | BLOCKED | INTERPRET-01, CONSTRAINT-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-05 | mock | PASS | UC-02, UC-03 | ровно один вопрос blocking_now о городе отправления; город не предполагается | 2/2 доказательств прошли | `test_missing_origin_gives_one_blocking_question`<br>`test_three_blocking_classes_and_only_blocking_fields_are_asked` |
| K-05 | live | BLOCKED | UC-02, UC-03 | см. сценарий | Live-диалог требует LLM (DEC-07) и Live-провайдеров (DEC-09) | — |
| K-06 | mock | PASS | UC-02 | ответ дополняет прежние данные; повторных вопросов нет; обязательность не теряется | 2/2 доказательств прошли | `test_clarification_answer_is_merged_without_repeated_questions`<br>`test_answer_supplements_previous_data_without_repeating_questions` |
| K-07 | mock | PASS | CONSTRAINT-01, UC-02 | not_applicable только по явной схеме применимости; unknown не равен matched | 2/2 доказательств прошли | `test_unknown_is_not_not_applicable_and_not_matched`<br>`test_unknown_and_not_applicable_are_distinct` |
| K-08 | mock | PASS | INTERPRET-01, CONSTRAINT-01 | OR оценивается деревом условий, не смешивается с AND | 2/2 доказательств прошли (фраза на естественном языке требует LLM (DEC-07, LLM off): на Mock проверены ядро и структурированная форма) | `test_and_or_matrix`<br>`test_and_or_fallback_conflicts_in_nested_tree` |
| K-08 | live | BLOCKED | INTERPRET-01, CONSTRAINT-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-09 | mock | PASS | FALLBACK-01, INTERPRET-01 | FALLBACK активируется только при доказанном условии и завершённом поиске | 3/3 доказательств прошли (фраза на естественном языке требует LLM (DEC-07, LLM off): на Mock проверены ядро и структурированная форма) | `test_fallback_without_proven_activation_keeps_primary_branch`<br>`test_fallback_is_not_activated_when_primary_matched_or_search_incomplete`<br>`test_and_or_fallback_conflicts_in_nested_tree` |
| K-09 | live | BLOCKED | FALLBACK-01, INTERPRET-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-10 | mock | PASS | INTERPRET-01, COVERAGE-01 | вложенное дерево оценивается; unparsed сохраняется и даёт unknown; циклы и висячие ссылки — unknown | 3/3 доказательств прошли | `test_and_or_fallback_conflicts_in_nested_tree`<br>`test_tree_cycle_and_dangling_reference_are_unknown`<br>`test_unparsed_group_is_kept_as_unknown` |
| K-11 | mock | PASS | INTERPRET-02 | даты разрешены по правилу с записью статуса; по местной дате пояса | 3/3 доказательств прошли (фраза на естественном языке требует LLM (DEC-07, LLM off): на Mock проверены ядро и структурированная форма) | `test_calendar_resolves_with_rule`<br>`test_calendar_uses_local_date_of_timezone`<br>`test_next_weekend_on_friday_is_resolved` |
| K-11 | live | BLOCKED | INTERPRET-02 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-12 | mock | PASS | HOTEL-01, PRICE-01 | возраст ребёнка передан во все поиски и размещение | 1/1 доказательств прошли | `test_child_six_years_is_carried_to_every_search` |
| K-12 | live | BLOCKED | HOTEL-01, PRICE-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-13 | mock | PASS | INTERPRET-02, UC-10 | даты не выдумываются; неоднозначность → вопрос | 2/2 доказательств прошли | `test_calendar_does_not_invent_dates`<br>`test_unexpected_request_gives_limited_answer_or_question` |
| K-14 | mock | PASS | HOTEL-01, PRICE-01 | совместная доступность комплекта; цена = сумма номеров × ночи (27 000 ₽) | 3/3 доказательств прошли | `test_two_rooms_5000_and_4000_for_three_nights_is_27000`<br>`test_one_room_unavailable_makes_set_unavailable`<br>`test_independent_but_not_joint_availability_is_unknown` |
| K-14 | live | BLOCKED | HOTEL-01, PRICE-01 | см. сценарий | Live запрещён (DEC-09); по плану на Live ожидается unknown (V4) | — |
| K-15 | mock | PASS | UC-03, DEC-25 | DEC-25 открыт: допущение не делается, задаётся вопрос; допущения (если разрешены) — рядом с суммой | 3/3 доказательств прошли | `test_no_assumptions_are_made_and_party_is_asked`<br>`test_assumptions_are_shown_next_to_the_sum`<br>`test_missing_party_size_asks_blocking_question` |
| K-16 | mock | PASS | CONNECTION-01, DEC-20 | раннее прибытие — не нарушение; неутверждённый буфер даёт unknown | 1/1 доказательств прошли | `test_early_arrival_is_not_a_violation_and_unapproved_buffer_gives_unknown` |
| K-16 | live | BLOCKED | CONNECTION-01, DEC-20 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-17 | mock | PASS | PRICE-01, DEC-18 | итог partial; известная часть показана | 2/2 доказательств прошли | `test_transfer_with_unknown_cost_makes_total_partial`<br>`test_unknown_component_makes_total_partial_known_lower_bound` |
| K-17 | live | BLOCKED | PRICE-01, DEC-18 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-18 | mock | PASS | CONNECTION-01, CONNECTION-02 | доказанный конфликт — violated; DST и пояса учтены | 3/3 доказательств прошли | `test_arrival_after_check_in_day_is_violated`<br>`test_missing_times_give_unknown_and_return_on_other_day_is_violated`<br>`test_dst_transition_and_cross_zone_connection` |
| K-19 | mock | PASS | AVAIL-01, RANK-01 | unknown не превращается в available; неизвестный отель остаётся кандидатом | 2/2 доказательств прошли | `test_availability_semantics_is_strict`<br>`test_unknown_hotel_stays_candidate_and_unavailable_is_rejected` |
| K-19 | live | BLOCKED | AVAIL-01, RANK-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-20 | mock | PASS | RANK-01, ACCEPT-03 | корректная работа при 0, 1 и многих вариантах | 1/1 доказательств прошли | `test_zero_one_many_prices` |
| K-21 | mock | PASS | RANK-01 | классы без весов; стабильный порядок по каноническому ID | 1/1 доказательств прошли | `test_rank_classes_and_stable_order_without_weights` |
| K-22 | mock | PASS | UC-09, REPLAN-01, REPLAN-02 | условия сохранены; зависимые компоненты перепроверены; старая корзина — 409; новая переоценена | 2/2 доказательств прошли | `test_replan_dates_preserves_conditions_and_reprices_cart`<br>`test_failed_replan_keeps_last_agreed_revision` |
| K-22 | live | BLOCKED | UC-09, REPLAN-01, REPLAN-02 | см. сценарий | T-E2E-LIVE-02: Live запрещён до DEC-04, DEC-09, DEC-11 | — |
| K-23 | mock | PASS | UC-09, REPLAN-01 | без нового поиска; явный выбор сохранён; бюджет переоценён; нарушение видно | 1/1 доказательств прошли | `test_budget_reduction_reevaluates_without_new_search` |
| K-23 | live | BLOCKED | UC-09, REPLAN-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-24 | mock | PASS | UC-09, REPLAN-01 | правила отеля и транспорта перепроверены; трансфер инвалидирован; прочие условия сохранены | 1/1 доказательств прошли | `test_dog_rechecks_hotel_and_transport_rules` |
| K-24 | live | BLOCKED | UC-09, REPLAN-01 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
| K-25 | mock | PASS | UC-09, REPLAN-01 | цена пересчитана; условия сохранены; всё зависимое перепроверено | 2/2 доказательств прошли | `test_one_more_adult_reprices_and_keeps_conditions`<br>`test_cart_is_repriced_after_replan` |
| K-26 | mock | PASS | UC-08, ARCH-05 | отказ ничего не оформляет; два раздельных согласия; повтор не дублирует чек; checkout не вызывается | 5/5 доказательств прошли | `test_decline_creates_nothing`<br>`test_confirmation_requires_both_consents`<br>`test_double_confirmation_returns_same_receipt`<br>`test_external_checkout_is_never_called`<br>`test_stale_cart_requires_recheck_or_separate_consent` |
| K-27 | mock | PASS | SCOPE-01, SEARCH-STOP-01, UC-10 | частичный план без отеля, неполнота видна; остановка не доказывает отсутствие вариантов | 3/3 доказательств прошли | `test_unexpected_request_gives_limited_answer_or_question`<br>`test_timeout_never_proves_empty_search`<br>`test_call_limit_gives_limits_exhausted_with_unchecked_sources` |
| K-28 | replay | PASS | DATA-MODE-03, ACCEPT-01 | Replay используется только при совпадении значимых параметров | 2/2 доказательств прошли | `test_live_to_replay_only_with_matching_parameters`<br>`test_replay_availability_is_unknown` |
| K-29 | mock | PASS | DATA-MODE-01, UI-01 | data_mode=mock у каждого оффера, компонента и позиции корзины; ДЕМО в ответе | 2/2 доказательств прошли | `test_mock_is_visible_in_every_card`<br>`test_mock_offers_are_marked_demo_and_never_available` |
| K-30 | mock | PASS | UC-10 | ограниченный полезный ответ или вопрос; без галлюцинаций и ложного успеха | 1/1 доказательств прошли | `test_unexpected_request_gives_limited_answer_or_question` |
| K-30 | live | BLOCKED | UC-10 | см. сценарий | Live запрещён до решений DEC-04 (MCP-клиент), DEC-09 (провайдеры), DEC-11 (проверки среды) | — |
