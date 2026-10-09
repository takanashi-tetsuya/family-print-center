// The 更多設定 dialog and the quick print-effect presets.
import { $, state, make } from "./core.js";
import { preferences, rememberPrintPreferences, defaultPrintPreferences, currentPrintPreferences, validCustomMedia, restorePreferenceChoices } from "./preferences.js";
import { basicDriverOptions, selectedPrinter, availablePrintModes, validPrintMode, printerCapabilities, printModeLabel, mediaLabel, printOptionDefinitions, optionText, renderCapabilities } from "./printers.js";
import { updatePrintState } from "./print.js";
import { setPaperOptions, mediaDimensions } from "./paper.js";
import { renderMarginFields, readMarginFields, validMargins, renderMarginSummary, renderMarginDiagram, marginPaperDimensions, marginOrientation } from "./margins.js";

export function advancedPrintMode() {
  const value = $("advanced-print-mode")?.value;
  return validPrintMode(value) ? value : state.printMode;
}

function plain(text) {
  return String(text).replace(/（[^）]*）/g, "");
}

export function renderAdvancedSummary() {
  renderMarginSummary();
  const selections = printOptionDefinitions().filter((item) => state.advancedOptions[item.key]);
  const listed = selections.slice(0, 3).map((item) => `${plain(item.label || item.key)}：${plain(optionText(item, state.advancedOptions[item.key]))}`);
  $("advanced-summary").textContent = selections.length ? `已調整 ${listed.join("、")}${selections.length > 3 ? ` 等 ${selections.length} 項` : ""}` : "紙張種類、清晰度、一張多頁等，都是自動。";
  renderPresets();
}

// Quick modes fill in ordinary settings; every individual choice stays visible and editable.
export function printPresets() {
  const presets = printerCapabilities().presets;
  const definitions = printOptionDefinitions();
  const offered = (key, value) => definitions.some((item) => item.key === key && item.choices.some((choice) => choice.value === value));
  return Array.isArray(presets) ? presets.filter((preset) => preset && typeof preset.id === "string" && typeof preset.label === "string"
    && Object.entries(preset.advanced || {}).every(([key, value]) => offered(key, value))) : [];
}

export function activePreset() {
  const presets = printPresets();
  const keys = new Set(presets.flatMap((preset) => Object.keys(preset.advanced || {})));
  return presets.find((preset) => (preset.basic?.color_mode || "") === $("color-mode").value
    && [...keys].every((key) => (state.advancedOptions[key] || "") === (preset.advanced?.[key] || ""))) || null;
}

function renderPresets() {
  const presets = printPresets();
  const active = activePreset();
  $("preset-field").hidden = presets.length < 2;
  $("preset-buttons").replaceChildren(...presets.map((preset) => {
    const button = make("button", preset === active ? "active" : "", preset.label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(preset === active));
    button.addEventListener("click", () => applyPreset(preset));
    return button;
  }));
  $("preset-hint").textContent = active ? active.summary || "" : "已按你的選擇調整。";
}

function applyPreset(preset) {
  printPresets().forEach((item) => Object.keys(item.advanced || {}).forEach((key) => { delete state.advancedOptions[key]; }));
  Object.assign(state.advancedOptions, preset.advanced || {});
  const color = preset.basic?.color_mode || "";
  $("color-mode").value = Array.from($("color-mode").options).some((option) => option.value === color) ? color : "";
  if (color) basicDriverOptions["color-mode"].forEach((key) => { delete state.advancedOptions[key]; });
  renderAdvancedSummary();
  rememberPrintPreferences();
}

export function selectAdvancedTab(group, focus = false) {
  document.querySelectorAll("[data-settings-tab]").forEach((tab) => {
    const active = tab.dataset.settingsTab === group;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== group; });
}

export function updateCustomMediaFields() {
  const bounds = printerCapabilities(advancedPrintMode()).custom_media;
  const active = !!bounds && $("advanced-media").value === "custom";
  const size = mediaDimensions($("advanced-media").value);
  $("custom-media-fields").hidden = false;
  for (const [id, minimum, maximum, preferred] of [["custom-width", bounds?.min_width, bounds?.max_width, 210], ["custom-height", bounds?.min_height, bounds?.max_height, 297]]) {
    const input = $(id);
    input.disabled = !active;
    input.readOnly = !active;
    input.required = active;
    if (!active) { input.value = size ? String(id === "custom-width" ? size.width_mm : size.height_mm) : ""; continue; }
    input.min = String(minimum);
    input.max = String(maximum);
    if (!input.value) input.value = String(Math.max(minimum, Math.min(maximum, preferred)));
  }
  $("custom-media-hint").textContent = active ? `可用範圍：寬 ${bounds.min_width}–${bounds.max_width} mm，高 ${bounds.min_height}–${bounds.max_height} mm。` : size ? "尺寸依所選紙張；需要不同尺寸時，選擇「自訂尺寸」。" : "自動會沿用文件與打印機的紙張設定。";
  renderMarginDiagram();
}

