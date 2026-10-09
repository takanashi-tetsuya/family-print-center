// Entry point: status polling and every event on the page. The parts live in ./app/.
import { $, state, make, showToast, api, emptyState } from "./app/core.js";
import { rememberPrintPreferences } from "./app/preferences.js";
import { basicDriverOptions, relatedOptionKeys, refreshPrinters } from "./app/printers.js";
import { advancedPrintMode, renderAdvancedSummary, selectAdvancedTab, updateCustomMediaFields, openAdvancedSettings, changeAdvancedPrintMode, closeAdvancedSettings, applyAdvancedSettings } from "./app/settings.js";
import { refreshJobs } from "./app/jobs.js";
import { bindPreviewControls, addFiles } from "./app/files.js";
import { setRangeMode, copiesValue, updatePrintState, requestPrint, submitPrint } from "./app/print.js";
import { bindMarginControls, renderMarginFields, renderMarginDiagram } from "./app/margins.js";

function connectionState(healthy) {
  state.healthy = healthy;
  const dot = make("span", healthy ? "status-dot" : "status-dot offline");
  $("connection-state").replaceChildren(dot, document.createTextNode(healthy ? "已連線" : "連線中斷"));
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  $("demo-alert").hidden = !status.demo;
  const alert = $("service-alert");
  let title = "";
  let body = "";
  if (!status.cups?.available) {
    title = "打印服務尚未就緒";
    body = status.cups?.error || "打印服務還在啟動中，請稍候。如果一直出現，請家中管理員檢查。";
  } else if (status.conversion?.office === false) {
    title = "Office 列印服務尚未就緒";
    body = "Word、PowerPoint 和 Excel 需要 LibreOffice 列印服務。安裝完成後即可使用；目前仍可上傳支援的其他格式。";
  }
  $("service-alert-title").textContent = title;
  $("service-alert-body").textContent = body;
  alert.hidden = !title;
  const formats = Array.isArray(status.formats) ? status.formats : [];
  const extensions = formats.map((f) => String(f).toLowerCase().replace(/^\./, "")).filter((f) => /^[a-z0-9]+$/.test(f));
  if (extensions.length) $("file-input").accept = extensions.map((f) => `.${f}`).join(",");
  const limit = Number(status.max_upload_mb);
  $("upload-limit").textContent = Number.isFinite(limit) && limit > 0 ? `每份文件最多 ${limit} MB・依文件格式準備列印` : "文件準備完成後，即可預覽與列印";
  $("upload-limit").title = extensions.length ? `可上傳：${extensions.map((f) => f.toUpperCase()).join("、")}` : "";
  document.querySelectorAll(".format-tags span").forEach((tag) => {
    const unavailable = ["Word", "PPT", "Excel"].includes(tag.textContent) && status.conversion?.office === false;
    tag.classList.toggle("unavailable", unavailable);
    tag.title = unavailable ? "Office 列印服務尚未就緒" : tag.textContent === "電子書" ? "EPUB、FB2 電子書與 CBZ 漫畫" : "";
  });
  updatePrintState();
}

async function refreshAll(initial = false) {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    state.status = await api("/api/status");
    if (state.status.csrf_token) state.csrf = state.status.csrf_token;
    renderStatus();
    connectionState(true);
    const results = await Promise.allSettled([refreshPrinters(), refreshJobs()]);
    for (const [index, result] of results.entries()) {
      if (result.status !== "rejected") continue;
      if (!initial) continue;
      emptyState($(index === 0 ? "printer-list" : "jobs-list"), result.reason.message, index === 0 ? "printer" : "clock");
      if (index === 1) state.jobsSnapshot = "";
    }
  } catch (error) {
    connectionState(false);
    $("service-alert").hidden = false;
    $("service-alert-title").textContent = "暫時無法連線到打印中心";
    $("service-alert-body").textContent = error.message;
    if (initial) {
      emptyState($("printer-list"), "等待設備連線", "printer");
      emptyState($("jobs-list"), "連線恢復後會自動更新", "clock");
      state.jobsSnapshot = "";
      $("upload-limit").textContent = "服務連線恢復後會顯示文件大小限制";
    }
  } finally {
    state.refreshing = false;
  }
}

