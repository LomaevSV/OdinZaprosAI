/* О!дин запрос — минимальный интерфейс S1 (UI-01). Чистый JS без сборки. */
"use strict";

const API = "/api/v1";
const state = { session: null, trip: null, cart: null, revision: null, busy: false };

const $ = (id) => document.getElementById(id);
const text = (value) => document.createTextNode(value == null ? "" : String(value));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("data-") || key.startsWith("aria-") || key === "role") node.setAttribute(key, value);
    else node[key] = value;
  }
  for (const child of children) node.append(child instanceof Node ? child : text(child));
  return node;
}

function requestId() {
  return "req-" + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "").slice(0, 20) : Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
}

// Язык интерфейса — русский: сетевые и серверные сбои не показываются английскими текстами браузера.
async function call(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    throw new Error("нет связи с сервером, повторите попытку");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload.message || `сервер вернул ошибку (код ${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return payload;
}

// UI-008: единая лента диалога, как в обычном чате: каждое сообщение — один раз, по порядку; варианты выбора
// (города, даты) — внутри сообщения, к которому относятся. Своей прокрутки у ленты нет: прокручивается страница.
// UI-012: к новому сообщению лента подводится только для действий из чата (текст, кнопки в сообщениях);
// выбор в панели поездки (предложения, направления, корзина) не уводит страницу к чату.
function say(role, message, follow = false) {
  const node = el("div", { class: `msg ${role}`, "data-testid": `msg-${role}` }, message);
  $("messages").append(node);
  if (role === "user") markDialogStarted();
  if (follow && node.scrollIntoView) node.scrollIntoView({ block: "nearest" });
  return node;
}

// UI-009: длинный пример — только до первого сообщения новой поездки; введённый текст не трогается.
const PLACEHOLDER_SHORT = "Напишите сообщение…";

function markDialogStarted() {
  $("text-input").placeholder = PLACEHOLDER_SHORT;
}

function busyNotice(follow = false) {
  say("assistant", "Подождите: предыдущий запрос ещё обрабатывается, затем повторите действие.", follow);
}

// Конфликт ревизии (409) не «молчит»: понятное сообщение, обновлённая поездка и кнопка «Повторить».
function sayRetry(message, retry, follow = false) {
  const node = say("assistant", message, follow);
  if (!retry) return node;
  const button = el("button", { type: "button", class: "option retry", "data-testid": "retry-button" }, "Повторить");
  button.addEventListener("click", () => {
    button.disabled = true;
    retry();
  });
  node.append(el("div", { class: "question-options" }, button));
  return node;
}

async function refreshTrip() {
  if (!state.trip) return;
  try {
    renderTrip(await call("GET", `${API}/trips/${state.trip.trip_id}`));
  } catch (error) {
    // Обновить не удалось: сообщение об ошибке уже показано, повтор отправит актуальную ревизию сервера.
  }
}

const LLM_MODE_LABELS = { live: "подключён", off: "отключён", replay: "Replay (записанные ответы)" };
const MODE_LABELS = {
  mock: "ДЕМО (Mock)",
  replay: "Replay: данные на дату",
  live: "Live",
  mixed: "Смешанный: Live + ДЕМО",
};

function modeBadge(mode) {
  return el("span", { class: `badge badge-${mode}`, "data-testid": "mode-badge" }, MODE_LABELS[mode] || mode);
}

function money(price) {
  if (!price || price.amount == null) return "сумма неизвестна";
  const [integer, fraction] = price.amount.split(".");
  const grouped = Number(integer).toLocaleString("ru-RU");
  const suffix = price.currency === "RUB" ? "₽" : price.currency || "";
  return `${grouped}${fraction && fraction !== "00" ? "," + fraction : ""} ${suffix}`.trim();
}

const COMPLETENESS = { complete: "полная", partial: "неполная (известна часть)", unknown: "неизвестна" };
const STATUS = { matched: "выполнено", violated: "нарушено", unknown: "не подтверждено", not_applicable: "не применимо" };
const AVAILABILITY = { available: "есть по данным поставщика на момент запроса", unavailable: "нет по данным поставщика", unknown: "не подтверждено" };
const EVIDENCE = { search_listing: "поисковая выдача", offer_details: "детали предложения", provider_checked: "проверено у поставщика", historical: "исторические данные", mock: "ДЕМО", none: "нет данных" };
const STOP_REASONS = {
  sufficient_results: "найдено достаточно вариантов",
  sources_exhausted: "опрошены все подключённые источники",
  limits_exhausted: "исчерпан лимит запросов на ход",
  deadline: "истекло время на ход",
  provider_unavailable: "источник данных недоступен",
  user_stopped: "остановлен пользователем",
  blocked_by_decision: "источник не разрешён к использованию",
};
const FIELD_LABELS = { budget_total: "бюджет", transport_mode: "вид транспорта", pets: "животные", hotel_breakfast: "завтрак", hotel_stars_min: "звёздность", hotel_free_cancellation: "бесплатная отмена", hotel_room_type: "семейный номер (пожелание)" };
const ROLE_LABELS = { outbound: "туда", return: "обратно", hotel: "гостиница", transfer_in: "трансфер к гостинице", transfer_out: "трансфер от гостиницы" };
const COLUMN_TITLES = { outbound: "Транспорт туда", hotel: "Гостиницы", return: "Транспорт обратно" };

// UI-007: заметный заголовок секции с числом вариантов; связан с секцией для экранных чтецов.
function sectionTitle(id, title, count) {
  return el("h4", { class: "column-title", id },
    title, ...(count == null ? [] : [" ", el("span", { class: "count" }, `(${count})`)]));
}

function availabilityText(availability) {
  const label = AVAILABILITY[availability.availability_status] || availability.availability_status;
  return `Наличие: ${label} (${EVIDENCE[availability.evidence_level] || availability.evidence_level})`;
}

// UI-004/UI-005: для ДЕМО-данных непроверенное наличие уже сказано общим уведомлением и бейджем «ДЕМО» на карточке;
// для Live/Replay строка наличия остаётся на каждой карточке без сокращений.
function availabilityLines(availability) {
  const demoUnknown = availability.data_mode === "mock" && availability.availability_status === "unknown";
  return demoUnknown ? [] : [availabilityText(availability)];
}

const MOCK_AVAILABILITY_UNKNOWN = /^наличие \(.+\) не подтверждено источником$/;

function visibleUnknowns(plan) {
  if (plan.trip_data_mode !== "mock") return plan.unknowns;
  return plan.unknowns.filter((item) => !MOCK_AVAILABILITY_UNKNOWN.test(item));
}

function asOf(source) {
  const moment = source && (source.retrieved_at || source.generated_at);
  return moment ? new Date(moment).toLocaleString("ru-RU") : "—";
}

function card(title, subtitle, price, source, mode, extra = []) {
  return el(
    "div",
    { class: "card", "data-testid": "offer-card" },
    el("div", {}, el("strong", {}, title), " ", modeBadge(mode)),
    el("div", {}, subtitle),
    el("div", {}, `Цена: ${money(price)} — ${COMPLETENESS[price.total_completeness] || price.total_completeness}`),
    el("div", { class: "meta" }, `Источник: ${source.provider}${source.tool ? " / " + source.tool : ""}; актуально на ${asOf(source)}`),
    ...extra.map((line) => el("div", { class: "meta" }, line)),
  );
}

// TRF-SELECT-01: маршруты «Как добраться» — трансферы, вошедшие в план (пару «выбранная точка ↔ выбранная
// гостиница» собирает сервер). Порядок: к гостинице, затем обратно. Это справка, а не бронируемое предложение.
const ROUTE_ORDER = { transfer_in: 0, transfer_out: 1 };

function planRoutes(trip) {
  const ids = new Set(((trip.plan && trip.plan.components) || []).filter((c) => c.kind === "transfer").map((c) => c.offer_id));
  const rank = (offer) => (offer.direction in ROUTE_ORDER ? ROUTE_ORDER[offer.direction] : 2);
  return (trip.transfer_options || []).filter((offer) => ids.has(offer.offer_id)).sort((a, b) => rank(a) - rank(b));
}

function routeTitle(title) {
  return String(title).replace(/^Трансфер на автомобиле:\s*/, "Маршрут на автомобиле: ");
}

function routeCard(offer) {
  const direction = { transfer_in: "К гостинице", transfer_out: "От гостиницы" }[offer.direction] || "Маршрут";
  const km = offer.terms && offer.terms.distance_km != null ? `, ≈ ${offer.terms.distance_km} км` : "";
  const time = offer.duration_minutes != null ? `≈ ${offer.duration_minutes} мин${km} (оценка без пробок)` : "время не оценено";
  const cost = offer.price && offer.price.amount != null ? money(offer.price) : "неизвестна";
  return el("div", { class: "card route", "data-testid": "route-card", "data-route-id": offer.offer_id, "data-direction": offer.direction },
    el("div", {}, el("strong", {}, routeTitle(offer.title)), " ", modeBadge(offer.source.data_mode)),
    el("div", {}, `${direction}: ${time}`),
    el("div", {}, `Стоимость: ${cost}; не бронируется`),
    el("div", { class: "meta" }, `Источник: ${offer.source.provider}${offer.source.tool ? " / " + offer.source.tool : ""}; актуально на ${asOf(offer.source)}`));
}

const DATES_PERIOD_PENDING_ID = "amb-llm-dates-period";

function dayText(iso) {
  const [year, month, day] = String(iso).split("-");
  return `${day}.${month}.${year}`;
}

// DATE-004/005: понятая, но ещё не завершённая дата не выглядит как «даты не заданы».
function datesText(request) {
  if (request.date_range) return `${request.date_range.start} — ${request.date_range.end}`;
  const ambiguities = request.ambiguities || [];
  const expressions = request.date_expressions || [];
  const departure = expressions.filter((e) => e.status === "resolved" && e.resolved_start && !e.resolved_end).slice(-1)[0];
  if (departure) return `отъезд ${dayText(departure.resolved_start)} («${departure.raw_text}»), возвращение уточняется`;
  const period = ambiguities.find((a) => a.ambiguity_id === DATES_PERIOD_PENDING_ID);
  if (period) {
    const phrase = (expressions.filter((e) => e.status === "ambiguous").slice(-1)[0] || {}).raw_text;
    const label = phrase ? `период «${phrase}»` : "период поездки";
    return (period.options || []).length ? `${label} — выберите вариант дат в чате` : `${label} — уточняется длительность`;
  }
  return "даты не заданы";
}

// CITY-002: «Изменить направление» — небольшая форма рядом с поездкой и список предложенных городов.
function openDestinationChange() {
  const form = $("destination-form");
  form.hidden = false;
  const section = $("destination-section-anchor");
  const hasCandidates = Boolean(state.trip && (state.trip.candidate_destinations || []).length);
  $("destination-hint").textContent = hasCandidates
    ? "Введите город или выберите из предложенных направлений ниже и в чате. Прочие условия поездки сохранятся."
    : "Введите город. Прочие условия поездки (откуда, даты, состав, бюджет) сохранятся.";
  if (hasCandidates && section && section.scrollIntoView) section.scrollIntoView({ block: "nearest" });
  if ($("destination-input").focus) $("destination-input").focus();
}

function closeDestinationChange() {
  $("destination-form").hidden = true;
}

$("destination-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = $("destination-input").value.trim();
  if (!name) {
    $("destination-hint").textContent = "Укажите город назначения.";
    say("assistant", "Укажите город назначения.");
    return;
  }
  chooseDestination({ place: { name } }, { anchor: () => $("destinations") }).then((turn) => {
    if (turn) {
      $("destination-input").value = "";
      closeDestinationChange();
    }
  });
});
$("destination-cancel").addEventListener("click", closeDestinationChange);

function renderTrip(trip) {
  state.trip = trip;
  state.revision = trip.revision;
  $("revision").textContent = `ревизия ${trip.revision}`;
  const summary = $("trip-summary");
  summary.replaceChildren();
  const request = trip.request;
  if (request) {
    const party = request.party.adults ? `${request.party.adults} взр.` + (request.party.children_ages.length ? `, дети: ${request.party.children_ages.join(", ")}` : "") : "состав не указан";
    const route = el("p", { "data-testid": "trip-route" },
      `${request.origin ? request.origin.name : "?"} → ${request.destination ? request.destination.name : "направление не задано"}`);
    // CITY-002: направление меняется на любом этапе — кнопка рядом с текущим городом (ход без LLM).
    const change = el("button", { type: "button", class: "link-button", "data-testid": "change-destination", "aria-controls": "destination-form" },
      request.destination ? "Изменить направление" : "Выбрать направление");
    change.addEventListener("click", openDestinationChange);
    route.append(" ", change);
    summary.append(route, el("p", { "data-testid": "trip-dates" }, `Даты: ${datesText(request)}. Гости: ${party.replace(/\.$/, "")}.`));
  }
  const plan = trip.plan;
  if (plan) {
    summary.append(
      el("p", { "data-testid": "plan-total" }, `Итог: ${money(plan.total_price)} — стоимость ${COMPLETENESS[plan.completeness]}`),
      el("p", { class: "meta" }, `Режим поездки: ${MODE_LABELS[plan.trip_data_mode] || plan.trip_data_mode}`),
    );
    if (visibleUnknowns(plan).length) summary.append(el("p", { class: "meta" }, "Не подтверждено: " + visibleUnknowns(plan).join("; ")));
    for (const check of plan.connection_checks) {
      summary.append(el("p", { class: `status-${check.status}`, "data-testid": "connection-check" },
        `Стык ${ROLE_LABELS[check.from_component] || check.from_component} → ${ROLE_LABELS[check.to_component] || check.to_component}: ${STATUS[check.status]} (${check.basis})`));
    }
  }
  if (trip.search_termination) {
    summary.append(el("p", { class: "meta", "data-testid": "stop-reason" },
      `Поиск: ${STOP_REASONS[trip.search_termination.reason] || trip.search_termination.reason}. Это не означает, что вариантов нет.`));
  }

  const cards = $("cards");
  cards.replaceChildren();
  offerNodes.clear();
  destinationNodes.clear();
  const hotels = Object.fromEntries((trip.hotels || []).map((hotel) => [hotel.hotel_id, hotel]));
  const chosen = new Set((plan ? plan.components : []).map((component) => component.offer_id));
  // UI-002: три колонки «туда | гостиница | обратно»; пустая колонка — честное пустое состояние.
  const columns = { outbound: [], hotel: [], return: [] };
  for (const offer of trip.transport_offers || []) {
    const node = card(offer.title, `${offer.direction === "outbound" ? "Туда" : "Обратно"}: ${new Date(offer.departure_at).toLocaleString("ru-RU")} → ${new Date(offer.arrival_at).toLocaleString("ru-RU")}`, offer.price, offer.source, offer.source.data_mode, availabilityLines(offer.availability));
    makeSelectable(node, offer.direction, offer.offer_id, offer.title, chosen.has(offer.offer_id));
    (columns[offer.direction] || columns.outbound).push(node);
  }
  for (const offer of trip.hotel_offers || []) {
    const hotel = hotels[offer.hotel_id] || {};
    const stars = hotel.star_rating == null ? "категория не указана" : `${hotel.star_rating}★`;
    const node = card(hotel.name || offer.hotel_id, `${stars}; завтрак: ${offer.terms.breakfast_included == null ? "не подтверждён" : offer.terms.breakfast_included ? "да" : "нет"}`, offer.price_total, offer.source, offer.source.data_mode, availabilityLines(offer.availability));
    makeSelectable(node, "hotel", offer.offer_id, hotel.name || offer.hotel_id, chosen.has(offer.offer_id));
    columns.hotel.push(node);
  }
  const hasOffers = Object.values(columns).some((items) => items.length);
  if (hasOffers) {
    const grid = el("div", { class: "offer-columns", "data-testid": "offer-columns" });
    for (const role of ["outbound", "hotel", "return"]) {
      const empty = role === "return" && trip.request && trip.request.return_trip === false
        ? "обратный путь не нужен"
        : "вариантов не найдено (это не значит, что их нет)";
      grid.append(el("section", { class: "offer-column", "data-testid": `column-${role}`, "aria-labelledby": `column-title-${role}` },
        sectionTitle(`column-title-${role}`, COLUMN_TITLES[role], columns[role].length),
        ...(columns[role].length ? columns[role] : [el("p", { class: "meta empty" }, empty)])));
    }
    cards.append(grid);
  }
  // CART-UI-003: трансферы — отдельная секция, а не продолжение колонки «Транспорт обратно».
  // TRF-SELECT-01: «Как добраться» — только маршруты текущего плана (выбранная гостиница), справочно.
  const routes = planRoutes(trip);
  const planHasHotel = Boolean(plan && plan.components.some((component) => component.role === "hotel"));
  if (routes.length || planHasHotel) {
    const section = el("section", { class: "offer-section transfers", "data-testid": "transfer-section", "aria-labelledby": "transfer-title" },
      sectionTitle("transfer-title", "Как добраться", routes.length || null),
      el("p", { class: "meta" }, "Справочные автомобильные маршруты, а не услуги бронирования: время в пути — оценка без пробок, стоимость и наличие не оцениваются, в демо-покупку не входят. Маршруты относятся к выбранной гостинице и обновляются при её смене."));
    if (!routes.length) section.append(el("p", { class: "meta empty", "data-testid": "route-empty" }, "Маршрут до выбранной гостиницы пока не рассчитан"));
    for (const offer of routes) section.append(routeCard(offer));
    cards.append(section);
  }
  const candidates = trip.candidate_destinations || [];
  // UI-013: «Предложенные направления» — в своём постоянном месте панели (над вариантами), а не в конце
  // списка предложений: после выбора города и появления вариантов блок не перескакивает, содержимое
  // обновляется на месте и остаётся доступным для смены выбора.
  const destinations = $("destinations") || cards;
  if (destinations !== cards) destinations.replaceChildren();
  if (candidates.length) {
    // CITY-001: предложенные направления выбираются нажатием или клавиатурой; выбирается только «куда».
    const chosenName = trip.request && trip.request.destination ? trip.request.destination.name : null;
    const section = el("section", { class: "offer-section destinations", id: "destination-section-anchor", "data-testid": "destination-section", "aria-labelledby": "destination-title" },
      sectionTitle("destination-title", "Предложенные направления", candidates.length),
      el("p", { class: "meta" }, "Нажмите на направление, чтобы выбрать его как пункт назначения («куда»). Город отправления не меняется; направление можно сменить в любой момент."));
    for (const candidate of candidates) {
      const node = el("div", { class: "card", "data-testid": "destination-card" },
        el("div", {}, el("strong", {}, candidate.place.name), " ", modeBadge(candidate.source.data_mode)),
        el("div", {}, candidate.description),
        el("div", { class: "meta" }, `${candidate.cost_note}; климат: ${{ not_loaded: "нормы не загружены", norm: "климатическая норма", unknown: "неизвестно" }[candidate.climate_status] || candidate.climate_status}; визы: не проверено`));
      makeDestinationSelectable(node, candidate, candidate.place.name === chosenName);
      destinationNodes.set(candidate.place.name, node);
      section.append(node);
    }
    destinations.append(section);
  }

  const constraints = $("constraints");
  constraints.replaceChildren();
  const byId = Object.fromEntries(((trip.request && trip.request.constraints) || []).map((c) => [c.constraint_id, c]));
  const roleByOffer = Object.fromEntries((plan ? plan.components : []).map((c) => [c.offer_id, c.role]));
  for (const evaluation of trip.constraint_evaluations || []) {
    if (evaluation.status === "not_applicable") continue;
    const constraint = byId[evaluation.constraint_id];
    const label = constraint ? (FIELD_LABELS[constraint.field] || constraint.field) + (constraint.requiredness === "required" ? " (обязательно)" : "") : evaluation.constraint_id;
    const role = ROLE_LABELS[roleByOffer[evaluation.component_id] || evaluation.component_id];
    constraints.append(el("li", { class: `status-${evaluation.status}` }, `${label}${role ? ", " + role : ""}: ${STATUS[evaluation.status]} — ${evaluation.reason}`));
  }
  updateCartButton(trip);
  renderSelectionBar(trip);
  markChosenCities(trip);
}

function renderSelectionBar(trip) {
  const bar = $("selection-bar");
  bar.replaceChildren();
  const plan = trip.plan;
  if (!plan || !plan.components.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const titles = { outbound: "Туда", hotel: "Гостиница", return: "Обратно" };
  for (const component of plan.components) {
    if (!titles[component.role]) continue;
    bar.append(el("div", { "data-testid": `selected-${component.role}` }, `${titles[component.role]}: ${component.title}`));
  }
  bar.append(el("div", { class: "total", "data-testid": "selection-total" }, `Итого: ${money(plan.total_price)} — стоимость ${COMPLETENESS[plan.completeness]}`));
  const constraints = (trip.request && trip.request.constraints) || [];
  const budget = constraints.find((c) => c.field === "budget_total");
  if (budget) {
    const evaluation = (trip.constraint_evaluations || []).find((e) => e.constraint_id === budget.constraint_id && !e.component_id);
    const limit = money({ amount: String(budget.value), currency: trip.request.budget ? trip.request.budget.currency : "RUB" });
    const status = evaluation ? STATUS[evaluation.status] : "не оценено";
    bar.append(el("div", { class: evaluation ? `status-${evaluation.status}` : "", "data-testid": "selection-budget" }, `Бюджет: до ${limit} — ${status}`));
  }
}

// CART-UI-001: кнопка не «молчит». Если корзину собрать нельзя, нажатие объясняет причину.
function cartBlocker(trip) {
  if (!trip || !trip.plan || !trip.plan.components.length) {
    return "план поездки ещё не собран: задайте запрос или ответьте на уточняющие вопросы";
  }
  if (trip.status === "purchased_demo") return "демо-покупка по этой поездке уже оформлена";
  return null;
}

function updateCartButton(trip) {
  const button = $("cart-btn");
  const blocker = cartBlocker(trip);
  button.setAttribute("aria-disabled", blocker ? "true" : "false");
  button.title = blocker || "";
  button.classList.toggle("inactive", Boolean(blocker));
  $("cart-hint").textContent = blocker ? `Корзина недоступна: ${blocker}.` : "";
}

// UI-001: выбор альтернативы. Выбираемые роли — транспорт туда/обратно и гостиница.
const SELECTABLE_ROLES = new Set(["outbound", "return", "hotel"]);
// UI-012: карточки текущей отрисовки — чтобы после перерисовки найти выбранную и оставить её на месте.
const offerNodes = new Map();
const destinationNodes = new Map();

// UI-012: выбор в панели не двигает страницу. Положение карточки запоминается при нажатии; после
// изменений (сообщение в ленте выше, перерисовка панели) компенсируется только её сдвиг, без прокрутки
// к чату, к началу или концу. Фокус клавиатуры переходит на новую копию карточки без прокрутки.
function pinView(find) {
  const topOf = (node) => (node && node.getBoundingClientRect ? node.getBoundingClientRect().top : null);
  const start = find();
  const origin = topOf(start);
  const focused = Boolean(start) && document.activeElement === start;
  return function restore() {
    const node = find();
    if (!node) return;
    const now = topOf(node);
    if (origin != null && now != null && now !== origin && typeof window !== "undefined" && window.scrollBy) {
      window.scrollBy(0, now - origin);
    }
    if (focused && document.activeElement !== node && node.focus) node.focus({ preventScroll: true });
  };
}

function makeSelectable(node, role, offerId, title, selected) {
  if (selected) node.classList.add("selected");
  if (!SELECTABLE_ROLES.has(role)) return;
  offerNodes.set(offerId, node);
  node.classList.add("selectable");
  node.setAttribute("data-offer-id", offerId);
  node.setAttribute("data-role", role);
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-pressed", selected ? "true" : "false");
  node.title = selected ? "Выбрано" : "Нажмите, чтобы выбрать этот вариант";
  node.addEventListener("click", () => selectOffer(role, offerId, title));
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectOffer(role, offerId, title);
    }
  });
}

function makeDestinationSelectable(node, candidate, selected) {
  node.classList.add("selectable");
  if (selected) node.classList.add("selected");
  node.setAttribute("data-candidate-id", candidate.candidate_id);
  node.setAttribute("data-candidate-name", candidate.place.name);
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-pressed", selected ? "true" : "false");
  node.title = selected ? "Выбрано направлением" : "Нажмите, чтобы выбрать это направление";
  const choose = () => chooseDestination(candidate, { anchor: () => destinationNodes.get(candidate.place.name) || $("destinations") });
  node.addEventListener("click", choose);
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      choose();
    }
  });
}

// CITY-001/002: выбор направления — ход с одним полем «куда»; сервер дополняет им известные условия поездки
// (город отправления, даты, состав, незакрытые уточнения не теряются). Ход без LLM: платных вызовов нет.
// Направление меняется на любом этапе: A → B → A, после уточнений, плана и корзины (корзина — недействительна).
// UI-012/013: из чата (follow) лента показывает ответ; из панели (anchor) страница остаётся на месте.
async function chooseDestination(candidate, view = {}) {
  if (!state.trip) return null;
  const restore = view.anchor ? pinView(view.anchor) : () => {};
  if (state.busy) {
    busyNotice(view.follow);
    restore();
    return null;
  }
  const name = candidate.place.name;
  const current = state.trip.request && state.trip.request.destination;
  if (current && current.name === name) {
    say("assistant", `Направление «${name}» уже выбрано.`, view.follow);
    restore();
    return null;
  }
  state.busy = true;
  try {
    say("user", `Выбираю направление (куда): ${name}`, view.follow);
    restore();
    const turn = await sendMessage({ form: { destination: name } }, () => chooseDestination(candidate, view), view.follow);
    if (turn) resetCart();
    return turn;
  } finally {
    state.busy = false;
    restore();
  }
}

function currentSelections(trip) {
  // Сервер заменяет набор выбранных компонентов целиком: передаём текущий выбор по всем ролям,
  // чтобы выбор гостиницы не менял незаметно транспорт, и наоборот.
  const selections = {};
  for (const component of (trip && trip.plan ? trip.plan.components : [])) {
    if (SELECTABLE_ROLES.has(component.role)) selections[component.role] = component.offer_id;
  }
  return selections;
}

async function selectOffer(role, offerId, title) {
  if (!state.trip || state.busy) return;
  const restore = pinView(() => offerNodes.get(offerId));
  if (currentSelections(state.trip)[role] === offerId) {
    say("assistant", `Этот вариант (${ROLE_LABELS[role] || role}) уже выбран.`);
    restore();
    return;
  }
  state.busy = true;
  try {
    const selections = { ...currentSelections(state.trip), [role]: offerId };
    const trip = await call("POST", `${API}/trips/${state.trip.trip_id}/selections`, {
      request_id: requestId(),
      trip_revision: state.revision,
      selections,
    });
    resetCart();
    renderTrip(trip);
    const total = trip.plan ? `${money(trip.plan.total_price)} — стоимость ${COMPLETENESS[trip.plan.completeness]}` : "не рассчитан";
    // UI-012: подтверждение видно рядом с карточками (отметка «выбрано» и строка в панели выбора), а не только в чате.
    $("selection-bar").prepend(el("div", { class: "selection-status", role: "status", "data-testid": "selection-status" },
      `✓ Выбрано (${ROLE_LABELS[role] || role}): ${title}. Итог пересчитан.`));
    say("assistant", `Выбрано (${ROLE_LABELS[role] || role}): ${title}. Итог пересчитан: ${total}. Прежняя корзина недействительна.`);
  } catch (error) {
    say("assistant", `Не удалось выбрать вариант: ${error.message}`);
  } finally {
    state.busy = false;
    restore();
  }
}

function resetCart() {
  state.cart = null;
  $("cart").hidden = true;
  $("cart").replaceChildren();
}

// DATE-003/DATE-005: предложенные варианты дат — кнопки внутри сообщения ассистента, к которому относятся.
// Это предложения, а не бронь: цены и наличие — после выбора. Варианты прошлых ходов устаревают (не кликаются).
let optionButtons = [];
const cityButtons = [];

function retireOptions() {
  for (const button of optionButtons) {
    button.disabled = true;
    button.classList.add("stale");
    button.title = "Варианты устарели: выберите из последнего сообщения";
  }
  optionButtons = [];
}

function attachQuestionOptions(node, questions) {
  for (const question of questions || []) {
    const options = question.options || [];
    if (!options.length) continue;
    const group = el("div", { class: "question-options", role: "group", "aria-label": "Варианты дат поездки" });
    for (const option of options) {
      const button = el("button", { type: "button", class: "option", "data-testid": "question-option", "data-option-id": option.option_id }, option.label);
      button.title = "Выбрать эти даты (предложение, не бронирование)";
      button.addEventListener("click", () => chooseDates(question, option));
      optionButtons.push(button);
      group.append(button);
    }
    node.append(group);
  }
}

// CITY-002: предложенные города — кнопки в сообщении с предложением; остаются доступными для смены выбора.
function attachDestinationOptions(node, candidates) {
  const group = el("div", { class: "question-options cities", role: "group", "aria-label": "Предложенные направления" });
  for (const candidate of candidates) {
    const button = el("button", { type: "button", class: "option", "data-testid": "city-option", "data-candidate-name": candidate.place.name }, candidate.place.name);
    button.title = "Выбрать это направление («куда»); выбор можно изменить";
    button.addEventListener("click", () => chooseDestination(candidate, { follow: true }));
    cityButtons.push(button);
    group.append(button);
  }
  node.append(group);
}

function markChosenCities(trip) {
  const chosen = trip.request && trip.request.destination ? trip.request.destination.name : null;
  for (const button of cityButtons) {
    const selected = button.getAttribute("data-candidate-name") === chosen;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", selected ? "true" : "false");
  }
}

function setOptionsDisabled(disabled) {
  for (const button of optionButtons) button.disabled = disabled;
}

// Выбор варианта дат: ход без LLM; сервер сверяет вариант с текущей ревизией и сохраняет прочие условия.
async function chooseDates(question, option) {
  if (!state.trip) return;
  if (state.busy) {
    busyNotice(true);
    return;
  }
  const range = state.trip.request && state.trip.request.date_range;
  if (range && range.start === option.date_from && range.end === option.date_to) {
    say("assistant", `Даты ${option.label} уже выбраны.`, true);
    return;
  }
  state.busy = true;
  setOptionsDisabled(true);
  const offered = optionButtons;
  try {
    say("user", `Выбираю даты: ${option.label}`, true);
    const turn = await sendMessage({ choice: { ambiguity_id: question.ambiguity_id, option_id: option.option_id, date_from: option.date_from, date_to: option.date_to } },
      () => chooseDates(question, option), true);
    if (turn) resetCart();
  } finally {
    state.busy = false;
    if (optionButtons === offered) setOptionsDisabled(false);
  }
}

// CART-UI-002/003: корзина в логике путешествия; трансферы — отдельные секции «туда» и «обратно».
const CART_SECTIONS = [
  ["outbound", "Транспорт туда", ""],
  ["transfer_in", "Как добраться: к гостинице (справочно)", "transfer"],
  ["hotel", "Проживание", ""],
  ["transfer_out", "Как добраться: от гостиницы (справочно)", "transfer"],
  ["return", "Транспорт обратно", ""],
];

function cartItemNode(item) {
  const route = item.informational && item.kind === "transfer";
  return el("div", { class: "card", "data-testid": "cart-item", "data-role": item.role },
    el("div", {}, el("strong", {}, route ? routeTitle(item.title) : item.title), " ", modeBadge(item.data_mode)),
    el("div", {}, route
      ? `Стоимость: ${item.price.amount == null ? "неизвестна" : money(item.price)} — справочный маршрут, не бронируется`
      : `Цена: ${money(item.price)}${item.informational ? " — информационная позиция" : ""}`),
    el("div", { class: "meta" }, item.note || `Источник: ${item.source.provider}`));
}

function renderCart(cart) {
  state.cart = cart;
  const box = $("cart");
  box.hidden = false;
  box.replaceChildren(el("h3", {}, `Корзина ${cart.cart_id}`));
  const known = new Set(CART_SECTIONS.map(([role]) => role));
  const sections = [...CART_SECTIONS, ["other", "Прочее", ""]];
  for (const [role, title, kind] of sections) {
    const items = cart.items.filter((item) => (role === "other" ? !known.has(item.role) : item.role === role));
    if (!items.length) continue;
    box.append(el("section", { class: `cart-section ${kind}`.trim(), "data-testid": `cart-section-${role}`, "aria-labelledby": `cart-title-${role}` },
      el("h4", { class: "cart-section-title", id: `cart-title-${role}` }, title),
      ...items.map(cartItemNode)));
  }
  box.append(el("p", {}, `Итог: ${money(cart.total_price)} — ${COMPLETENESS[cart.completeness]}`));
  box.append(el("p", { class: "notice" }, cart.demo_notice));
  if (cart.status === "awaiting_confirmation") {
    const button = el("button", { type: "button", "data-testid": "open-confirm" }, "Перейти к подтверждению");
    button.addEventListener("click", openConfirm);
    box.append(button);
  } else if (cart.declined_at) {
    box.append(el("p", { class: "meta" }, "Отказ: ничего не оформлено."));
  }
}

function openConfirm() {
  const list = $("risk-list");
  list.replaceChildren(...state.cart.significant_unknowns.map((risk) => el("li", {}, risk)));
  $("consent-purchase").checked = false;
  $("consent-risk").checked = false;
  $("confirm-dialog").showModal();
}

function renderReceipt(receipt) {
  const box = $("receipt");
  box.hidden = false;
  box.replaceChildren(
    el("h3", {}, `Демо-чек ${receipt.receipt_id}`),
    el("p", { "data-testid": "receipt-notice" }, receipt.notice),
    el("p", {}, `Итог: ${money(receipt.total_price)} — ${COMPLETENESS[receipt.completeness]}`),
  );
}

async function refreshEvents() {
  if (!state.session) return;
  try {
    const response = await fetch(`${API}/sessions/${state.session.session_id}/events?after=0`);
    const body = await response.text();
    $("progress").hidden = !/turn_started/.test(body.split("\n\n").slice(-2).join(""));
  } catch (error) {
    $("progress").hidden = true;
  }
}

async function ensureSession() {
  if (state.session) return state.session;
  state.session = await call("POST", `${API}/sessions`);
  state.revision = state.session.trip_revision;
  $("llm-badge").textContent = `LLM: ${LLM_MODE_LABELS[state.session.llm_mode] || "режим неизвестен"}`;
  const dataMode = state.session.data_mode;
  $("mode-badge").textContent = `Данные: ${dataMode === "mixed" ? "Live (при сбое — ДЕМО)" : MODE_LABELS[dataMode] || dataMode}`;
  $("mode-badge").className = `badge badge-${dataMode === "mock" ? "mock" : "live"}`;
  // UI-005: одно уведомление о режиме данных вместо двух крупных баннеров; бейджи «ДЕМО» на карточках остаются.
  if (dataMode === "mock") {
    $("data-notice").textContent = "Демонстрационный режим: все предложения, цены и наличие синтетические (пометка «ДЕМО») — это не цены, не наличие и не расписание.";
  }
  applyManualVisibility();
  if (state.session.llm_mode === "live") {
    $("llm-notice").textContent = publicPrivacyNotice(state.session.privacy_notice) ||
      "LLM подключён: опишите поездку своими словами. Цены, даты и условия проверяет код, а не модель.";
    $("text-label").textContent = "Опишите поездку или изменение условий (текст уйдёт LLM-провайдеру)";
    // UI-003: при работающем LLM форма — запасной путь; свёрнута (DEC-08 без LLM — раскрыта).
    $("structured-details").open = false;
  } else if (state.session.llm_mode === "replay") {
    $("llm-notice").textContent = "LLM в режиме Replay: понимаются только заранее записанные фразы.";
  } else {
    offerManualMode("LLM не подключён: свободный текст не обрабатывается.");
  }
  renderTrip(state.session.state);
  return state.session;
}

function formPayload(form) {
  const data = new FormData(form);
  const value = (name) => (data.get(name) || "").toString().trim();
  const list = (name) => value(name).split(",").map((item) => item.trim()).filter(Boolean);
  const payload = {
    origin: value("origin") || null,
    destination: value("destination") || null,
    date_from: value("date_from") || null,
    date_to: value("date_to") || null,
    adults: value("adults") ? Number(value("adults")) : null,
    children_ages: list("children_ages").map(Number),
    return_trip: data.get("return_trip") === "on",
    wishes_tags: list("wishes_tags"),
    budget_required: data.get("budget_required") === "on",
    breakfast_required: data.get("breakfast_required") === "on",
  };
  if (value("budget_amount")) { payload.budget_amount = value("budget_amount"); payload.budget_currency = "RUB"; }
  if (value("transport_modes")) payload.transport_modes = [value("transport_modes")];
  if (data.get("breakfast") === "on") payload.breakfast = true;
  if (value("stars_min")) payload.stars_min = Number(value("stars_min"));
  if (data.get("pet") === "on") payload.pets = [{ kind: "dog", count: 1 }];
  if (data.get("room_family") === "on") payload.room_type = "family";
  return payload;
}

// UI-010 (решение владельца): кнопка «Без LLM», форма и предложения ручного режима скрыты из публичного
// интерфейса при подключённом LLM. Реализация, API и тесты ручного режима сохранены; для диагностики режим
// открывается адресом с параметром «?manual=1». Без LLM (DEC-08) форма — единственный способ ввода и видна.
const MANUAL_DIAGNOSTIC = typeof location !== "undefined" && /(?:^|[?&])manual=1(?:&|$)/.test(location.search || "");

function manualAllowed() {
  return MANUAL_DIAGNOSTIC || Boolean(state.session && state.session.llm_mode === "off");
}

function applyManualVisibility() {
  const visible = manualAllowed();
  $("manual-btn").hidden = !visible;
  $("structured-details").hidden = !visible;
  if (!visible) $("manual-offer").hidden = true;
}

// Уведомление о передаче данных сохраняется; фраза о скрытой форме заменяется на верную для интерфейса.
function publicPrivacyNotice(notice) {
  if (!notice || manualAllowed()) return notice;
  return notice.replace("Форма отправляется без LLM.", "Выбор вариантов нажатием (города, даты, предложения) обрабатывается без LLM.");
}

// UI-003: ручной режим «Без LLM» — существующая форма. Ход из формы сервер обрабатывает без LLM
// (ни Интерпретатора, ни Composer). Уже известные условия поездки переносятся в форму; поля, которые
// пользователь менял в форме сам, не перезаписываются.
const touchedFields = new Set();

function requestFormValues(request) {
  const by = Object.fromEntries((request.constraints || []).map((c) => [c.field, c]));
  const party = request.party || {};
  const modes = by.transport_mode && Array.isArray(by.transport_mode.value) ? by.transport_mode.value : [];
  const required = (field) => Boolean(by[field] && by[field].requiredness === "required");
  return {
    origin: request.origin ? request.origin.name : "",
    destination: request.destination ? request.destination.name : "",
    date_from: request.date_range ? request.date_range.start : "",
    date_to: request.date_range ? request.date_range.end : "",
    adults: party.adults == null ? "" : String(party.adults),
    children_ages: (party.children_ages || []).join(", "),
    return_trip: request.return_trip !== false,
    budget_amount: request.budget ? String(request.budget.amount) : "",
    budget_required: required("budget_total"),
    transport_modes: modes.length === 1 ? modes[0] : "",
    breakfast: Boolean(by.hotel_breakfast && by.hotel_breakfast.value === true),
    breakfast_required: required("hotel_breakfast"),
    stars_min: by.hotel_stars_min ? String(by.hotel_stars_min.value) : "",
    pet: (party.pets || []).length > 0,
    room_family: Boolean(by.hotel_room_type && by.hotel_room_type.value === "family"),
    wishes_tags: (request.wishes_tags || []).join(", "),
  };
}

function fillFormFromTrip() {
  const request = state.trip && state.trip.request;
  const form = $("trip-form");
  if (!request || !form.elements) return false;
  for (const [name, value] of Object.entries(requestFormValues(request))) {
    const input = form.elements.namedItem(name);
    if (!input || touchedFields.has(name)) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  }
  return true;
}

function openManualMode() {
  const filled = fillFormFromTrip();
  const details = $("structured-details");
  details.open = true;
  $("manual-offer").hidden = true;
  if (details.scrollIntoView) details.scrollIntoView({ behavior: "smooth", block: "start" });
  say("assistant", "Ручной режим без LLM: проверьте условия в форме и нажмите «Подобрать». " +
    (filled ? "Уже известные условия перенесены в форму. " : "") +
    "Запрос из формы обрабатывается без LLM.");
}

function offerManualMode(reason) {
  if (!manualAllowed()) return;
  const button = el("button", { type: "button", "data-testid": "manual-offer-button" }, "Заполнить форму без LLM");
  button.addEventListener("click", openManualMode);
  $("manual-offer").replaceChildren(el("span", {}, `${reason} `), button);
  $("manual-offer").hidden = false;
}

$("manual-btn").addEventListener("click", openManualMode);
for (const type of ["input", "change"]) {
  $("trip-form").addEventListener(type, (event) => {
    if (event.target && event.target.name) touchedFields.add(event.target.name);
  });
}

// Ход диалога: ответ ассистента — одно сообщение ленты; вопросы уже входят в его текст, варианты выбора
// (даты, предложенные города) прикрепляются к нему же. Отдельной панели «Вопрос» нет (UI-008).
async function sendMessage(body, retry, follow = false) {
  await ensureSession();
  $("progress").hidden = false;
  try {
    const turn = await call("POST", `${API}/sessions/${state.session.session_id}/messages`, {
      request_id: requestId(),
      trip_revision: state.revision,
      ...body,
    });
    retireOptions();
    const node = say("assistant", turn.assistant_text, follow);
    attachQuestionOptions(node, turn.questions);
    const candidates = turn.state.candidate_destinations || [];
    const request = turn.state.request;
    if (candidates.length && request && !request.destination && !(turn.questions || []).length) {
      attachDestinationOptions(node, candidates);
    }
    renderTrip(turn.state);
    if (body.text && turn.input_mode === "llm_unavailable") {
      offerManualMode("LLM сейчас недоступен: текст не обработан и сохранён в поле ввода. Можно продолжить без LLM.");
    } else {
      $("manual-offer").hidden = true;
    }
    return turn;
  } catch (error) {
    if (error.status === 409) {
      await refreshTrip();
      sayRetry(`Действие не выполнено: ${error.message}. Состояние поездки обновлено — повторите действие.`, retry, follow);
    } else {
      sayRetry(`Ошибка: ${error.message}`, error.status ? null : retry, follow);
    }
    if (body.text) offerManualMode("Текст не обработан и сохранён в поле ввода. Можно продолжить без LLM.");
    return null;
  } finally {
    $("progress").hidden = true;
  }
}

$("trip-form").addEventListener("submit", (event) => {
  event.preventDefault();
  say("user", "Запрос из формы (без LLM)", true);
  sendMessage({ form: formPayload(event.target) }, undefined, true).then((turn) => { if (turn) touchedFields.clear(); });
});

// UI-011: отправленный текст сразу уходит из поля в ленту (один раз), не дожидаясь ответа LLM. Пока ход
// идёт, кнопка «Отправить» недоступна и повторная отправка не начинается. Если текст не обработан
// (нет связи, ошибка сервера, LLM недоступен), он возвращается в поле — если пользователь не начал
// писать новое сообщение; при сбое связи в ленте есть кнопка «Повторить».
function setSending(sending) {
  const button = $("text-send");
  if (button) button.disabled = sending;
  $("text-form").setAttribute("aria-busy", sending ? "true" : "false");
}

async function sendText(value) {
  if (state.busy) {
    busyNotice(true);
    return null;
  }
  state.busy = true;
  setSending(true);
  try {
    const turn = await sendMessage({ text: value }, () => sendText(value), true);
    const input = $("text-input");
    if (turn && turn.input_mode !== "llm_unavailable") {
      if (input.value.trim() === value) input.value = "";
    } else if (!input.value.trim()) {
      input.value = value;
    }
    return turn;
  } finally {
    state.busy = false;
    setSending(false);
  }
}

$("text-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("text-input").value.trim();
  if (!value) return;
  if (state.busy) {
    busyNotice(true);
    return;
  }
  $("text-input").value = "";
  say("user", value, true);
  sendText(value);
});

$("cart-btn").addEventListener("click", async () => {
  const blocker = cartBlocker(state.trip);
  if (blocker) {
    say("assistant", `Корзину пока нельзя сформировать: ${blocker}.`);
    return;
  }
  if (state.busy) return;
  state.busy = true;
  const button = $("cart-btn");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Формирую корзину…";
  try {
    const response = await call("POST", `${API}/trips/${state.trip.trip_id}/cart`, { request_id: requestId(), trip_revision: state.revision });
    state.revision = response.trip_revision;
    renderCart(response.cart);
    try {
      renderTrip(await call("GET", `${API}/trips/${state.trip.trip_id}`));
    } catch (error) {
      // Корзина уже создана; устаревшая панель поездки не мешает подтверждению по ревизии корзины.
    }
    const items = response.cart.items.length;
    say("assistant", `Корзина сформирована: позиций — ${items}, итог ${money(response.cart.total_price)} (${COMPLETENESS[response.cart.completeness]}). Проверьте её ниже и перейдите к подтверждению.`);
    if ($("cart").scrollIntoView) $("cart").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    say("assistant", `Ошибка корзины: ${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = label;
    state.busy = false;
  }
});

