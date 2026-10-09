// The file list: uploads, preparation, and the page preview of the chosen file.
import { $, state, make, icon, showToast, api } from "./core.js";
import { setRangeMode, updatePrintState, clearDone } from "./print.js";

function bytesLabel(bytes) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function safePreviewURL(path) {
  if (!path) return null;
  try {
    const url = new URL(path, window.location.origin);
    if (url.origin !== window.location.origin || !/^\/api\/uploads\/[^/]+\/pdf$/.test(url.pathname)) return null;
    return url.href;
  } catch { return null; }
}

function safePagePreviewURL(path) {
  if (!path) return null;
  try {
    const url = new URL(path, window.location.origin);
    if (url.origin !== window.location.origin || !/^\/api\/uploads\/[^/]+\/pages\/[1-9]\d*\/preview\.png$/.test(url.pathname)) return null;
    return url.href;
  } catch { return null; }
}

// Every file in the list shares one set of print settings and is sent one after another.
const maxFiles = 20;

function itemPhase(item) {
  if (item.uploading) return "uploading";
  if (!item.upload) return "waiting";
  if (item.upload.status === "ready" && item.upload.id) return "ready";
  return ["queued", "converting"].includes(item.upload.status) ? "preparing" : "error";
}

export function listStatus() {
  const phases = state.items.map(itemPhase);
  const ready = phases.filter((phase) => phase === "ready").length;
  const failed = phases.filter((phase) => phase === "error").length;
  return { total: phases.length, ready, failed, busy: phases.length - ready - failed };
}

function currentItem() {
  return state.items.find((item) => item.key === state.current) || null;
}

// Page ranges only make sense for a single document.
export function soleUpload() {
  return state.items.length === 1 ? state.items[0].upload : null;
}

// Files already sent in a round that stopped part way are not sent again.
export function itemsToPrint() {
  return state.items.filter((item) => !item.sent);
}

// Office page counts come from the preview and can change with the paper settings.
export function pageTotal(items) {
  if (!items.length || items.some((item) => !Number.isInteger(item.upload?.pages))) return null;
  return { count: items.reduce((total, item) => total + item.upload.pages, 0), approximate: items.some((item) => item.upload.native) };
}

export function renderPreviewPage() {
  const upload = currentItem()?.upload;
  if (!upload || upload.status !== "ready") return;
  const total = Number.isInteger(upload.pages) && upload.pages > 0 ? upload.pages : 1;
  state.previewPage = Math.max(1, Math.min(total, state.previewPage));
  const page = state.previewPage;
  const url = safePagePreviewURL(`/api/uploads/${encodeURIComponent(upload.id)}/pages/${page}/preview.png`);
  $("preview-page-position").textContent = `第 ${page} / ${total} 頁`;
  $("previous-page").disabled = page <= 1;
  $("next-page").disabled = page >= total;
  const image = $("pdf-preview");
  image.alt = `${currentItem().name || "文件"}，第 ${page} 頁`;
  if (url && image.getAttribute("src") !== url) {
    $("preview-loading").hidden = false;
    $("preview-error").hidden = true;
    image.hidden = true;
    image.src = url;
    image.closest(".preview-frame").scrollTop = 0;
  }
}

function renderPreview() {
  const item = currentItem();
  const upload = item?.upload;
  const ready = upload?.status === "ready" && !!upload.id;
  $("native-document").hidden = !ready || !upload.native;
  const pdf = ready ? safePreviewURL(upload.preview_url || (!upload.native ? `/api/uploads/${encodeURIComponent(upload.id)}/pdf` : null)) : null;
  const preview = ready && (upload.pages > 0 || upload.page_preview_url) ? safePagePreviewURL(upload.page_preview_url || `/api/uploads/${encodeURIComponent(upload.id)}/pages/1/preview.png`) : null;
  $("native-description").textContent = preview ? "可以先看 LibreOffice 產生的預覽，實際列印仍使用 Office 原檔。字型與版面由 LibreOffice 處理。" : "Office 原檔已準備完成，目前沒有可用預覽。實際列印由 LibreOffice 處理，頁數與分頁依原檔設定。";
  $("preview-panel").hidden = !preview;
  if (preview) {
    if (state.previewId !== upload.id) {
      state.previewId = upload.id;
      state.previewPage = 1;
    }
    renderPreviewPage();
    $("open-preview").hidden = !pdf;
    if (pdf) $("open-preview").href = pdf;
    else $("open-preview").removeAttribute("href");
    $("page-count").textContent = upload.pages ? `${upload.pages} 頁` : "PDF 預覽";
    const caption = upload.native ? "LibreOffice 預覽 · 列印仍使用原檔；紙張設定可能影響最終分頁。" : "確認版面與頁數後，再開始列印。";
    $("preview-caption").textContent = state.items.length > 1 ? `第 ${state.items.indexOf(item) + 1} / ${state.items.length} 份：${item.name} · ${caption}` : caption;
    $("spreadsheet-hint").hidden = !/\.(xlsx?|ods|csv|tsv)$/i.test(item.name || "");
  } else {
    $("pdf-preview").removeAttribute("src");
    $("pdf-preview").hidden = true;
    $("open-preview").removeAttribute("href");
    state.previewId = null;
    state.previewPage = 1;
  }
}

