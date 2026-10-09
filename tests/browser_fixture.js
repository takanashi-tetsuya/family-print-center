"use strict";

// Runs the actual app.js modules against a small DOM stand-in. No network or print jobs are sent.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

// Node still keeps vm modules behind a flag, so the check runs itself again with it.
if (!vm.SourceTextModule) {
  const { status } = require("node:child_process").spawnSync(process.execPath, ["--experimental-vm-modules", "--disable-warning=ExperimentalWarning", ...process.execArgv, ...process.argv.slice(1)], { stdio: "inherit" });
  process.exit(status ?? 1);
}
if (typeof vm.SourceTextModule.prototype.linkRequests !== "function") throw new Error("These checks need Node.js 24 or newer.");

const entry = path.resolve(__dirname, "../print_center/static/app.js");
const exported = "state, preferences, renderPrinters, refreshPrinters, renderCapabilities, rememberPrintPreferences, openAdvancedSettings, closeAdvancedSettings, applyAdvancedSettings, printModeLabel, updatePrintState, addFiles, removeItem, selectItem, pollUploads, pumpUploads, itemsToPrint, requestPrint, submitPrint".split(", ");

// The entry skips its first refresh, and the functions the checks call are exported from whichever module holds them.
function source(file) {
  let text = fs.readFileSync(file, "utf8");
  if (file === entry) {
    if (!text.includes("\nrefreshAll(true);\n")) throw new Error("app.js no longer starts with refreshAll(true)");
    text = text.replace("\nrefreshAll(true);\n", "\n");
  }
  const declared = [...text.matchAll(/^(export )?(?:async )?(?:function|const) ([\w$]+)/gm)];
  const hidden = declared.filter((match) => !match[1] && exported.includes(match[2])).map((match) => match[2]);
  return hidden.length ? `${text}export { ${hidden.join(", ")} };\n` : text;
}

function load(file, context, modules) {
  if (modules.has(file)) return modules.get(file);
  const module = new vm.SourceTextModule(source(file), { context, identifier: path.relative(process.cwd(), file) });
  modules.set(file, module);
  module.linkRequests(module.moduleRequests.map((request) => load(path.resolve(path.dirname(file), request.specifier), context, modules)));
  return module;
}

function run(context) {
  const modules = new Map();
  const main = load(entry, context, modules);
  main.instantiate();
  main.evaluate().catch(() => {});
  if (main.status === "errored") throw main.error;
  const check = {};
  for (const module of modules.values()) for (const name of exported) if (name in module.namespace) check[name] = module.namespace[name];
  const missing = exported.filter((name) => !(name in check));
  if (missing.length) throw new Error(`not found in app.js modules: ${missing.join(", ")}`);
  return check;
}

