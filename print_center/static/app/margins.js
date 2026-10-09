// Page margins are document layout choices, independent of printer driver options.
import { $, state } from "./core.js";
import { mediaDimensions } from "./paper.js";

const sides = ["top", "right", "bottom", "left"];
export function validMargins(value) {
  return !!value && typeof value === "object" && sides.every((side) => typeof value[`${side}_mm`] === "number" && Number.isFinite(value[`${side}_mm`]) && value[`${side}_mm`] >= 0 && value[`${side}_mm`] <= 100);
}

export function marginPreset(value) {
  if (!validMargins(value)) return "auto";
  if (sides.every((side) => value[`${side}_mm`] === 25.4)) return "normal";
  if (sides.every((side) => value[`${side}_mm`] === 12.7)) return "narrow";
  return "custom";
}

export function marginLabel(value) {
  const preset = marginPreset(value);
  return preset === "normal" ? "一般 · 25.4 mm" : preset === "narrow" ? "窄 · 12.7 mm" : preset === "custom" ? `上 ${value.top_mm}／下 ${value.bottom_mm}／左 ${value.left_mm}／右 ${value.right_mm} mm` : "自動";
}

export function renderMarginSummary() {
  $("margin-summary").textContent = marginLabel(state.margins);
}

export function readMarginFields() {
  if ($("margin-preset").value === "auto") return null;
  return Object.fromEntries(sides.map((side) => [side + "_mm", $("margin-" + side).value.trim() === "" ? NaN : Number($("margin-" + side).value)]));
}

export function renderMarginFields(value) {
  const valid = validMargins(value);
  $("margin-preset").value = marginPreset(value);
  sides.forEach((side) => {
    const input = $("margin-" + side);
    input.value = valid ? String(value[side + "_mm"]) : "";
    input.disabled = !valid;
    input.required = valid;
    input.setCustomValidity("");
  });
  renderMarginDiagram();
}

export function changeMarginPreset() {
  const preset = $("margin-preset").value;
  if (preset === "auto") return renderMarginFields(null);
  if (preset === "normal" || preset === "narrow") return renderMarginFields(Object.fromEntries(sides.map((side) => [side + "_mm", preset === "normal" ? 25.4 : 12.7])));
  sides.forEach((side) => {
    const input = $("margin-" + side);
    input.disabled = false;
    input.required = true;
    if (!input.value) input.value = "25.4";
    input.setCustomValidity("");
  });
  renderMarginDiagram();
}

export function renderMarginDiagram() {
  const value = readMarginFields();
  const dimensions = marginPaperDimensions() || { width_mm: 210, height_mm: 297 };
  const content = $("margin-content");
  if (content.style) sides.forEach((side) => { content.style[side] = `${validMargins(value) ? Math.min(48, value[side + "_mm"] / (side === "left" || side === "right" ? dimensions.width_mm : dimensions.height_mm) * 100) : 12}%`; });
  if ($("margin-paper").style) $("margin-paper").style.aspectRatio = String(dimensions.width_mm / dimensions.height_mm);
  $("margin-diagram-label").textContent = `頁面示意 · ${marginPreset(value) === "auto" ? "自動" : "自訂版面"}`;
}

export function marginOrientation() {
  const draft = state.advancedDrafts?.get(state.advancedDraftMode);
  return draft ? draft.orientation || "" : $("orientation").value;
}

export function marginPaperDimensions() {
  const dimensions = mediaDimensions($("advanced-media").value, { width_mm: Number($("custom-width").value), height_mm: Number($("custom-height").value) });
  if (!dimensions) return null;
  const [short, long] = [dimensions.width_mm, dimensions.height_mm].sort((a, b) => a - b);
  return marginOrientation().includes("landscape") ? { width_mm: long, height_mm: short } : { width_mm: short, height_mm: long };
}

export function bindMarginControls() {
  $("margin-preset").addEventListener("change", changeMarginPreset);
  sides.forEach((side) => $("margin-" + side).addEventListener("input", (event) => {
    event.target.setCustomValidity("");
    $("margin-preset").value = "custom";
    renderMarginDiagram();
  }));
}
