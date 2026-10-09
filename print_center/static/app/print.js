// Page ranges, the print button and its summary, confirmation, and sending each file.
import { $, state, make, showToast, api } from "./core.js";
import { sideLabels, colorLabels, orientationLabels, selectedPrinter, availablePrintModes, selectedPrintProfile, printModeLabel, mediaLabel, printOptionDefinitions, optionText, renderPrinters } from "./printers.js";
import { printPresets, activePreset } from "./settings.js";
import { refreshJobs } from "./jobs.js";
import { listStatus, soleUpload, itemsToPrint, pageTotal, renderFiles } from "./files.js";

export function setRangeMode(useRange) {
  state.rangeMode = useRange;
  $("all-pages").classList.toggle("active", !useRange);
  $("selected-pages").classList.toggle("active", useRange);
  $("all-pages").setAttribute("aria-pressed", String(!useRange));
  $("selected-pages").setAttribute("aria-pressed", String(useRange));
  $("range-field").hidden = !useRange;
  $("page-ranges").required = useRange;
  $("page-ranges").setCustomValidity("");
  updatePrintState();
}

function parseRange() {
  const upload = soleUpload();
  if (!state.rangeMode || !upload) return { value: "", pages: upload?.pages || null };
  const value = $("page-ranges").value.replace(/，/g, ",").replace(/[–—]/g, "-").replace(/\s/g, "");
  if (!/^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$/.test(value)) throw new Error("請輸入有效頁碼，例如 1-3, 5, 8。");
  const intervals = value.split(",").map((part) => {
    const [startText, endText] = part.split("-");
    const start = Number(startText);
    const end = Number(endText ?? startText);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) throw new Error("頁碼要從 1 開始，範圍的結束頁不能小於開始頁。");
    if (!upload.native && upload.pages && end > upload.pages) throw new Error(`這份文件只有 ${upload.pages} 頁，請調整指定頁碼。`);
    return [start, end];
  }).sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const interval of intervals) {
    const previous = merged[merged.length - 1];
    if (previous && interval[0] <= previous[1] + 1) previous[1] = Math.max(previous[1], interval[1]);
    else merged.push([...interval]);
  }
  return { value, pages: merged.reduce((total, [start, end]) => total + end - start + 1, 0) };
}

export function copiesValue() {
  return Number($("copies").value);
}

export function updatePrintState() {
  const printer = selectedPrinter();
  const profile = selectedPrintProfile();
  const available = !!state.status?.cups?.available;
  const status = listStatus();
  const many = status.total > 1;
  const queue = itemsToPrint();
  const partial = queue.length < status.total;
  const ready = status.total > 0 && status.ready === status.total;
  const accepting = !!printer && profile?.accepting !== false && Number(profile?.state) !== 5;
  const native = queue.some((item) => item.upload?.native);
  if (many && state.rangeMode) setRangeMode(false);
  $("print-settings").disabled = !printer || state.printing;
  $("open-advanced-settings").disabled = !printer || state.printing;
  $("advanced-fields").disabled = !printer || state.printing;
  $("copies-minus").disabled = copiesValue() <= 1 || state.printing;
  $("copies-plus").disabled = copiesValue() >= 99 || state.printing;
  $("selected-pages").disabled = many;
  $("range-many-hint").hidden = !many;
  $("print-button").disabled = !available || !ready || !accepting || state.printing || !queue.length;
  let label = many ? `列印 ${queue.length} 份文件` : "開始列印";
  if (state.printing) label = many ? `正在送出 ${state.sending?.index || 1} / ${state.sending?.total || queue.length}…` : native ? "正在準備並提交…" : "正在提交…";
  else if (partial) label = `繼續列印剩下 ${queue.length} 份`;
  else if (!many && state.items[0]?.retry) label = "重新確認並提交";
  $("print-button").querySelector("span").textContent = label;
  let summary = "先選擇文件";
  if (status.total && !ready) summary = status.failed ? (many ? `${status.failed} 份文件無法使用，移除後才能列印` : "文件準備失敗，請重新上傳") : "文件正在準備中";
  if (ready) {
    const copies = Number.isInteger(copiesValue()) && copiesValue() > 0 ? copiesValue() : 1;
    if (!many) {
      const upload = state.items[0].upload;
      let pages = upload.pages;
      try { pages = parseRange().pages; } catch { pages = null; }
      summary = `${pages ? `${upload.native && !state.rangeMode ? "預覽 " : ""}${pages} 頁 · ` : upload.native ? "依原檔設定 · " : ""}${copies} 份${state.rangeMode ? " · 指定頁面" : " · 全部頁面"}`;
    } else {
      const pages = pageTotal(queue);
      summary = `${queue.length} 份文件 · ${pages ? `${pages.approximate ? "約 " : ""}${pages.count} 頁 · ` : ""}每份印 ${copies} 份`;
    }
  }
  $("print-summary").textContent = summary;
  let guidance = "文件準備完成並選好打印機後，即可列印。";
  if (state.printing) guidance = many ? "文件會一份一份送到打印機，請稍候。" : native ? "正在準備 Office 文件並交給打印機，較大的文件需要一些時間。" : "正在把文件交給打印機，請稍候。";
  else if (!available) guidance = "等待打印服務就緒。";
  else if (!printer) guidance = "先選擇一台打印機。";
  else if (!accepting) guidance = "這台打印機暫停中，請家中管理員到管理後台按「恢復」。";
  else if (status.failed && many) guidance = "按文件旁的 × 移除無法使用的文件，其他文件就能列印。";
  else if (ready && queue.some((item) => item.printError)) guidance = `沒送出的原因寫在文件下方，處理好再按一次。${partial ? "已送出的文件不會重印。" : ""}`;
  else if (ready) guidance = "按下開始列印後，會先請你確認設定。";
  $("print-guidance").textContent = guidance;
  renderStage(printer, available, ready, accepting, status);
}