$("choose-file").addEventListener("click", () => $("file-input").click());
$("add-files").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) => addFiles(event.target.files));
// Files can be dropped on the whole upload panel, also once the list is showing.
const dropTarget = () => $("upload-zone").hidden ? $("upload-panel") : $("upload-zone");
$("upload-panel").addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!state.printing) dropTarget().classList.add("dragging");
});
$("upload-panel").addEventListener("dragleave", (event) => {
  if ($("upload-panel").contains(event.relatedTarget)) return;
  $("upload-zone").classList.remove("dragging");
  $("upload-panel").classList.remove("dragging");
});
$("upload-panel").addEventListener("drop", (event) => {
  event.preventDefault();
  $("upload-zone").classList.remove("dragging");
  $("upload-panel").classList.remove("dragging");
  addFiles(event.dataTransfer.files);
});
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => event.preventDefault());
$("all-pages").addEventListener("click", () => setRangeMode(false));
$("selected-pages").addEventListener("click", () => { setRangeMode(true); $("page-ranges").focus(); });
$("page-ranges").addEventListener("input", () => { $("page-ranges").setCustomValidity(""); updatePrintState(); });
$("copies").addEventListener("input", () => { $("copies").setCustomValidity(""); updatePrintState(); rememberPrintPreferences(); });
$("copies").addEventListener("blur", () => {
  if (!Number.isInteger(copiesValue()) || copiesValue() < 1 || copiesValue() > 99) $("copies").value = String(Math.min(99, Math.max(1, Math.round(copiesValue() || 1))));
  updatePrintState();
  rememberPrintPreferences();
});
$("copies-minus").addEventListener("click", () => { $("copies").value = String(Math.max(1, (copiesValue() || 1) - 1)); $("copies").setCustomValidity(""); updatePrintState(); rememberPrintPreferences(); });
$("copies-plus").addEventListener("click", () => { $("copies").value = String(Math.min(99, (copiesValue() || 1) + 1)); $("copies").setCustomValidity(""); updatePrintState(); rememberPrintPreferences(); });
$("media").addEventListener("change", () => {
  if ($("media").value === "custom") openAdvancedSettings("paper", "custom");
  else state.mediaChoice = $("media").value;
});
for (const [basic, drivers] of Object.entries(basicDriverOptions)) {
  $(basic).addEventListener("change", () => {
    if (basic === "media" && $(basic).value === "custom") return;
    if ($(basic).value) drivers.forEach((key) => { delete state.advancedOptions[key]; });
    renderAdvancedSummary();
    rememberPrintPreferences();
  });
}
$("orientation").addEventListener("change", rememberPrintPreferences);
$("open-advanced-settings").addEventListener("click", () => openAdvancedSettings());
$("open-margin-settings").addEventListener("click", () => openAdvancedSettings("paper"));
bindMarginControls();
for (const id of ["custom-width", "custom-height"]) $(id).addEventListener("input", renderMarginDiagram);
$("advanced-close").addEventListener("click", closeAdvancedSettings);
$("advanced-cancel").addEventListener("click", closeAdvancedSettings);
$("advanced-settings").addEventListener("cancel", () => { $("media").value = state.mediaChoice; state.advancedDrafts = null; });
$("advanced-print-mode")?.addEventListener("change", changeAdvancedPrintMode);
$("advanced-form").addEventListener("submit", applyAdvancedSettings);
$("advanced-media").addEventListener("change", updateCustomMediaFields);
$("advanced-reset").addEventListener("click", () => {
  if ($("advanced-print-mode")) {
    $("advanced-print-mode").value = "auto";
    changeAdvancedPrintMode();
  }
  document.querySelectorAll("[data-cups-option]").forEach((input) => { input.value = ""; });
  $("advanced-media").value = "";
  updateCustomMediaFields();
  renderMarginFields(null);
  $("advanced-error").hidden = true;
});
$("advanced-settings").addEventListener("change", (event) => {
  const key = event.target.dataset.cupsOption;
  if (!key || !event.target.value) return;
  if (basicDriverOptions.media.includes(key)) {
    $("advanced-media").value = "";
    updateCustomMediaFields();
  }
  const selectedKeys = relatedOptionKeys(key, advancedPrintMode());
  document.querySelectorAll("[data-cups-option]").forEach((input) => {
    if (input !== event.target && Array.from(relatedOptionKeys(input.dataset.cupsOption, advancedPrintMode())).some((relatedKey) => selectedKeys.has(relatedKey))) input.value = "";
  });
});
$("advanced-media").addEventListener("change", () => {
  if (!$("advanced-media").value) return;
  document.querySelectorAll("[data-cups-option]").forEach((input) => {
    if (basicDriverOptions.media.includes(input.dataset.cupsOption)) input.value = "";
  });
});
const advancedTabs = Array.from(document.querySelectorAll("[data-settings-tab]"));
advancedTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectAdvancedTab(tab.dataset.settingsTab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? advancedTabs.length - 1 : (index + (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) + advancedTabs.length) % advancedTabs.length;
    selectAdvancedTab(advancedTabs[next].dataset.settingsTab, true);
  });
});
$("print-form").addEventListener("submit", requestPrint);
$("confirmation-cancel").addEventListener("click", () => { state.pendingPrint = null; $("print-confirmation").close(); });
$("print-confirmation").addEventListener("cancel", () => { state.pendingPrint = null; });
$("confirmation-print").addEventListener("click", submitPrint);
$("refresh-jobs").addEventListener("click", async () => {
  const button = $("refresh-jobs");
  button.disabled = true;
  button.classList.add("loading-icon");
  try { await refreshJobs(); }
  catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.classList.remove("loading-icon"); }
});
bindPreviewControls();
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
window.setInterval(() => { if (!document.hidden) refreshAll(); }, 12000);
refreshAll(true);
