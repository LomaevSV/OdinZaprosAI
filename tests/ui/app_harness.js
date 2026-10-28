// Лёгкий UI-стенд для app.js: минимальный DOM без зависимостей + реальный API на loopback.
// Запуск: ODIN_BASE=http://127.0.0.1:PORT node tests/ui/app_harness.js
// Печатает одну строку JSON с наблюдениями; проверки делает pytest (tests/ui/test_browser_handlers.py).
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const BASE = process.env.ODIN_BASE;
if (!BASE) throw new Error("ODIN_BASE не задан");

class ClassList {
  constructor(owner) { this.owner = owner; this.set = new Set(); }
  add(...names) { names.forEach((n) => this.set.add(n)); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); }
  contains(name) { return this.set.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    return on;
  }
}

class Node {
  constructor() { this.children = []; this.parent = null; }
  append(...items) {
    for (const item of items) {
      const child = item instanceof Node ? item : new TextNode(String(item));
      child.parent = this;
      this.children.push(child);
    }
  }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(value) { this.replaceChildren(new TextNode(value == null ? "" : String(value))); }
}

class TextNode extends Node {
  constructor(value) { super(); this.value = value; }
  get textContent() { return this.value; }
}

class Element extends Node {
  constructor(tag, id) {
    super();
    this.tagName = tag.toUpperCase();
    this.id = id || "";
    this.attributes = {};
    this.classList = new ClassList(this);
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.title = "";
    this.scrolled = 0;
    this.scrollTop = 0;
    this.scrollHeight = 0;
  }
  get className() { return [...this.classList.set].join(" "); }
  set className(value) { this.classList.set = new Set(String(value).split(/\s+/).filter(Boolean)); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  dispatch(type, extra = {}) {
    if (type === "click" && this.disabled) return Promise.resolve([]);  // как в браузере: disabled не кликается
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
    return Promise.all((this.listeners[type] || []).map((handler) => handler(event)));
  }
  scrollIntoView() { this.scrolled += 1; }
  showModal() { this.open = true; }
  walk(visit) { visit(this); for (const child of this.children) if (child instanceof Element) child.walk(visit); }
}

const html = fs.readFileSync(path.join(ROOT, "src/odin/static/index.html"), "utf8");
const byId = {};
for (const match of html.matchAll(/<(\w+)[^>]*\sid="([^"]+)"/g)) byId[match[2]] = new Element(match[1], match[2]);
for (const match of html.matchAll(/<(\w+)[^>]*\sid="([^"]+)"[^>]*\shidden[\s>]/g)) byId[match[2]].hidden = true;
for (const match of html.matchAll(/<button[^>]*\sid="([^"]+)"[^>]*>([^<]*)</g)) byId[match[1]].textContent = match[2];
for (const match of html.matchAll(/<button[^>]*\sid="([^"]+)"[^>]*\sdisabled[\s>]/g)) byId[match[1]].disabled = true;
const textOf = (id) => (byId[id] ? byId[id].textContent : null);

// Поля формы ручного ввода (UI-003): значения и флажки из разметки, как у браузера при загрузке.
const formFields = {};
const tripFormHtml = (html.match(/<form id="trip-form"[\s\S]*?<\/form>/) || [""])[0];
for (const match of tripFormHtml.matchAll(/<(input|select)\b([^>]*)>/g)) {
  const attrs = match[2];
  const name = (attrs.match(/\sname="([^"]+)"/) || [])[1];
  if (!name) continue;
  const field = new Element(match[1]);
  field.name = name;
  field.type = match[1] === "select" ? "select-one" : (attrs.match(/\stype="([^"]+)"/) || [, "text"])[1];
  field.value = (attrs.match(/\svalue="([^"]*)"/) || [, ""])[1];
  field.checked = /\schecked(\s|$)/.test(attrs);
  formFields[name] = field;
}
byId["trip-form"].elements = { namedItem: (name) => formFields[name] || null };

const document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new Element(tag),
  createTextNode: (value) => new TextNode(value),
};