$("confirm-form").addEventListener("submit", async (event) => {
  const action = event.submitter ? event.submitter.value : "cancel";
  if (action === "cancel") {
    try {
      const response = await call("POST", `${API}/carts/${state.cart.cart_id}/decline`, { request_id: requestId(), trip_revision: state.revision });
      renderCart(response.cart);
      const trip = await call("GET", `${API}/trips/${state.trip.trip_id}`);
      renderTrip(trip);
      say("assistant", "Отказ принят: ничего не оформлено.");
    } catch (error) {
      say("assistant", `Ошибка: ${error.message}`);
    }
    return;
  }
  try {
    const response = await call("POST", `${API}/carts/${state.cart.cart_id}/confirm`, {
      request_id: requestId(),
      trip_revision: state.revision,
      confirmation_id: state.confirmationId || (state.confirmationId = requestId()),
      purchase_consent: $("consent-purchase").checked,
      risk_acknowledgement: $("consent-risk").checked,
    });
    renderCart(response.cart);
    renderReceipt(response.receipt);
    const trip = await call("GET", `${API}/trips/${state.trip.trip_id}`);
    renderTrip(trip);
  } catch (error) {
    say("assistant", `Подтверждение не выполнено: ${error.message}`);
  }
});

ensureSession().catch((error) => say("assistant", `Ошибка: ${error.message}`));
setInterval(refreshEvents, 5000);
