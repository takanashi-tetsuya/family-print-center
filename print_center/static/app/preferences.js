// Printing choices remembered per printer and print mode, in one cookie of this browser.
import { $, state } from "./core.js";
import { basicDriverOptions, selectedPrinter, printerCapabilities, printOptionDefinitions } from "./printers.js";
import { copiesValue } from "./print.js";

const preferenceCookie = "fpc_print_preferences";
const preferenceLifetime = 365 * 24 * 60 * 60;
const preferenceCookieLimit = 3800;
const printModes = new Set(["auto", "driver", "driverless"]);
export const preferences = readPreferences();

function preferenceString(value, limit = 256) {
  return typeof value === "string" && value.length <= limit && !/[\u0000-\u001f\u007f]/.test(value);
}

function preferenceCookieAttributes(maxAge = preferenceLifetime) {
  return `; Path=/; Max-Age=${maxAge}; SameSite=Lax${window.location.protocol === "https:" ? "; Secure" : ""}`;
}

function clearPreferenceCookie() {
  try { document.cookie = `${preferenceCookie}=${preferenceCookieAttributes(0)}`; } catch (_) { /* Cookies may be disabled. */ }
}

// This cookie holds printing choices only. Uploaded files, page ranges and session credentials never enter it.
function readPreferences() {
  const empty = { last: "", printers: new Map() };
  try {
    const value = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(`${preferenceCookie}=`))?.slice(preferenceCookie.length + 1);
    if (!value) return empty;
    if (value.length > preferenceCookieLimit || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("Invalid preferences");
    const decoded = atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "="));
    const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(decoded, (character) => character.charCodeAt(0))));
    const now = Math.floor(Date.now() / 1000);
    if (data?.v !== 1 || !Number.isInteger(data.e) || data.e <= now || data.e > now + preferenceLifetime + 300 || !Array.isArray(data.p) || data.p.length > 12) throw new Error("Expired preferences");
    if (preferenceString(data.l, 127)) empty.last = data.l;
    for (const entry of data.p) {
      if (!entry || !preferenceString(entry.n, 127) || !entry.n || !Number.isInteger(entry.t) || entry.t > now + 300 || now - entry.t > preferenceLifetime || !Array.isArray(entry.s) || entry.s.length > 3) continue;
      const modes = new Map();
      for (const saved of entry.s) {
        if (!saved || !printModes.has(saved.m) || !Array.isArray(saved.b) || saved.b.length !== 5 || !saved.b.slice(1).every((value) => preferenceString(value))) continue;
        const advanced = Array.isArray(saved.a) ? saved.a.slice(0, 64).filter((pair) => Array.isArray(pair) && pair.length === 2 && preferenceString(pair[0], 128) && pair[0] && !["__proto__", "constructor", "prototype"].includes(pair[0]) && preferenceString(pair[1]) && pair[1]) : [];
        const custom = Array.isArray(saved.c) && saved.c.length === 2 && saved.c.every((value) => Number.isFinite(value) && value > 0 && value <= 5000) ? { width_mm: saved.c[0], height_mm: saved.c[1] } : null;
        modes.set(saved.m, { copies: Number.isInteger(saved.b[0]) && saved.b[0] >= 1 && saved.b[0] <= 99 ? saved.b[0] : 1, sides: saved.b[1], color: saved.b[2], media: saved.b[3], orientation: saved.b[4], advanced: Object.fromEntries(advanced), custom });
      }
      empty.printers.set(entry.n, { updated: entry.t, mode: printModes.has(entry.m) ? entry.m : "auto", modes });
    }
  } catch (_) { clearPreferenceCookie(); }
  return empty;
}

