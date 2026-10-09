// The printer list, and what the selected printer and print mode can do.
import { $, state, make, icon, api, emptyState } from "./core.js";
import { preferences, rememberPrintPreferences, defaultPrintPreferences, validCustomMedia, restorePreferenceChoices } from "./preferences.js";
import { renderAdvancedSummary } from "./settings.js";
import { updatePrintState } from "./print.js";

export const sideLabels = {
  "one-sided": "單面列印",
  "two-sided-long-edge": "雙面列印・長邊翻頁",
  "two-sided-short-edge": "雙面列印・短邊翻頁",
};
export const colorLabels = { monochrome: "黑白", color: "彩色", auto: "自動選擇" };
export const orientationLabels = { "": "跟文件一樣", portrait: "直向", landscape: "橫向", "reverse-portrait": "直向（上下顛倒）", "reverse-landscape": "橫向（上下顛倒）" };
export const basicDriverOptions = { sides: ["Duplex"], "color-mode": ["ColorModel"], media: ["PageSize", "PageRegion"] };

export function selectedPrinter() {
  return state.printers.find((printer) => printer.name === state.selectedPrinter) || null;
}

export function availablePrintModes(printer = selectedPrinter()) {
  return Array.isArray(printer?.print_modes) ? printer.print_modes.filter((mode) => mode && ["driver", "driverless"].includes(mode.id) && mode.capabilities && typeof mode.capabilities === "object") : [];
}

export function validPrintMode(mode, printer = selectedPrinter()) {
  return mode === "auto" || availablePrintModes(printer).some((profile) => profile.id === mode);
}

export function selectedPrintProfile(mode = state.printMode) {
  const printer = selectedPrinter();
  const profiles = availablePrintModes(printer);
  return profiles.find((profile) => profile.id === (mode === "auto" ? printer?.default_mode : mode)) || profiles[0] || printer;
}

export function printerCapabilities(mode = state.printMode) {
  return selectedPrintProfile(mode)?.capabilities || {};
}

export function printModeLabel(mode = state.printMode) {
  const profile = availablePrintModes().find((item) => item.id === mode);
  if (mode !== "auto" && typeof profile?.label === "string" && profile.label.trim()) return profile.label;
  if (mode === "driverless") return "免驅動 IPP（相容）";
  if (mode === "driver") return "打印機驅動";
  return selectedPrintProfile(mode)?.id === "driverless" ? "自動（目前使用免驅動 IPP）" : availablePrintModes().some((profile) => profile.id === "driver") ? "自動（優先使用驅動）" : "自動";
}

export function mediaLabel(value) {
  if (value === "custom") return state.customMedia ? `自訂（${state.customMedia.width_mm} × ${state.customMedia.height_mm} mm）` : "自訂尺寸…";
  const text = String(value);
  const lower = text.toLowerCase();
  const dimensions = lower.match(/_([\d.]+)x([\d.]+)(mm|in)$/);
  const name = lower.replace(/^[a-z]+_/, "").replace(/_[\d.]+x[\d.]+(?:mm|in)$/, "").replace(/\.borderless$/, "").replace(/a-([3456])/, "a$1").replace(/b-([56])/, "b$1");
  const names = { a3: "A3", a4: "A4", a5: "A5", a6: "A6", b5: "B5", b6: "B6", c5: "C5 信封", c6: "C6 信封", letter: "Letter", legal: "Legal", executive: "Executive", dl: "DL 信封", monarch: "Monarch 信封", "number-10": "10 號信封", "env-10": "10 號信封", "env-c-6": "C6 信封", "env-dl": "DL 信封", hagaki: "明信片", postcard: "明信片", "photo-l": "L 相片", "16k": "16K", "govt-letter": "8 × 10 吋", "index-4x6": "4 × 6 吋", "index-5x8": "5 × 8 吋", "5x-7": "5 × 7 吋", foolscap: "Foolscap", you4: "洋形 4 號信封", chou3: "長形 3 號信封", chou4: "長形 4 號信封", kaku2: "角形 2 號信封", oufuku: "往返明信片", "l": "L 版相片", "2l": "2L 版相片" };
  let label = names[name] || (/a[0-9]$|b[0-9]$/.test(name) ? name.toUpperCase() : name.startsWith("custom") ? "自訂紙張" : name || text);
  if (dimensions) {
    const round = (number) => dimensions[3] === "in" ? number : String(Math.round(Number(number) * 10) / 10);
    const size = `${round(dimensions[1])} × ${round(dimensions[2])} ${dimensions[3] === "in" ? "吋" : "mm"}`;
    label = /^[\d.\-]+x[\d.\-]+(?:mm|in)?$/.test(name) ? `其他尺寸（${size}）` : label === size ? size : `${label}（${size}）`;
  }
  if (lower.includes("borderless")) label += " · 無邊框";
  return label;
}