function fileMeta(item, phase) {
  const parts = [bytesLabel(item.size)];
  if (item.upload?.native) parts.push("Office 原檔");
  if (phase === "waiting") parts.push(item.retryAt ? "排隊中，稍後自動上傳" : "等待上傳");
  else if (phase === "uploading") parts.push("上傳中…");
  else if (phase === "preparing") parts.push(item.upload.status === "queued" ? "等待處理" : "準備中…");
  else if (phase === "ready" && item.upload.pages) parts.push(`${item.upload.native ? "預覽 " : ""}${item.upload.pages} 頁`);
  if (item.sending) parts.push("正在送出…");
  else if (item.jobId && !item.printError) parts.push(`已送出 #${item.jobId}`);
  return parts.join(" · ");
}

// A quiet line when LibreOffice drew some of the document's fonts with others.
function fontNote(item) {
  const fonts = (Array.isArray(item.upload?.substituted_fonts) ? item.upload.substituted_fonts : []).filter((font) => typeof font?.name === "string" && font.name);
  if (!fonts.length) return "";
  const names = fonts.slice(0, 3).map((font) => `『${font.name}』`).join("、");
  const more = fonts.length > 3 ? `等 ${fonts.length} 種字體` : "";
  return `這份文件用了${names}${more}，已用${fonts.every((font) => font.similar) ? "相近" : "其他"}字體代替，排版可能略有不同。`;
}

// Rows are built once and updated in place, so polling never steals focus or replays animations.
function fileRow(item) {
  if (item.row) return item.row;
  const row = make("li", "file-row");
  const pick = make("button", "file-pick");
  pick.type = "button";
  const symbol = make("span", "file-icon");
  const info = make("span", "file-info");
  const name = make("strong", "", item.name);
  name.title = item.name;
  const meta = make("span", "file-meta");
  info.append(name, meta);
  pick.append(symbol, info);
  pick.addEventListener("click", () => selectItem(item));
  const remove = make("button", "icon-button file-remove");
  remove.type = "button";
  remove.setAttribute("aria-label", `移除「${item.name}」`);
  remove.title = "移除文件";
  remove.append(icon("close"));
  remove.addEventListener("click", () => removeItem(item));
  const error = make("p", "file-row-error");
  error.hidden = true;
  const note = make("p", "file-row-note");
  note.hidden = true;
  row.append(pick, remove, error, note);
  item.row = { row, pick, symbol, meta, remove, error, note, symbolName: "" };
  return item.row;
}