const formData = new Map();
function fieldValues() {
  const values = {};
  for (const [name, field] of Object.entries(formFields)) {
    if (field.type === "checkbox") { if (field.checked) values[name] = "on"; } else values[name] = field.value;
  }
  return values;
}
class FormData {
  constructor(form) { this.values = formData.get(form.id) || (form.id === "trip-form" ? fieldValues() : {}); }
  get(name) { return name in this.values ? this.values[name] : null; }
}

const requests = [];
async function fetchProxy(url, options = {}) {
  requests.push({ method: options.method || "GET", path: String(url).replace(/\?.*/, ""), body: options.body ? JSON.parse(options.body) : null });
  return fetch(BASE + url, options);
}

const sandbox = {
  document, FormData, Node, console, crypto: globalThis.crypto, fetch: fetchProxy,
  setInterval: () => 0, Date, Number, Object, JSON, Set, Map, Array, String, Boolean, Error, Promise,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, "src/odin/static/app.js"), "utf8"), sandbox, { filename: "app.js" });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate, label, timeout = 20000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeout) throw new Error(`timeout: ${label}`);
    await sleep(25);
  }
}
const messages = () => byId.messages.children.map((child) => child.textContent);
function selectableCards() {
  const found = [];
  byId.cards.walk((node) => { if (node.getAttribute && node.getAttribute("data-offer-id")) found.push(node); });
  return found;
}
const selected = (role) => selectableCards().filter((c) => c.getAttribute("data-role") === role && c.classList.contains("selected")).map((c) => c.getAttribute("data-offer-id"));
const count = (method, fragment) => requests.filter((r) => r.method === method && r.path.includes(fragment)).length;
const formValues = () => Object.fromEntries(Object.entries(formFields).map(([n, f]) => [n, f.type === "checkbox" ? f.checked : f.value]));
function findAll(root, predicate) {
  const found = [];
  root.walk((node) => { if (predicate(node)) found.push(node); });
  return found;
}
const byTestid = (root, prefix) => findAll(root, (n) => n.getAttribute && (n.getAttribute("data-testid") || "").startsWith(prefix));
const destinationCards = () => byTestid(byId.cards, "destination-card");
// Язык интерфейса: все тексты, которые видит пользователь (включая подсказки title/aria-label).
const VISIBLE_IDS = ["messages", "questions", "trip-summary", "cards", "selection-bar", "cart-hint", "constraints", "cart", "receipt", "mode-badge", "llm-badge", "data-notice", "llm-notice", "manual-offer", "text-label", "cart-btn", "revision", "progress"];
function visibleText() {
  const parts = [];
  for (const id of VISIBLE_IDS) {
    const node = byId[id];
    if (!node) continue;
    parts.push(node.textContent);
    node.walk((child) => {
      if (child.title) parts.push(child.title);
      const label = child.getAttribute && child.getAttribute("aria-label");
      if (label) parts.push(label);
    });
  }
  return parts.filter(Boolean);
}
const state_request = () => { const current = vm.runInContext("state", sandbox); return current.trip && current.trip.request; };