export function openAdvancedSettings(group = "paper", requestedMedia = null) {
  const printer = selectedPrinter();
  if (!printer || state.printing) return;
  $("advanced-printer-name").textContent = printer.label || printer.name;
  $("advanced-error").hidden = true;
  state.advancedDrafts = new Map(preferences.printers.get(state.selectedPrinter)?.modes || []);
  const draft = currentPrintPreferences();
  if (requestedMedia === "custom") {
    const bounds = printerCapabilities().custom_media;
    if (bounds) {
      draft.media = "custom";
      draft.custom = validCustomMedia(draft.custom) ? draft.custom : { width_mm: Math.max(bounds.min_width, Math.min(bounds.max_width, 210)), height_mm: Math.max(bounds.min_height, Math.min(bounds.max_height, 297)) };
    }
  }
  state.advancedDrafts.set(state.printMode, draft);
  state.advancedDraftMode = state.printMode;
  const modeSelect = $("advanced-print-mode");
  if (modeSelect) {
    modeSelect.replaceChildren(new Option(printModeLabel("auto"), "auto"), ...availablePrintModes().map((profile) => new Option(printModeLabel(profile.id), profile.id)));
    modeSelect.value = state.printMode;
    modeSelect.disabled = availablePrintModes().length === 0;
  }
  renderAdvancedFields(state.printMode, draft);
  selectAdvancedTab(group);
  $("advanced-tabs").setAttribute("aria-orientation", window.matchMedia("(max-width: 600px)").matches ? "horizontal" : "vertical");
  $("advanced-fields").disabled = false;
  $("advanced-settings").showModal();
  selectAdvancedTab(group, true);
}

function renderAdvancedFields(mode, draft) {
  const caps = printerCapabilities(mode);
  const media = Array.isArray(caps.media) ? [...caps.media] : [];
  if (caps.custom_media) media.push("custom");
  setPaperOptions("advanced-media", media, draft.media, draft.custom);
  if ($("advanced-media").value === "custom" && !validCustomMedia(draft.custom, mode)) $("advanced-media").value = "";
  $("custom-width").value = validCustomMedia(draft.custom, mode) ? String(draft.custom.width_mm) : "";
  $("custom-height").value = validCustomMedia(draft.custom, mode) ? String(draft.custom.height_mm) : "";
  updateCustomMediaFields();
  renderMarginFields(draft.margins);
  const containers = Object.fromEntries(["paper", "quality", "layout", "device"].map((name) => [name, $("advanced-options-" + name)]));
  Object.values(containers).forEach((container) => container.replaceChildren());
  printOptionDefinitions(mode).forEach((definition, index) => {
    // A supported primary paper selector replaces duplicate driver page-size lists.
    if (caps.media?.length && basicDriverOptions.media.includes(definition.key) && !draft.advanced?.[definition.key]) return;
    if (definition.key === "finishings" && definition.choices.every((choice) => choice.value === "3")) return;
    // A single choice that is already the printer's default is not a real choice.
    if (definition.choices.length === 1 && definition.choices[0].value === definition.default) return;
    const container = containers[definition.group] || containers.device;
    const field = make("div", "settings-group");
    const label = make("label", "", definition.label || definition.key);
    const select = make("select");
    select.id = "advanced-option-" + index;
    select.dataset.cupsOption = definition.key;
    label.htmlFor = select.id;
    select.append(new Option("自動", ""));
    for (const choice of definition.choices) select.append(new Option(choice.label || choice.value, choice.value));
    const value = draft.advanced?.[definition.key] || "";
    select.value = definition.choices.some((choice) => choice.value === value) ? value : "";
    field.append(label, select);
    if (definition.default) field.append(make("p", "field-hint", "打印機預設：" + optionText(definition, definition.default)));
    container.append(field);
  });
  Object.entries(containers).forEach(([name, container]) => {
    if (!container.children.length && name !== "paper") container.append(make("p", "advanced-empty", name === "device" ? "這台打印機沒有其他專用選項。" : "這台打印機在這裡沒有可以調整的項目。"));
  });
  if ($("advanced-print-mode-hint")) $("advanced-print-mode-hint").textContent = mode === "driverless" ? "遇到驅動不相容時，可改用免驅動 IPP；可用設定可能不同。" : availablePrintModes().some((profile) => profile.id === "driver") ? "自動會優先使用已安裝的打印機驅動；你的選擇會在這個瀏覽器中記住。" : "目前使用免驅動 IPP；你的選擇會在這個瀏覽器中記住。";
}