export function renderFiles() {
  const list = $("file-list");
  const status = listStatus();
  const many = status.total > 1;
  for (const item of state.items) {
    const parts = fileRow(item);
    const phase = itemPhase(item);
    const current = item.key === state.current;
    const busy = phase === "uploading" || phase === "preparing" || item.sending;
    if (parts.symbolName !== (busy ? "busy" : "file")) {
      parts.symbolName = busy ? "busy" : "file";
      parts.symbol.replaceChildren(busy ? make("span", "spinner") : icon("file"));
    }
    parts.row.classList.toggle("current", many && current);
    parts.row.classList.toggle("failed", phase === "error" || !!item.printError);
    parts.pick.setAttribute("aria-pressed", String(many && current));
    parts.meta.textContent = fileMeta(item, phase);
    // Errors stay on their file until it is removed or sent successfully.
    const error = phase === "error" ? item.upload.error || "文件無法轉換，請檢查文件格式後重新上傳。" : item.printError ? `沒有送出：${item.printError}` : "";
    parts.error.textContent = error;
    parts.error.hidden = !error;
    const note = !error && phase === "ready" ? fontNote(item) : "";
    if (parts.note.textContent !== note) parts.note.textContent = note;
    parts.note.hidden = !note;
    parts.remove.disabled = state.printing || item.uploading;
    if (parts.row.parentNode !== list) list.append(parts.row);
  }
  list.hidden = !status.total;
  list.classList.toggle("many", many);
  $("upload-zone").hidden = status.total > 0;
  $("add-files").hidden = !status.total || status.total >= maxFiles;
  $("add-files").disabled = state.printing;
  $("choose-file").disabled = state.printing;
  $("file-input").disabled = state.printing;
  $("upload-note").textContent = many ? `${status.ready} / ${status.total} 份準備好了` : "可一次放入多份文件";
  $("conversion-progress").hidden = !status.busy;
  if (status.busy) {
    const phase = itemPhase(state.items.find((item) => !["ready", "error"].includes(itemPhase(item))));
    let title = many ? `正在準備文件，已完成 ${status.ready} / ${status.total} 份…` : "正在準備文件…";
    let description = many ? "文件會一份一份處理，好了會自動輪到下一份。" : "正在處理文件版面，請稍候。";
    if (!many && phase === "uploading") title = "正在上傳文件…";
    if (!many && phase === "waiting") [title, description] = ["等待上傳…", "目前等待處理的文件較多，稍後會自動上傳。"];
    if (!many && phase === "preparing" && state.items[0].upload.status === "queued") [title, description] = ["文件已收到，等待處理…", "其他文件處理完成後，會自動輪到這一份。"];
    if (state.pollTrouble) description = "暫時無法取得文件狀態，正在嘗試重新連線…";
    $("conversion-title").textContent = title;
    $("conversion-description").textContent = description;
  }
  renderPreview();
  updatePrintState();
}

function selectItem(item) {
  if (state.current === item.key) return;
  state.current = item.key;
  renderFiles();
}

function stopUploadPolling() {
  if (state.uploadTimer) window.clearTimeout(state.uploadTimer);
  state.uploadTimer = null;
}

// One timer follows every file that is still being prepared.
async function pollUploads() {
  stopUploadPolling();
  if (state.polling) return;
  const pending = state.items.filter((item) => itemPhase(item) === "preparing");
  if (!pending.length) return;
  state.polling = true;
  let trouble = false;
  let finished = false;
  for (const item of pending) {
    try {
      const upload = await api(`/api/uploads/${encodeURIComponent(item.upload.id)}`);
      if (!state.items.includes(item)) continue;
      item.upload = upload;
      if (upload.status === "ready") {
        finished = true;
        if (state.items.length === 1) showToast(upload.native ? (upload.preview_url ? "Office 原檔與預覽已準備完成。確認預覽後，LibreOffice 會使用原檔列印。" : "Office 原檔已準備完成，確認設定後會由 LibreOffice 直接打印。") : `文件已準備完成${upload.pages ? `，共 ${upload.pages} 頁` : ""}。確認預覽後就能列印。`);
      } else if (upload.status === "error") {
        finished = true;
        const message = upload.error || "文件轉換失敗，請換一份文件再試。";
        showToast(state.items.length === 1 ? message : `「${item.name}」無法準備：${message}`, true);
      }
    } catch {
      trouble = true;
    }
  }
  state.polling = false;
  state.pollTrouble = trouble;
  const status = listStatus();
  if (finished && status.total > 1 && !status.busy) {
    showToast(status.failed ? `其餘 ${status.ready} 份已準備好。移除無法使用的文件後就能列印。` : `${status.total} 份文件都準備好了，確認預覽後就能列印。`, !!status.failed);
  }
  renderFiles();
  pumpUploads();
  if (state.items.some((item) => itemPhase(item) === "preparing")) state.uploadTimer = window.setTimeout(pollUploads, trouble ? 5000 : 1400);
}

