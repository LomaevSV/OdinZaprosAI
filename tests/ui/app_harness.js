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

const document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new Element(tag),
  createTextNode: (value) => new TextNode(value),
};

const formData = new Map();
class FormData {
  constructor(form) { this.values = formData.get(form.id) || {}; }
  get(name) { return name in this.values ? this.values[name] : null; }
}

const requests = [];
async function fetchProxy(url, options = {}) {
  requests.push({ method: options.method || "GET", path: String(url).replace(/\?.*/, "") });
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

(async () => {
  const out = {};
  await until(() => requests.some((r) => r.path.endsWith("/sessions")) && sandbox.document && byId.revision.textContent, "session");

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
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((error) => {
  process.stdout.write(JSON.stringify({ error: String(error && error.stack || error) }) + "\n");
  process.exitCode = 1;
});