// `respond(url, options)` answers fetch calls; without it every call gets `response`.
function environment(cookie = "", protocol = "http:", respond = null) {
  const elements = new Map();
  const cookieWrites = [];
  let storedCookie = cookie;
  class Element {
    constructor(tag = "div", id = "") { this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.parentNode = null; this.dataset = {}; this.attributes = {}; this.events = new Map(); this._value = ""; this.open = false; this.disabled = false; this.classList = { add() {}, remove() {}, toggle() {} }; }
    get value() { return this.tagName === "SELECT" && !this.options.some((option) => option.value === this._value) ? "" : this._value; }
    set value(value) { this._value = String(value); }
    get options() { return this.children.flatMap((child) => child.tagName === "OPTION" ? [child] : child.tagName === "OPTGROUP" ? child.options : []); }
    get firstElementChild() { return this.children[0] || null; }
    append(...children) { for (const child of children) { child.parentNode?.children && child.remove(); child.parentNode = this; this.children.push(child); } }
    replaceChildren(...children) { this.children.forEach((child) => { child.parentNode = null; }); this.children = []; this.append(...children); }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); this.parentNode = null; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key]; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(type, handler) { if (!this.events.has(type)) this.events.set(type, []); this.events.get(type).push(handler); }
    async emit(type) { for (const handler of this.events.get(type) || []) await handler({ target: this, preventDefault() {} }); }
    closest() { return new Element("div"); }
    querySelector(selector) { this.found ??= new Map(); if (!this.found.has(selector)) this.found.set(selector, new Element("span")); return this.found.get(selector); }
    contains() { return false; }
    focus() {}
    showModal() { this.open = true; }
    close() { this.open = false; }
    setCustomValidity(message) { this.validationMessage = message; }
    get willValidate() { return this.tagName === "INPUT" && !this.disabled; }
    get validity() { const n = Number(this.value); return { valid: !this.validationMessage && (!this.required || this.value !== "") && (!this.value || (this.min === undefined || n >= Number(this.min)) && (this.max === undefined || n <= Number(this.max))) }; }
    reportValidity() { return this.id === "advanced-form" ? this.elements.every((element) => !element.willValidate || element.validity.valid) : this.validity.valid; }
    get elements() { return [...document.querySelectorAll("[data-cups-option]"), ...["custom-width", "custom-height", "advanced-media", "advanced-print-mode", "margin-top", "margin-right", "margin-bottom", "margin-left"].map(get)]; }
  }
  const selects = new Set(["sides", "color-mode", "media", "orientation", "advanced-media", "advanced-print-mode", "margin-preset", "preview-file"]);
  const inputs = new Set(["copies", "custom-width", "custom-height", "page-ranges", "preview-page-number", "margin-top", "margin-right", "margin-bottom", "margin-left"]);
  function get(id) { if (!elements.has(id)) elements.set(id, new Element(selects.has(id) ? "select" : inputs.has(id) ? "input" : "div", id)); return elements.get(id); }
  const document = {
    get cookie() { return storedCookie; },
    set cookie(value) { cookieWrites.push(value); storedCookie = /Max-Age=0(?:;|$)/.test(value) ? "" : value.split(";")[0]; },
    getElementById: get,
    querySelector() { return null; },
    querySelectorAll(selector) {
      if (selector !== "[data-cups-option]") return [];
      const found = new Set();
      function visit(node) { if (node.dataset?.cupsOption) found.add(node); (node.children || []).forEach(visit); }
      for (const id of ["advanced-options-paper", "advanced-options-quality", "advanced-options-layout", "advanced-options-device"]) visit(get(id));
      return [...found];
    },
    createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    createTextNode: (text) => ({ textContent: text }), addEventListener() {}, dispatchEvent() {}, hidden: false,
  };
  function Option(text, value) { const element = new Element("option"); element.textContent = text; element.value = value; return element; }
  get("copies").value = "1";
  get("margin-preset").append(...["auto", "normal", "narrow", "custom"].map((value) => new Option(value, value)));
  get("margin-preset").value = "auto";
  get("orientation").append(...["", "portrait", "landscape", "reverse-portrait", "reverse-landscape"].map((value) => new Option(value, value)));
  const response = { printers: [], default: "" };
  const window = { location: { protocol, origin: `${protocol}//example.test` }, setTimeout: () => 1, clearTimeout() {}, setInterval() {}, matchMedia: () => ({ matches: false }), crypto: webcrypto };
  const fetch = respond || (async () => ({ ok: true, json: async () => response }));
  const context = vm.createContext({ window, document, Option, TextEncoder, TextDecoder, Uint8Array, atob, btoa, Headers, AbortController, FormData, URL, Intl, CustomEvent: class {}, console, fetch });
  const check = run(context);
  const toasts = () => get("toasts").children.map((toast) => toast.children[1].textContent);
  return { ...check, get, document, cookieWrites, response, toasts, cookie: () => storedCookie, cups: () => document.querySelectorAll("[data-cups-option]") };
}

module.exports = { environment };