function advancedDraftChoices() {
  const mode = state.advancedDraftMode;
  const draft = state.advancedDrafts?.get(mode) || defaultPrintPreferences();
  const advanced = {};
  document.querySelectorAll("[data-cups-option]").forEach((input) => { if (input.value) advanced[input.dataset.cupsOption] = input.value; });
  const media = $("advanced-media").value;
  return { ...draft, media, advanced, custom: media === "custom" ? { width_mm: Number($("custom-width").value), height_mm: Number($("custom-height").value) } : draft.custom, margins: readMarginFields() };
}

export function changeAdvancedPrintMode() {
  const mode = $("advanced-print-mode")?.value || state.printMode;
  if (!state.advancedDrafts || !validPrintMode(mode) || mode === state.advancedDraftMode) return;
  state.advancedDrafts.set(state.advancedDraftMode, advancedDraftChoices());
  state.advancedDraftMode = mode;
  const draft = state.advancedDrafts.get(mode) || defaultPrintPreferences();
  state.advancedDrafts.set(mode, draft);
  renderAdvancedFields(mode, draft);
  $("advanced-error").hidden = true;
}

export function closeAdvancedSettings() {
  $("media").value = state.mediaChoice;
  state.advancedDrafts = null;
  $("advanced-settings").close();
}

export function applyAdvancedSettings(event) {
  event.preventDefault();
  const margins = readMarginFields();
  if (margins !== null && !validMargins(margins)) {
    selectAdvancedTab("paper");
    $("advanced-error").textContent = "四邊頁邊距請輸入 0 到 100 mm 的數值。";
    $("advanced-error").hidden = false;
    return;
  }
  const paper = marginPaperDimensions();
  const fits = (width, height) => width - margins.left_mm - margins.right_mm >= 1 && height - margins.top_mm - margins.bottom_mm >= 1;
  const automaticOrientation = !marginOrientation() || marginOrientation() === "auto";
  if (paper && margins && !fits(paper.width_mm, paper.height_mm) && !(automaticOrientation && fits(paper.height_mm, paper.width_mm))) {
    selectAdvancedTab("paper");
    $("advanced-error").textContent = "頁邊距超過紙張大小，請保留至少 1 mm 寬、高的內容區域。";
    $("advanced-error").hidden = false;
    return;
  }
  const invalid = Array.from($("advanced-form").elements).find((input) => input.willValidate && !input.validity.valid);
  if (invalid) {
    const panel = invalid.closest("[data-settings-panel]");
    if (panel) selectAdvancedTab(panel.dataset.settingsPanel);
    invalid.reportValidity();
    return;
  }
  if (!$("advanced-form").reportValidity()) return;
  const mode = $("advanced-print-mode")?.value || state.printMode;
  const definitions = printOptionDefinitions(mode);
  const staleOption = Array.from(document.querySelectorAll("[data-cups-option]")).some((input) => input.value && !definitions.some((item) => item.key === input.dataset.cupsOption && item.choices.some((choice) => choice.value === input.value)));
  const media = $("advanced-media").value;
  const caps = printerCapabilities(mode);
  if (!selectedPrinter() || !validPrintMode(mode) || staleOption || (media && media !== "custom" && !(caps.media || []).includes(media)) || (media === "custom" && !validCustomMedia({ width_mm: Number($("custom-width").value), height_mm: Number($("custom-height").value) }, mode))) {
    $("advanced-error").textContent = "打印機的可用設定剛剛更新了，請關閉後重新打開「更多設定」。";
    $("advanced-error").hidden = false;
    return;
  }
  const draft = advancedDraftChoices();
  const entry = preferences.printers.get(state.selectedPrinter) || { modes: new Map() };
  entry.modes.set(state.printMode, currentPrintPreferences());
  preferences.printers.set(state.selectedPrinter, entry);
  state.printMode = mode;
  renderCapabilities();
  restorePreferenceChoices(draft);
  const customOption = Array.from($("media").options).find((option) => option.value === "custom");
  if (customOption) customOption.textContent = mediaLabel("custom");
  renderAdvancedSummary();
  updatePrintState();
  rememberPrintPreferences();
  state.advancedDrafts = null;
  $("advanced-settings").close();
}
