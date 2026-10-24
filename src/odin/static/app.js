/* О!дин запрос — минимальный интерфейс S1 (UI-01). Чистый JS без сборки. */
"use strict";

const API = "/api/v1";
const state = { session: null, trip: null, cart: null, revision: null };

const $ = (id) => document.getElementById(id);
const text = (value) => document.createTextNode(value == null ? "" : String(value));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("data-")) node.setAttribute(key, value);
    else node[key] = value;
  }
  for (const child of children) node.append(child instanceof Node ? child : text(child));
  return node;
}

function requestId() {
  return "req-" + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "").slice(0, 20) : Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
}

async function call(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload.message || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function say(role, message) {
  $("messages").append(el("div", { class: `msg ${role}` }, message));
  $("messages").scrollTop = $("messages").scrollHeight;
}

function modeBadge(mode) {
  const labels = { mock: "ДЕМО (Mock)", replay: "Replay: данные на дату", live: "Live" };
  return el("span", { class: `badge badge-${mode}`, "data-testid": "mode-badge" }, labels[mode] || mode);
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

function renderTrip(trip) {
  state.trip = trip;
  state.revision = trip.revision;
  $("revision").textContent = `ревизия ${trip.revision}`;
  const summary = $("trip-summary");
  summary.replaceChildren();
  const request = trip.request;
  if (request) {
    const party = request.party.adults ? `${request.party.adults} взр.` + (request.party.children_ages.length ? `, дети: ${request.party.children_ages.join(", ")}` : "") : "состав не указан";
    const dates = request.date_range ? `${request.date_range.start} — ${request.date_range.end}` : "даты не заданы";
    summary.append(
      el("p", {}, `${request.origin ? request.origin.name : "?"} → ${request.destination ? request.destination.name : "направление не задано"}`),
      el("p", {}, `Даты: ${dates}. Гости: ${party}.`),
    );
  }
  const plan = trip.plan;
  if (plan) {
    summary.append(
      el("p", { "data-testid": "plan-total" }, `Итог: ${money(plan.total_price)} — стоимость ${COMPLETENESS[plan.completeness]}`),
      el("p", { class: "meta" }, `Режим поездки: ${plan.trip_data_mode === "mock" ? "ДЕМО (Mock)" : plan.trip_data_mode}`),
    );
    if (plan.unknowns.length) summary.append(el("p", { class: "meta" }, "Не подтверждено: " + plan.unknowns.join("; ")));
    for (const check of plan.connection_checks) {
      summary.append(el("p", { class: `status-${check.status}`, "data-testid": "connection-check" },
        `Стык ${check.from_component} → ${check.to_component}: ${STATUS[check.status]} (${check.basis})`));
    }
  }
  if (trip.search_termination) {
    summary.append(el("p", { class: "meta", "data-testid": "stop-reason" },
      `Поиск: ${trip.search_termination.reason}. Это не означает, что вариантов нет.`));
  }

  const cards = $("cards");
  cards.replaceChildren();
  const hotels = Object.fromEntries((trip.hotels || []).map((hotel) => [hotel.hotel_id, hotel]));
  const chosen = new Set((plan ? plan.components : []).map((component) => component.offer_id));
  for (const offer of trip.transport_offers || []) {
    const node = card(offer.title, `${offer.direction === "outbound" ? "Туда" : "Обратно"}: ${new Date(offer.departure_at).toLocaleString("ru-RU")} → ${new Date(offer.arrival_at).toLocaleString("ru-RU")}`, offer.price, offer.source, offer.source.data_mode, [`Наличие: ${offer.availability.availability_status}`]);
    if (chosen.has(offer.offer_id)) node.classList.add("selected");
    cards.append(node);
  }
  for (const offer of trip.hotel_offers || []) {
    const hotel = hotels[offer.hotel_id] || {};
    const stars = hotel.star_rating == null ? "категория не указана" : `${hotel.star_rating}★`;
    const node = card(hotel.name || offer.hotel_id, `${stars}; завтрак: ${offer.terms.breakfast_included == null ? "не подтверждён" : offer.terms.breakfast_included ? "да" : "нет"}`, offer.price_total, offer.source, offer.source.data_mode, [`Наличие: ${offer.availability.availability_status} (${offer.availability.unknown_reason || offer.availability.evidence_level})`]);
    if (chosen.has(offer.offer_id)) node.classList.add("selected");
    cards.append(node);
  }
  for (const offer of trip.transfer_options || []) {
    cards.append(card(offer.title, `Время: ~${offer.duration_minutes} мин (оценка)`, offer.price, offer.source, offer.source.data_mode, offer.limitations));
  }
  for (const candidate of trip.candidate_destinations || []) {
    cards.append(el("div", { class: "card", "data-testid": "destination-card" },
      el("div", {}, el("strong", {}, candidate.place.name), " ", modeBadge(candidate.source.data_mode)),
      el("div", {}, candidate.description),
      el("div", { class: "meta" }, `${candidate.cost_note}; климат: ${candidate.climate_status}; визы: не проверено`)));
  }

  const constraints = $("constraints");
  constraints.replaceChildren();
  for (const evaluation of trip.constraint_evaluations || []) {
    constraints.append(el("li", { class: `status-${evaluation.status}` }, `${evaluation.constraint_id}: ${STATUS[evaluation.status]} — ${evaluation.reason}`));
  }
  $("cart-btn").disabled = !(plan && plan.components.length) || trip.status === "purchased_demo";
}

function renderQuestions(questions) {
  const box = $("questions");
  box.replaceChildren();
  for (const question of questions || []) box.append(el("div", { class: "msg assistant" }, `Вопрос: ${question.question}`));
}

function renderCart(cart) {
  state.cart = cart;
  const box = $("cart");
  box.hidden = false;
  box.replaceChildren(el("h3", {}, `Корзина ${cart.cart_id}`));
  for (const item of cart.items) {
    box.append(el("div", { class: "card" },
      el("div", {}, el("strong", {}, item.title), " ", modeBadge(item.data_mode)),
      el("div", {}, `Цена: ${money(item.price)}${item.informational ? " — информационная позиция" : ""}`),
      el("div", { class: "meta" }, item.note || `Источник: ${item.source.provider}`)));
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
  $("llm-badge").textContent = `LLM: ${state.session.llm_mode}`;
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
  return payload;
}

async function sendMessage(body) {
  await ensureSession();
  $("progress").hidden = false;
  try {
    const turn = await call("POST", `${API}/sessions/${state.session.session_id}/messages`, {
      request_id: requestId(),
      trip_revision: state.revision,
      ...body,
    });
    say("assistant", turn.assistant_text);
    renderQuestions(turn.questions);
    renderTrip(turn.state);
  } catch (error) {
    say("assistant", `Ошибка: ${error.message}`);
  } finally {
    $("progress").hidden = true;
  }
}

$("trip-form").addEventListener("submit", (event) => {
  event.preventDefault();
  say("user", "Запрос из формы");
  sendMessage({ form: formPayload(event.target) });
});

$("text-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("text-input").value.trim();
  if (!value) return;
  say("user", value);
  sendMessage({ text: value });
});

$("cart-btn").addEventListener("click", async () => {
  try {
    const response = await call("POST", `${API}/trips/${state.trip.trip_id}/cart`, { request_id: requestId(), trip_revision: state.revision });
    state.revision = response.trip_revision;
    renderCart(response.cart);
  } catch (error) {
    say("assistant", `Ошибка корзины: ${error.message}`);
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