(async () => {
  const out = { form_values_initial: formValues() };
  await until(() => requests.some((r) => r.path.endsWith("/sessions")) && sandbox.document && byId.revision.textContent, "session");
  if (process.env.HARNESS_MODE === "session") {
    out.llm_badge = textOf("llm-badge");
    out.structured_open = byId["structured-details"] ? byId["structured-details"].open !== false : null;
    out.post_requests = requests.filter((r) => r.method === "POST").map((r) => r.path.replace(/\/(s|t)-[0-9a-f]+/g, "/$1-*"));
    process.stdout.write(JSON.stringify(out) + "\n");
    return;
  }

  if (process.env.HARNESS_MODE === "manual") {
    // UI-003: кнопка «Без LLM», предложение при недоступном LLM, перенос условий, ход формы без LLM.
    const visibleOffer = () => !byId["manual-offer"].hidden;
    const offerButton = () => {
      let found = null;
      byId["manual-offer"].walk((node) => { if (node.getAttribute && node.getAttribute("data-testid") === "manual-offer-button") found = node; });
      return found;
    };
    out.manual_button = textOf("manual-btn");
    out.offer_at_start = visibleOffer();
    if (process.env.HARNESS_FORM_FIELDS) {
      for (const pair of process.env.HARNESS_FORM_FIELDS.split(",")) {
        const [name, value] = pair.split("=");
        formFields[name].value = value;
      }
    }
    const sayText = async (value) => {
      const shown = messages().length;
      byId["text-input"].value = value;
      await byId["text-form"].dispatch("submit");
      await until(() => messages().length >= shown + 2 && byId.progress.hidden, "text turn");
      await sleep(50);
      return messages().slice(shown + 1).join(" | ");
    };
    if (process.env.HARNESS_TEXT_OK) {
      out.ok_reply = await sayText(process.env.HARNESS_TEXT_OK);
      out.ok_text_cleared = byId["text-input"].value === "";
      out.offer_after_ok = visibleOffer();
    }
    if (process.env.HARNESS_TEXT_FAIL) {
      out.fail_reply = await sayText(process.env.HARNESS_TEXT_FAIL);
      out.fail_text_kept = byId["text-input"].value;
      out.offer_after_fail = visibleOffer();
      out.offer_text = textOf("manual-offer");
    }
    if (process.env.HARNESS_TOUCH) {
      // Поле, изменённое пользователем в форме, не перезаписывается условиями из диалога.
      const [name, value] = process.env.HARNESS_TOUCH.split("=");
      formFields[name].value = value;
      await byId["trip-form"].dispatch("input", { target: formFields[name] });
    }
    byId["structured-details"].open = false;
    const opener = process.env.HARNESS_OPEN === "offer" ? offerButton() : byId["manual-btn"];
    out.opener_found = Boolean(opener);
    await opener.dispatch("click");
    out.structured_open_after = byId["structured-details"].open === true;
    out.manual_message = messages().slice(-1)[0];
    out.offer_after_open = visibleOffer();
    out.form_values = Object.fromEntries(Object.entries(formFields).map(([n, f]) => [n, f.type === "checkbox" ? f.checked : f.value]));
    if (process.env.HARNESS_FORM_DATES) {
      const [from, to] = process.env.HARNESS_FORM_DATES.split(",");
      formFields.date_from.value = from;
      formFields.date_to.value = to;
    }
    const posts = count("POST", "/messages");
    const shown = messages().length;
    await byId["trip-form"].dispatch("submit");
    await until(() => count("POST", "/messages") > posts && messages().length >= shown + 2 && byId.progress.hidden, "form turn");
    out.form_reply = messages().slice(shown + 1).join(" | ");
    out.form_user_message = messages()[shown];
    out.cards_after_form = selectableCards().length;
    process.stdout.write(JSON.stringify(out) + "\n");
    return;
  }

  if (process.env.HARNESS_MODE === "inspiration") {
    // CITY-001: предложенное направление выбирается нажатием/клавиатурой; «откуда» и прочие условия не теряются.
    const today = new Date();
    const start = new Date(today.getTime() + 30 * 86400000).toISOString().slice(0, 10);
    const end = new Date(today.getTime() + 32 * 86400000).toISOString().slice(0, 10);
    formData.set("trip-form", { origin: "Москва", destination: "", wishes_tags: "море", date_from: start, date_to: end, adults: "2", return_trip: "on" });
    await byId["trip-form"].dispatch("submit");
    await until(() => destinationCards().length > 0, "destination cards");
    const cards = destinationCards();
    out.destination_cards = cards.map((c) => ({ id: c.getAttribute("data-candidate-id"), role: c.getAttribute("role"), tabindex: c.getAttribute("tabindex"), selectable: c.classList.contains("selectable"), text: c.textContent }));
    const first = cards[0];
    const posts = count("POST", "/messages");
    await first.dispatch("click");
    await until(() => count("POST", "/messages") > posts && state_request() && state_request().destination, "destination chosen");
    await until(() => byId.progress.hidden, "turn done");
    const chosenPost = requests.filter((r) => r.method === "POST" && r.path.endsWith("/messages")).slice(-1)[0];
    out.choice_body = chosenPost.body;
    out.after_click = { origin: state_request().origin && state_request().origin.name, destination: state_request().destination.name, adults: state_request().party.adults, dates: state_request().date_range };
    out.offer_cards_after_click = selectableCards().length;
    out.selected_destination = destinationCards().filter((c) => c.classList.contains("selected")).map((c) => c.getAttribute("data-candidate-id"));
    const before = count("POST", "/messages");
    await destinationCards().find((c) => c.classList.contains("selected")).dispatch("click");
    out.reclick_requests = count("POST", "/messages") - before;
    out.reclick_message = messages().slice(-1)[0];
    const other = destinationCards().find((c) => !c.classList.contains("selected"));
    if (other) {
      const name = other.textContent;
      await other.dispatch("keydown", { key: "Enter" });
      await until(() => count("POST", "/messages") > before && byId.progress.hidden && state_request().destination.name !== out.after_click.destination, "second destination");
      out.after_keyboard = { origin: state_request().origin && state_request().origin.name, destination: state_request().destination.name, card_text: name };
    }
    out.visible_text = visibleText();
    process.stdout.write(JSON.stringify(out) + "\n");
    return;
  }

  // CART-UI-001: нажатие без плана не «молчит».
  const before = messages().length;
  await byId["cart-btn"].dispatch("click");
  out.cart_without_plan_message = messages().slice(before).join(" | ");
  out.cart_without_plan_requests = count("POST", "/cart");
  out.cart_hint_without_plan = textOf("cart-hint");

  // Запрос из формы.
  const today = new Date();
  const start = new Date(today.getTime() + 30 * 86400000).toISOString().slice(0, 10);
  const end = new Date(today.getTime() + 32 * 86400000).toISOString().slice(0, 10);
  formData.set("trip-form", { origin: "Москва", destination: "Казань", date_from: start, date_to: end, adults: "2", return_trip: "on" });
  await byId["trip-form"].dispatch("submit");
  await until(() => byId.cards.children.length > 0, "cards");
  out.selectable_count = selectableCards().length;
  if (!out.selectable_count) { process.stdout.write(JSON.stringify(out) + "\n"); return; }
  out.roles = [...new Set(selectableCards().map((c) => c.getAttribute("data-role")))].sort();
  out.initial = { hotel: selected("hotel"), outbound: selected("outbound"), return: selected("return") };
  out.cart_hint_with_plan = textOf("cart-hint");
  // P2: колонки, панель выбора, баннер Mock, повторы пометок, свёрнутая форма.
  const testids = [];
  byId.cards.walk((node) => { if (node.getAttribute && node.getAttribute("data-testid")) testids.push(node.getAttribute("data-testid")); });
  out.columns = testids.filter((t) => t.startsWith("column-"));
  out.mock_banner_present = Boolean(byId["mock-banner"]);
  out.data_notice = textOf("data-notice");
  out.column_titles = findAll(byId.cards, (n) => n.classList && n.classList.contains("column-title")).map((n) => n.textContent);
  out.column_labelled = byTestid(byId.cards, "column-").map((n) => n.getAttribute("aria-labelledby"));
  out.transfer_section = byTestid(byId.cards, "transfer-section").map((n) => n.textContent);
  out.transfers_inside_columns = byTestid(byId.cards, "column-").some((column) => /Трансфер|трансфер/.test(column.textContent));
  out.selection_bar = textOf("selection-bar");
  out.selection_bar_hidden = byId["selection-bar"] ? byId["selection-bar"].hidden : null;
  out.demo_card_availability_lines = selectableCards().filter((c) => c.textContent.includes("Наличие: не подтверждено (ДЕМО)")).length;
  out.structured_open = byId["structured-details"] ? byId["structured-details"].open !== false : null;

  // UI-001: выбор другой гостиницы.
  const altHotel = selectableCards().find((c) => c.getAttribute("data-role") === "hotel" && !c.classList.contains("selected"));
  out.alt_hotel = altHotel.getAttribute("data-offer-id");
  const totalBefore = byId["trip-summary"].textContent;
  await altHotel.dispatch("click");
  await until(() => count("POST", "/selections") >= 1 && selected("hotel")[0] === out.alt_hotel, "hotel selected");
  out.after_hotel = { hotel: selected("hotel"), outbound: selected("outbound"), return: selected("return") };
  out.summary_changed = byId["trip-summary"].textContent !== totalBefore;
  out.selection_message = messages().slice(-1)[0];

  // UI-001: выбор другого транспорта туда — гостиница не меняется.
  const altOut = selectableCards().find((c) => c.getAttribute("data-role") === "outbound" && !c.classList.contains("selected"));
  out.alt_outbound = altOut ? altOut.getAttribute("data-offer-id") : null;
  if (altOut) {
    await altOut.dispatch("keydown", { key: "Enter" });
    await until(() => count("POST", "/selections") >= 2 && selected("outbound")[0] === out.alt_outbound, "outbound selected");
  }
  out.after_outbound = { hotel: selected("hotel"), outbound: selected("outbound"), return: selected("return") };

  // Повторный клик по уже выбранному — без запроса.
  const sameBefore = count("POST", "/selections");
  await selectableCards().find((c) => c.getAttribute("data-offer-id") === out.alt_hotel).dispatch("click");
  out.reselect_requests = count("POST", "/selections") - sameBefore;

  // CART-UI-001: корзина по актуальному выбору.
  await byId["cart-btn"].dispatch("click");
  await until(() => !byId.cart.hidden, "cart shown");
  out.cart_posts = count("POST", "/cart");
  out.cart_text = byId.cart.textContent;
  out.cart_sections = byTestid(byId.cart, "cart-section-").map((n) => n.getAttribute("data-testid").replace("cart-section-", ""));
  out.cart_section_titles = findAll(byId.cart, (n) => n.classList && n.classList.contains("cart-section-title")).map((n) => n.textContent);
  out.cart_items_by_section = Object.fromEntries(byTestid(byId.cart, "cart-section-").map((section) => [section.getAttribute("data-testid").replace("cart-section-", ""), byTestid(section, "cart-item").map((i) => i.getAttribute("data-role"))]));
  out.cart_scrolled = byId.cart.scrolled;
  out.cart_message = messages().slice(-1)[0];
  out.cart_button_restored = byId["cart-btn"].textContent;
  out.revision_text = byId.revision.textContent;

  // Выбор после корзины делает её недействительной в интерфейсе.
  const another = selectableCards().find((c) => c.getAttribute("data-role") === "hotel" && !c.classList.contains("selected"));
  if (another) {
    await another.dispatch("click");
    await until(() => byId.cart.hidden, "cart reset after selection");
  }
  out.cart_hidden_after_reselect = byId.cart.hidden;
  out.requests = requests.filter((r) => r.method === "POST").map((r) => r.path.replace(/\/(s|t|cart)-[0-9a-f]+/g, "/$1-*"));
  out.visible_text = visibleText();
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((error) => {
  process.stdout.write(JSON.stringify({ error: String(error && error.stack || error) }) + "\n");
  process.exitCode = 1;
});
