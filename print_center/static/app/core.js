// Shared page state, DOM helpers and the JSON API client.
export const $ = (id) => document.getElementById(id);
export const state = {
  csrf: document.querySelector('meta[name="csrf-token"]')?.content || "",
  status: null,
  printers: [],
  selectedPrinter: "",
  items: [],
  current: "",
  nextKey: 0,
  uploadTimer: null,
  pumpTimer: 0,
  polling: false,
  pollTrouble: false,
  previewPage: 1,
  previewId: null,
  printing: false,
  sending: null,
  justPrinted: false,
  doneTimer: 0,
  rangeMode: false,
  pendingPrint: null,
  refreshing: false,
  healthy: false,
  advancedOptions: {},
  customMedia: null,
  margins: null,
  mediaChoice: "",
  printMode: "auto",
  printerPreferencesLoaded: false,
  advancedDraftMode: "auto",
  advancedDrafts: null,
  jobsSnapshot: "",
  jobsTimer: 0,
};

export function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${name}`);
  svg.append(use);
  return svg;
}

export function showToast(message, error = false) {
  const toast = make("div", error ? "toast error" : "toast");
  toast.append(icon(error ? "info" : "check"), make("span", "", message));
  const close = make("button");
  close.type = "button";
  close.setAttribute("aria-label", "關閉提示");
  close.append(icon("close"));
  close.addEventListener("click", () => toast.remove());
  toast.append(close);
  $("toasts").append(toast);
  while ($("toasts").children.length > 4) $("toasts").firstElementChild.remove();
  window.setTimeout(() => toast.remove(), error ? 10000 : 6500);
}

export async function api(path, options = {}) {
  const { timeout = 15000, ...request } = options;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  const headers = new Headers(request.headers || {});
  headers.set("Accept", "application/json");
  if (request.method && request.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
  if (request.body && !(request.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
    request.body = JSON.stringify(request.body);
  }
  try {
    const response = await fetch(path, { ...request, headers, credentials: "same-origin", signal: controller.signal, cache: "no-store" });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data?.error || data?.message || data?.detail;
      const error = new Error(typeof detail === "string" ? detail : `操作未完成（${response.status}），請稍後再試。`);
      error.status = response.status;
      error.retryAfter = Number(response.headers?.get("Retry-After")) || 0;
      throw error;
    }
    if (!data) throw new Error("服務回應格式異常，請重新整理頁面。");
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("連線逾時，請檢查設備是否在線，再試一次。");
    if (error instanceof TypeError) throw new Error("無法連線到打印中心，請確認家中網路連線。");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

export function emptyState(parent, message, symbol = "printer") {
  const empty = make("div", "empty-state");
  empty.append(icon(symbol), make("p", "", message));
  parent.replaceChildren(empty);
}