// Files go up one at a time, and only while the server's conversion queue has room.
function pumpUploads() {
  window.clearTimeout(state.pumpTimer);
  if (state.items.some((item) => item.uploading)) return;
  const next = state.items.find((item) => !item.upload);
  if (!next) return;
  const wait = next.retryAt - Date.now();
  if (wait > 0) {
    state.pumpTimer = window.setTimeout(pumpUploads, wait);
    return;
  }
  const limit = Number(state.status?.max_pending_uploads) || 8;
  if (state.items.filter((item) => itemPhase(item) === "preparing").length >= limit) return;
  sendUpload(next);
}

async function sendUpload(item) {
  item.uploading = true;
  renderFiles();
  const body = new FormData();
  body.append("file", item.file);
  try {
    const upload = await api("/api/uploads", { method: "POST", body, timeout: 180000 });
    item.uploading = false;
    if (!state.items.includes(item)) {
      discardUpload(upload.id);
      return;
    }
    item.upload = { ...upload, name: item.name };
    item.retryAt = 0;
  } catch (error) {
    item.uploading = false;
    if (!state.items.includes(item)) return;
    if (error.status === 429) {
      // The conversion queue is full, often with someone else's files; try the same file again shortly.
      item.retryAt = Date.now() + Math.max(2, error.retryAfter || 5) * 1000;
    } else {
      item.upload = { status: "error", error: error.message };
      showToast(state.items.length === 1 ? error.message : `「${item.name}」無法上傳：${error.message}`, true);
    }
  }
  renderFiles();
  pollUploads();
  pumpUploads();
}

export function addFiles(files) {
  if (state.printing) return;
  const maxMB = Number(state.status?.max_upload_mb);
  const formats = Array.isArray(state.status?.formats) ? state.status.formats.map((format) => String(format).replace(/^\./, "").toLowerCase()) : [];
  const rejected = [];
  const added = [];
  let overflow = 0;
  for (const file of Array.from(files || [])) {
    const extension = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
    let reason = "";
    if (state.items.length >= maxFiles) { overflow += 1; continue; }
    if (Number.isFinite(maxMB) && file.size > maxMB * 1024 * 1024) reason = `超過 ${maxMB} MB，請縮小文件後再上傳。`;
    else if (file.size === 0) reason = "是空的，請選擇其他文件。";
    else if (formats.length && !formats.includes(extension)) reason = "的格式目前不支援，請轉存為 PDF 後再上傳。";
    else if (state.items.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) reason = "已經在清單裡了。";
    if (reason) { rejected.push(`「${file.name}」${reason}`); continue; }
    const item = { key: `file-${++state.nextKey}`, file, name: file.name, size: file.size, lastModified: file.lastModified, upload: null, uploading: false, retryAt: 0, sending: false, sent: false, jobId: null, printError: "", retry: null, row: null };
    state.items.push(item);
    added.push(item);
  }
  $("file-input").value = "";
  if (overflow) rejected.push(`一次最多 ${maxFiles} 份文件，多出來的 ${overflow} 份沒有加入。`);
  if (rejected.length) showToast(rejected.join(" "), true);
  if (!added.length) return;
  clearDone();
  if (!currentItem()) state.current = added[0].key;
  renderFiles();
  pumpUploads();
}

// A file still being prepared cannot be deleted yet; the server also clears old files on its own.
async function discardUpload(id, attempt = 0) {
  try { await api(`/api/uploads/${encodeURIComponent(id)}`, { method: "DELETE" }); }
  catch (error) {
    if (error.status === 400 && attempt < 36) window.setTimeout(() => discardUpload(id, attempt + 1), 5000);
  }
}

function removeItem(item) {
  if (state.printing || item.uploading) return;
  const index = state.items.indexOf(item);
  if (index < 0) return;
  state.items.splice(index, 1);
  item.row?.row.remove();
  if (state.current === item.key) state.current = (state.items[index] || state.items[index - 1])?.key || "";
  if (state.items.every((entry) => entry.sent)) state.items.forEach((entry) => { entry.sent = false; });
  clearDone();
  if (!state.items.length) {
    $("file-input").value = "";
    $("page-ranges").value = "";
    setRangeMode(false);
    state.pollTrouble = false;
  }
  renderFiles();
  if (item.upload?.id) discardUpload(item.upload.id);
  pumpUploads();
}