// One line at the top says what to do next; the printer figure and the step marks follow it.
function renderStage(printer, available, ready, accepting, status) {
  const many = status.total > 1;
  let stage = "idle";
  let step = "STEP 01 / 03";
  let text = "先放入文件";
  if (state.status && !available) [stage, step, text] = ["offline", "PLEASE WAIT", "打印服務還沒準備好"];
  else if (state.printing) [stage, step, text] = ["printing", "STEP 03 / 03", many && state.sending ? `正在送出 ${state.sending.index} / ${state.sending.total}…` : "正在送到打印機…"];
  else if (state.justPrinted) [stage, step, text] = ["done", "DONE", "已送出，去拿紙吧"];
  else if (status.failed) [stage, step, text] = ["error", "STEP 01 / 03", many ? `${status.failed} 份文件準備失敗，請移除` : "文件準備失敗，請換一份"];
  else if (status.busy) [stage, step, text] = ["preparing", "STEP 01 / 03", many ? `正在準備文件（${status.ready} / ${status.total}）…` : "正在準備文件…"];
  else if (!status.total) [stage, step, text] = ["idle", "STEP 01 / 03", "先放入文件"];
  else if (!printer) [stage, step, text] = ["choose", "STEP 02 / 03", "再選一台打印機"];
  else if (!accepting) [stage, step, text] = ["paused", "STEP 02 / 03", "這台打印機暫停中"];
  else if (state.items.some((item) => item.printError)) [stage, step, text] = ["error", "STEP 03 / 03", many ? "有文件沒送出，看看原因" : "沒有送出，看看原因"];
  else [stage, step, text] = ["ready", "STEP 03 / 03", "可以列印了"];
  const holder = $("workspace-stage");
  if (!holder) return;
  if (holder.dataset.stage !== stage) holder.dataset.stage = stage;
  $("stage-step").textContent = step;
  $("stage-text").textContent = text;
  const printerReady = !!printer && accepting;
  const steps = {
    "upload-title": ready ? "done" : "active",
    "printer-title": printerReady ? "done" : ready ? "active" : "todo",
    "settings-title": state.justPrinted ? "done" : ready && printerReady ? "active" : "todo",
  };
  for (const [heading, value] of Object.entries(steps)) {
    const panel = $(heading)?.closest(".panel");
    if (panel && panel.dataset.step !== value) panel.dataset.step = value;
  }
}

export function clearDone() {
  window.clearTimeout(state.doneTimer);
  state.justPrinted = false;
}

function confirmationRow(label, value) {
  const detail = make("dd");
  for (const line of [value].flat()) detail.append(make("span", "", line));
  $("confirmation-details").append(make("dt", "", label), detail);
}