export function setOptions(id, values, labels, preferred, fallback) {
  const select = $(id);
  const options = [...new Set(values.filter((value) => typeof value === "string" && value.length > 0))];
  select.replaceChildren(new Option(fallback, ""));
  for (const value of options) select.append(new Option(typeof labels === "function" ? labels(value) : (labels[value] || value), value));
  select.value = options.includes(preferred) ? preferred : "";
  select.disabled = options.length === 0;
}

export function printOptionDefinitions(mode = state.printMode) {
  const definitions = printerCapabilities(mode).print_options;
  return Array.isArray(definitions) ? definitions.filter((item) => item && typeof item.key === "string" && Array.isArray(item.choices) && item.choices.length) : [];
}

export function optionText(definition, value) {
  return definition?.choices.find((choice) => choice.value === value)?.label || value;
}

export function relatedOptionKeys(key, mode = state.printMode) {
  const keys = new Set([key]);
  const mappings = printerCapabilities(mode).option_aliases?.[key] || {};
  Object.values(mappings).forEach((mapping) => Object.keys(mapping).forEach((driverKey) => keys.add(driverKey)));
  return keys;
}

export function renderCapabilities(resetDefaults = false) {
  const printer = selectedPrinter();
  if (!validPrintMode(state.printMode)) resetDefaults = true;
  let saved;
  if (resetDefaults) {
    if ($("advanced-settings").open) $("advanced-settings").close();
    state.advancedDrafts = null;
    const entry = preferences.printers.get(state.selectedPrinter);
    state.printMode = validPrintMode(entry?.mode) ? entry.mode : "auto";
    saved = entry?.modes.get(state.printMode) || defaultPrintPreferences();
    state.advancedOptions = {};
    state.customMedia = null;
    $("orientation").value = "";
  }
  const caps = printerCapabilities();
  const sides = Array.isArray(caps.sides) ? caps.sides : [];
  const colors = Array.isArray(caps.color_modes) ? caps.color_modes : [];
  const media = Array.isArray(caps.media) ? [...caps.media] : [];
  if (caps.custom_media) media.push("custom");
  setOptions("sides", sides, sideLabels, resetDefaults ? "" : $("sides").value, "自動");
  setOptions("color-mode", colors.filter((value) => value !== "auto"), colorLabels, resetDefaults ? "" : $("color-mode").value, "自動");
  setOptions("media", media, mediaLabel, resetDefaults ? "" : $("advanced-settings").open ? state.mediaChoice : $("media").value, "自動");
  state.mediaChoice = $("media").value;
  const definitions = printOptionDefinitions();
  state.advancedOptions = Object.fromEntries(Object.entries(state.advancedOptions).filter(([key, value]) => definitions.some((item) => item.key === key && item.choices.some((choice) => choice.value === value))));
  if (!validCustomMedia(state.customMedia)) {
    state.customMedia = null;
    if (state.mediaChoice === "custom") { state.mediaChoice = ""; $("media").value = ""; }
  }
  if (resetDefaults) restorePreferenceChoices(saved);
  const customOption = Array.from($("media").options).find((option) => option.value === "custom");
  if (customOption) customOption.textContent = mediaLabel("custom");
  $("media-default-hint").textContent = printer ? `自動時用 ${caps.default_media ? mediaLabel(caps.default_media) : "打印機的設定"}；Word、Excel 等會用文件本身的紙張。` : "";
  renderAdvancedSummary();
  $("duplex-hint").hidden = !printer || sides.some((side) => side.startsWith("two-sided"));
  $("color-hint").hidden = !printer || colors.length === 0 || colors.some((color) => color === "color" || color === "auto");
  updatePrintState();
}