function writePreferences() {
  try {
    const now = Math.floor(Date.now() / 1000);
    const entries = [...preferences.printers].filter(([, entry]) => now - entry.updated <= preferenceLifetime)
      .sort(([nameA, entryA], [nameB, entryB]) => Number(nameB === preferences.last) - Number(nameA === preferences.last) || entryB.updated - entryA.updated)
      .slice(0, 12).map(([name, entry]) => ({ n: name, t: entry.updated, m: entry.mode, s: [...entry.modes].map(([mode, saved]) => ({ m: mode, b: [saved.copies, saved.sides, saved.color, saved.media, saved.orientation], a: Object.entries(saved.advanced), ...(saved.custom ? { c: [saved.custom.width_mm, saved.custom.height_mm] } : {}) })) }));
    const encode = () => btoa(Array.from(new TextEncoder().encode(JSON.stringify({ v: 1, e: now + preferenceLifetime, l: preferences.last, p: entries })), (byte) => String.fromCharCode(byte)).join("")).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    let encoded = encode();
    // Evict older printers first, then inactive modes. The active choices always take priority.
    while (encoded.length > preferenceCookieLimit && entries.length > 1) { entries.pop(); encoded = encode(); }
    while (encoded.length > preferenceCookieLimit && entries[0]?.s.length > 1) {
      const index = entries[0].s.findIndex((saved) => saved.m !== entries[0].m);
      entries[0].s.splice(index, 1);
      encoded = encode();
    }
    // A large driver must not crowd out the printer, mode, or basic paper and copy choices.
    const active = entries[0]?.s.find((saved) => saved.m === entries[0].m);
    while (encoded.length > preferenceCookieLimit && active?.a.length) {
      active.a.pop();
      encoded = encode();
    }
    if (encoded.length > preferenceCookieLimit) { clearPreferenceCookie(); return; }
    document.cookie = `${preferenceCookie}=${encoded}${preferenceCookieAttributes()}`;
  } catch (_) { /* A privacy setting must not prevent printing. */ }
}

export function rememberPrintPreferences() {
  if (!selectedPrinter()) return;
  const copies = copiesValue();
  if (!Number.isInteger(copies) || copies < 1 || copies > 99) return;
  const entry = preferences.printers.get(state.selectedPrinter) || { modes: new Map() };
  entry.updated = Math.floor(Date.now() / 1000);
  entry.mode = state.printMode;
  entry.modes.set(state.printMode, currentPrintPreferences());
  preferences.printers.set(state.selectedPrinter, entry);
  preferences.last = state.selectedPrinter;
  writePreferences();
}

export function defaultPrintPreferences() {
  return { copies: 1, sides: "", color: "", media: "", orientation: "", advanced: {}, custom: null };
}

export function currentPrintPreferences() {
  return { copies: copiesValue(), sides: $("sides").value, color: $("color-mode").value, media: state.mediaChoice, orientation: $("orientation").value, advanced: { ...state.advancedOptions }, custom: state.customMedia ? { ...state.customMedia } : null };
}

export function validCustomMedia(custom, mode = state.printMode) {
  const bounds = printerCapabilities(mode).custom_media;
  return !!bounds && custom && Number.isFinite(custom.width_mm) && Number.isFinite(custom.height_mm)
    && custom.width_mm >= bounds.min_width && custom.width_mm <= bounds.max_width && custom.height_mm >= bounds.min_height && custom.height_mm <= bounds.max_height;
}

export function restorePreferenceChoices(saved = defaultPrintPreferences()) {
  $("copies").value = String(Number.isInteger(saved.copies) && saved.copies >= 1 && saved.copies <= 99 ? saved.copies : 1);
  for (const [id, value] of [["sides", saved.sides], ["color-mode", saved.color], ["media", saved.media], ["orientation", saved.orientation]]) {
    $(id).value = Array.from($(id).options).some((option) => option.value === value) ? value : "";
  }
  const definitions = printOptionDefinitions();
  state.advancedOptions = Object.fromEntries(Object.entries(saved.advanced || {}).filter(([key, value]) => definitions.some((item) => item.key === key && item.choices.some((choice) => choice.value === value))));
  state.customMedia = validCustomMedia(saved.custom) ? { ...saved.custom } : null;
  if ($("media").value === "custom" && !state.customMedia) $("media").value = "";
  state.mediaChoice = $("media").value;
  for (const [basic, drivers] of Object.entries(basicDriverOptions)) {
    if (!drivers.some((key) => state.advancedOptions[key])) continue;
    $(basic).value = "";
    if (basic === "media") state.mediaChoice = "";
  }
}