function requestId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function requestPrint(event) {
  event.preventDefault();
  if ($("print-button").disabled || state.printing || !$("print-form").reportValidity()) return;
  const printer = selectedPrinter();
  const copies = copiesValue();
  if (!Number.isInteger(copies) || copies < 1 || copies > 99) {
    $("copies").setCustomValidity("份數請輸入 1 到 99 的整數。");
    $("copies").reportValidity();
    return;
  }
  let range;
  try { range = parseRange(); }
  catch (error) {
    $("page-ranges").setCustomValidity(error.message);
    $("page-ranges").reportValidity();
    showToast(error.message, true);
    return;
  }
  const settings = {
    printer: printer.name,
    print_mode: state.printMode,
    copies,
    page_ranges: range.value,
    sides: $("sides").value,
    color_mode: $("color-mode").value,
    media: $("media").value,
    orientation: $("orientation").value,
    advanced_options: { ...state.advancedOptions },
  };
  if (settings.media === "custom") settings.custom_media = { ...state.customMedia };
  if (state.margins) settings.margins = { ...state.margins };
  if (settings.margins && itemsToPrint().some((item) => item.upload.native && /\.(ppt|pptx|pptm|pps|ppsx|odp)$/i.test(item.upload.name || item.name))) {
    showToast("PowerPoint 使用原始版面；請將頁邊距設為「自動」，或先另存為 PDF 再上傳。", true);
    return;
  }
  // Each file has its own request number; an unchanged retry reuses it so nothing prints twice.
  const entries = itemsToPrint().map((item) => {
    const payload = { upload_id: item.upload.id, ...settings };
    const signature = JSON.stringify(payload);
    payload.request_id = item.retry?.signature === signature ? item.retry.request_id : requestId();
    return { item, payload };
  });
  const many = entries.length > 1;
  const native = entries.some((entry) => entry.item.upload.native);
  state.pendingPrint = entries;
  $("confirmation-details").replaceChildren();
  $("confirmation-description").textContent = `${native ? "Office 文件會照原檔直接列印，版面可能和 Word、Excel 中略有不同。" : "確認下面的設定，文件就會送到打印機。"}${many ? "多份文件會依序一份一份送出。" : ""}`;
  const names = entries.map((entry) => entry.item.upload.name || entry.item.name);
  confirmationRow("文件", many ? [`${names.length} 份`, ...names.slice(0, 6), ...(names.length > 6 ? [`…另外 ${names.length - 6} 份`] : [])] : names[0]);
  confirmationRow("打印機", printer.label || printer.name);
  if (availablePrintModes().length) confirmationRow("列印方式", printModeLabel());
  const upload = soleUpload();
  const total = pageTotal(entries.map((entry) => entry.item));
  if (upload) confirmationRow("頁面", range.value || `全部頁面${upload.pages ? `（${upload.native ? "預覽 " : ""}${upload.pages} 頁）` : upload.native ? "（依原檔設定）" : ""}`);
  else confirmationRow("頁面", `每份都印全部頁面${total ? `（合計${total.approximate ? "約" : ""} ${total.count} 頁）` : ""}`);
  confirmationRow("份數", many ? `每份文件 ${copies} 份` : `${copies} 份`);
  const preset = activePreset();
  if (preset && printPresets().length > 1) confirmationRow("列印效果", preset.label);
  confirmationRow("基本設定", [sideLabels[settings.sides] || "單雙面自動", colorLabels[settings.color_mode] || "色彩自動", settings.media ? mediaLabel(settings.media) : "紙張自動", settings.orientation ? orientationLabels[settings.orientation] : "方向跟文件一樣"].join(" · "));
  if (settings.margins) confirmationRow("頁邊距", `上 ${settings.margins.top_mm} · 下 ${settings.margins.bottom_mm} · 左 ${settings.margins.left_mm} · 右 ${settings.margins.right_mm} mm`);
  const advanced = printOptionDefinitions().filter((definition) => settings.advanced_options[definition.key]);
  if (!advanced.length) confirmationRow("更多設定", "全部自動");
  for (const definition of advanced) confirmationRow(definition.label || definition.key, optionText(definition, settings.advanced_options[definition.key]));
  $("confirmation-print").disabled = false;
  $("print-confirmation").showModal();
}

// Strictly one file at a time: Office files share one LibreOffice, and the server has few request threads.
export async function submitPrint() {
  if (state.printing || !state.pendingPrint) return;
  const entries = state.pendingPrint;
  state.pendingPrint = null;
  $("confirmation-print").disabled = true;
  $("print-confirmation").close();
  clearDone();
  state.printing = true;
  state.sending = { index: 0, total: entries.length };
  renderPrinters();
  const sent = [];
  let failure = null;
  for (const { item, payload } of entries) {
    if (!state.items.includes(item)) continue;
    state.sending.index += 1;
    item.sending = true;
    renderFiles();
    try {
      const result = await api("/api/print", { method: "POST", body: payload, timeout: item.upload.native || payload.margins ? 210000 : 30000 });
      Object.assign(item, { sending: false, sent: true, jobId: result.job_id, printError: "", retry: null });
      sent.push(result.job_id);
    } catch (error) {
      const { request_id, ...request } = payload;
      Object.assign(item, { sending: false, printError: error.message, retry: { request_id, signature: JSON.stringify(request) } });
      failure = { item, error };
      break;
    }
  }
  state.printing = false;
  state.sending = null;
  if (!failure) state.items.forEach((item) => { item.sent = false; });
  if (sent.length) document.dispatchEvent(new CustomEvent("printcenter:printed", { detail: { jobId: sent[sent.length - 1] } }));
  if (!failure) {
    state.justPrinted = true;
    state.doneTimer = window.setTimeout(() => { state.justPrinted = false; updatePrintState(); }, 6000);
    showToast(sent.length === 1 ? `已送出列印任務 #${sent[0]}，可在任務列表查看進度。` : `已送出 ${sent.length} 份文件，可在任務列表查看進度。`);
  } else if (entries.length === 1 && state.items.length === 1) {
    showToast(`${failure.error.message} 若連線中斷，請先查看任務列表，避免重複列印。`, true);
  } else {
    showToast(`「${failure.item.name}」沒有送出：${failure.error.message} ${sent.length ? `前面 ${sent.length} 份已送出，` : ""}剩下 ${itemsToPrint().length} 份還沒送出。若連線中斷，請先查看任務列表，避免重複列印。`, true);
  }
  renderFiles();
  renderPrinters();
  await refreshJobs().catch(() => {});
}