export function renderPrinters() {
  const list = $("printer-list");
  list.replaceChildren();
  $("printer-help").hidden = state.printers.length > 0;
  if (!state.printers.length) {
    emptyState(list, state.status?.cups?.available ? "還沒有新增打印機" : "等待打印服務連線");
    updatePrintState();
    return;
  }
  for (const printer of state.printers) {
    const selected = printer.name === state.selectedPrinter;
    const button = make("button", `printer-card${selected ? " selected" : ""}`);
    button.type = "button";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(selected));
    button.disabled = state.printing;
    const symbol = make("span", "printer-symbol");
    symbol.append(icon("printer"));
    const info = make("span", "printer-info");
    info.append(make("span", "printer-name", printer.label || printer.name));
    const meta = make("span", "printer-meta");
    const stopped = Number(printer.state) === 5 || printer.accepting === false;
    const dot = make("span", stopped ? "status-dot offline" : "status-dot");
    meta.append(dot, make("span", stopped ? "status-error" : "", printer.accepting === false ? "暫停接單" : (printer.state_label || "已連線")));
    const reasons = Array.isArray(printer.reasons) ? printer.reasons.filter((reason) => typeof reason === "string").slice(0, 2) : [];
    if (reasons.length) meta.append(make("span", "status-error", reasons.join("、")));
    if (printer.location) meta.append(make("span", "", printer.location));
    if (printer.is_default) meta.append(make("span", "printer-tag", "預設"));
    info.append(meta);
    // The printer's own words when something is wrong, untranslated.
    if ((stopped || reasons.length) && typeof printer.message === "string" && printer.message) info.append(make("span", "printer-message", printer.message));
    // Only a running-low supply is mentioned here; the full levels are on the admin page.
    const low = (Array.isArray(printer.supplies) ? printer.supplies : []).filter((supply) => supply?.low && typeof supply.label === "string" && Number.isFinite(supply.level));
    if (low.length) {
      const supply = make("span", `printer-supply${low.some((item) => item.level <= 0) ? " empty" : ""}`, low.map((item) => `${item.label}${item.level <= 0 ? "已用完" : "快用完了"}`).join("、"));
      supply.title = "打印機上次列印時回報的量，實際可能略有不同";
      info.append(supply);
    }
    button.append(symbol, info, make("span", "printer-selection"));
    button.addEventListener("click", () => {
      if (state.printing || state.selectedPrinter === printer.name) return;
      rememberPrintPreferences();
      state.selectedPrinter = printer.name;
      renderPrinters();
      renderCapabilities(true);
      rememberPrintPreferences();
    });
    button.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"].includes(event.key)) return;
      event.preventDefault();
      const index = state.printers.indexOf(printer);
      const forward = event.key === "ArrowDown" || event.key === "ArrowRight";
      const next = state.printers[(index + (forward ? 1 : -1) + state.printers.length) % state.printers.length];
      if (next.name === state.selectedPrinter) return;
      rememberPrintPreferences();
      state.selectedPrinter = next.name;
      renderPrinters();
      renderCapabilities(true);
      rememberPrintPreferences();
      $("printer-list").querySelector('[aria-checked="true"]')?.focus();
    });
    list.append(button);
  }
  updatePrintState();
}

export async function refreshPrinters() {
  const data = await api("/api/printers");
  const previous = state.selectedPrinter;
  state.printers = Array.isArray(data.printers) ? data.printers : [];
  if (!state.printers.some((printer) => printer.name === previous)) {
    const remembered = !state.printerPreferencesLoaded && state.printers.some((printer) => printer.name === preferences.last) ? preferences.last : "";
    state.selectedPrinter = remembered || state.printers.find((printer) => printer.name === data.default)?.name || state.printers.find((printer) => printer.is_default)?.name || state.printers[0]?.name || "";
  }
  renderPrinters();
  renderCapabilities(previous !== state.selectedPrinter);
  if (!state.printerPreferencesLoaded && state.selectedPrinter) {
    state.printerPreferencesLoaded = true;
    rememberPrintPreferences();
  }
}
