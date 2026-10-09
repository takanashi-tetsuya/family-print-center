"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const views = ["status", "printers", "add", "jobs", "fonts", "system"];
  const state = {
    csrf: document.querySelector('meta[name="csrf-token"]')?.content || "",
    session: null,
    printers: [],
    scan: null,
    scanTimer: null,
    dialog: null,
    searchTimer: null,
    searchRun: 0,
  };
  const connectionKinds = { ipp: "網路", ipps: "網路（加密）", legacy: "網路", network: "網路", usb: "USB 連接線" };

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(text, className, onClick) {
    const node = make("button", `btn${className ? ` ${className}` : ""}`, text);
    node.type = "button";
    if (onClick) node.addEventListener("click", () => run(node, onClick));
    return node;
  }

  async function run(control, task) {
    if (control.disabled) return;
    control.disabled = true;
    try { await task(); }
    catch (error) { notice(error.message, "error"); }
    finally { if (control.isConnected) control.disabled = false; }
  }

  function notice(message, kind = "success") {
    const box = make("div", `alert-message ${kind}`, message);
    box.setAttribute("role", kind === "error" ? "alert" : "status");
    const close = make("button", "close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "關閉訊息");
    close.addEventListener("click", () => box.remove());
    box.append(close);
    $("notices").append(box);
    while ($("notices").children.length > 3) $("notices").firstElementChild.remove();
    window.setTimeout(() => box.remove(), kind === "error" ? 12000 : 6000);
  }

  function label(text, kind) {
    return make("span", `label${kind ? ` ${kind}` : ""}`, text);
  }

  function table(columns, rows, emptyText) {
    if (!rows.length) return make("p", "lu-empty", emptyText);
    const node = make("table", "table lu-responsive");
    const head = make("thead");
    const headRow = make("tr");
    columns.forEach((column) => headRow.append(make("th", "", column)));
    head.append(headRow);
    const body = make("tbody");
    for (const cells of rows) {
      const row = make("tr");
      cells.forEach((cell, index) => {
        const td = make("td", index === cells.length - 1 && cell instanceof Node && cell.dataset?.actions ? "actions" : "");
        if (columns[index]) td.dataset.title = columns[index];
        if (cell instanceof Node) td.append(cell);
        else td.textContent = cell ?? "";
        row.append(td);
      });
      body.append(row);
    }
    node.append(head, body);
    return node;
  }

  function stack(main, sub, strong = true) {
    const box = make("div");
    box.append(make(strong ? "strong" : "span", "", main));
    if (sub) box.append(make("span", "sub", sub));
    return box;
  }

  function actions(...buttons) {
    const box = make("div");
    box.dataset.actions = "1";
    box.append(...buttons.filter(Boolean));
    return box;
  }

  function when(seconds) {
    if (!seconds) return "";
    return new Date(seconds * 1000).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  async function api(path, { method = "GET", body, timeout = 20000 } = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeout);
    const headers = new Headers({ Accept: "application/json" });
    if (method !== "GET") headers.set("X-CSRF-Token", state.csrf);
    let payload = body;
    if (body && !(body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
      payload = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, { method, headers, body: payload, credentials: "same-origin", signal: controller.signal });
    } catch (error) {
      throw new Error(error.name === "AbortError" ? "等候太久沒有回應，請稍後再試。" : "連不上打印中心，請確認網路。");
    } finally {
      window.clearTimeout(timer);
    }
    const data = await response.json().catch(() => ({}));
    if (data.csrf_token) state.csrf = data.csrf_token;
    if (response.status === 401 && data.login) {
      state.session = { logged_in: false };
      showLogin();
      throw new Error(data.error || "請重新登入。");
    }
    if (response.status === 403 && data.must_change) {
      state.session.must_change = true;
      location.hash = "#system";
    }
    if (!response.ok) throw new Error(data.error || "操作沒有完成，請稍後再試。");
    return data;
  }

  function confirmAction(title, body, okText = "確定") {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-ok").textContent = okText;
    dialog.returnValue = "";
    dialog.showModal();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    });
  }

  /* ---------- Session and navigation ---------- */

  function showOnly(id) {
    document.querySelectorAll(".lu-view").forEach((view) => { view.hidden = view.id !== id; });
  }

  function showLogin() {
    window.clearTimeout(state.scanTimer);
    $("admin-menu").hidden = true;
    $("logout").hidden = true;
    $("login-hint").hidden = !state.session?.initial_password;
    showOnly("view-login");
    $("login-password").focus();
  }

  function currentView() {
    const name = location.hash.slice(1);
    return views.includes(name) ? name : "status";
  }

  function route() {
    if (!state.session?.logged_in) return showLogin();
    $("admin-menu").hidden = false;
    $("logout").hidden = false;
    let view = currentView();
    if (state.session.must_change && view !== "system") {
      history.replaceState(null, "", "#system");
      view = "system";
    }
    document.querySelectorAll("#admin-menu a").forEach((link) => {
      link.classList.toggle("active", link.dataset.view === view);
      if (link.dataset.view === view) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    showOnly(`view-${view}`);
    document.title = `${document.querySelector(`#admin-menu a[data-view="${view}"]`).textContent} · 管理後台`;
    const loaders = { status: loadStatus, printers: loadPrinters, add: loadAdd, jobs: loadJobs, fonts: loadFonts, system: loadSystem };
    loaders[view]().catch((error) => notice(error.message, "error"));
  }

  async function start() {
    try {
      state.session = await api("/api/admin/session");
    } catch (error) {
      showOnly("view-login");
      notice(error.message, "error");
      return;
    }
    if (!state.session.configured) notice("管理員設定檔損壞。請依「忘記密碼」的方式重設。", "error");
    route();
  }

  /* ---------- Printers ---------- */

  // Levels are what the printer reported during its last job, so the time goes with them.
  function supplyList(printer) {
    const supplies = Array.isArray(printer.supplies) ? printer.supplies : [];
    if (!supplies.length) return null;
    const box = make("div", "supplies");
    for (const supply of supplies) {
      const level = Math.max(0, Math.min(100, Number(supply.level) || 0));
      const row = make("div", `supply${supply.low ? " low" : ""}`);
      row.title = supply.name || supply.label;
      const bar = make("span", "supply-bar");
      const fill = make("span", "supply-fill");
      fill.style.width = `${level}%`;
      if (/^#[0-9A-F]{6}$/.test(supply.color || "")) fill.style.backgroundColor = supply.color;
      bar.append(fill);
      row.append(make("span", "supply-name", supply.label), bar, make("span", "supply-level", `${level}%`));
      box.append(row);
    }
    if (printer.supplies_time) box.append(make("span", "sub", `打印機在 ${when(printer.supplies_time)} 回報`));
    return box;
  }

  function stateBadges(printer) {
    const box = make("div");
    if (!printer.accepting) box.append(label("不接受任務", "important"));
    else if (printer.state === 5) box.append(label("已暫停", "warning"));
    else if (printer.state === 4) box.append(label("列印中", "notice"));
    else box.append(label("待命", "success"));
    for (const reason of printer.reasons || []) box.append(" ", label(reason, "warning"));
    if (printer.message) box.append(make("span", "sub", printer.message));
    const supplies = supplyList(printer);
    if (supplies) box.append(supplies);
    return box;
  }

  async function refreshPrinters() {
    const data = await api("/api/admin/printers");
    state.printers = data.printers || [];
    return state.printers;
  }

  async function loadStatus() {
    const data = await api("/api/admin/overview");
    state.printers = data.printers || [];
    const rows = $("status-table");
    rows.replaceChildren();
    const defaultPrinter = state.printers.find((printer) => printer.is_default);
    const entries = [
      ["打印服務", data.cups.available ? label("正常運作", "success") : stack("無法連線", data.cups.error, false)],
      ["已加入的打印機", `${state.printers.length} 台`],
      ["預設打印機", defaultPrinter ? defaultPrinter.info || defaultPrinter.name : "尚未設定"],
      ["等待或正在列印的任務", `${data.active_jobs} 件`],
    ];
    const body = make("tbody");
    for (const [key, value] of entries) {
      const row = make("tr");
      const th = make("th", "", key);
      const td = make("td");
      td.append(value instanceof Node ? value : document.createTextNode(value));
      row.append(th, td);
      body.append(row);
    }
    rows.append(body);
    const list = $("status-printers");
    list.replaceChildren(table(["打印機", "狀態", "預設"], state.printers.map((printer) => [
      stack(printer.info || printer.name, [printer.location, printer.make_model].filter(Boolean).join(" · ")),
      stateBadges(printer),
      printer.is_default ? label("預設", "notice") : "",
    ]), "還沒有加入任何打印機。"));
    if (!state.printers.length) {
      const link = make("a", "btn cbi-button-add", "新增第一台打印機");
      link.href = "#add";
      list.append(link);
    }
  }

  async function printerAction(printer, action, success) {
    const result = await api(`/api/admin/printers/${encodeURIComponent(printer.name)}/${action}`, { method: "POST", timeout: 30000 });
    notice(typeof success === "function" ? success(result) : success);
    await loadPrinters();
  }

  async function loadPrinters() {
    await refreshPrinters();
    const rows = state.printers.map((printer) => [
      stack(printer.info || printer.name, [`代號 ${printer.name}`, printer.location].filter(Boolean).join(" · ")),
      stack(printer.connection, printer.uri, false),
      printer.make_model || "—",
      stateBadges(printer),
      actions(
        printer.is_default ? label("預設", "notice") : button("設為預設", "", () => printerAction(printer, "default", `已將「${printer.info || printer.name}」設為預設打印機。`)),
        button("測試頁", "", () => printerAction(printer, "test", (result) => `已送出測試頁（任務 ${result.job_id}）。`)),
        printer.state === 5 || !printer.accepting
          ? button("恢復", "cbi-button-positive", () => printerAction(printer, "resume", "打印機已恢復。"))
          : button("暫停", "", () => printerAction(printer, "pause", "打印機已暫停，新任務會先排隊。")),
        button("清空任務", "", async () => {
          if (!await confirmAction("清空任務", `要取消「${printer.info || printer.name}」所有尚未完成的任務嗎？`, "全部取消")) return;
          await printerAction(printer, "clear", "已取消這台打印機的所有待印任務。");
        }),
        button("編輯", "cbi-button-action", () => openEdit(printer)),
        button("刪除", "cbi-button-remove", async () => {
          if (!await confirmAction("刪除打印機", `確定要刪除「${printer.info || printer.name}」嗎？家人將不能再選擇這台打印機。`, "刪除")) return;
          await api(`/api/admin/printers/${encodeURIComponent(printer.name)}`, { method: "DELETE" });
          notice("打印機已刪除。");
          await loadPrinters();
        }),
      ),
    ]);
    $("printers-table").replaceChildren(table(["打印機", "連線", "驅動程式", "狀態", ""], rows, "還沒有加入任何打印機。"));
  }

  /* ---------- Add / edit dialog ---------- */

  function driverOption(driver, index) {
    const suffix = driver.id === "everywhere" ? "" : driver.match === "exact" ? index === 0 ? "（建議）" : "（相符）"
      : driver.match === "similar" ? "（相近型號）" : driver.match === "generic" ? "" : "";
    return new Option(`${driver.label}${suffix}`, driver.id);
  }

  async function uploadedDrivers() {
    try { return (await api("/api/admin/ppds")).uploads || []; }
    catch { return []; }
  }

  async function fillDriverSelect(candidates, { keepCurrent = "" } = {}) {
    const select = $("printer-driver");
    select.replaceChildren();
    if (keepCurrent) select.append(new Option(`維持目前的驅動（${keepCurrent}）`, ""));
    candidates.forEach((driver, index) => select.append(driverOption(driver, index)));
    for (const upload of await uploadedDrivers()) select.append(new Option(`已上傳：${upload.label}`, upload.id));
    select.append(new Option("搜尋其他驅動…", "__search"));
    select.value = keepCurrent ? "" : candidates.length ? candidates[0].id : "__search";
    driverChanged();
  }

  function driverChanged() {
    const select = $("printer-driver");
    const value = select.value;
    $("driver-search").hidden = value !== "__search";
    const note = $("printer-driver-note");
    const chosen = state.dialog?.candidates?.find((driver) => driver.id === value);
    if (value === "everywhere") note.textContent = "打印機自己提供列印功能，不需要額外驅動，通常是最穩定的選擇。";
    else if (value === "__search") note.textContent = "輸入品牌與型號搜尋已安裝的驅動，再從清單中點選。";
    else if (value.startsWith("upload:")) note.textContent = "使用你上傳的驅動檔。";
    else if (chosen?.match === "similar") note.textContent = "沒有完全相同的型號，這是最接近的驅動。新增後請列印測試頁確認。";
    else if (chosen?.match === "generic") note.textContent = "通用驅動可以列印，但進階選項（例如紙匣）可能較少。";
    else if (chosen) note.textContent = "依打印機回報的型號自動選出。";
    else note.textContent = "";
    if (value === "__search") window.setTimeout(() => $("driver-query").focus(), 0);
  }

  function searchDrivers() {
    window.clearTimeout(state.searchTimer);
    const query = $("driver-query").value.trim();
    const results = $("driver-results");
    if (query.length < 2) {
      results.replaceChildren();
      $("driver-search-note").textContent = "至少輸入 2 個字。";
      return;
    }
    $("driver-search-note").textContent = "搜尋中…（第一次可能需要幾秒）";
    state.searchTimer = window.setTimeout(async () => {
      const ticket = ++state.searchRun;
      try {
        const data = await api(`/api/admin/drivers?q=${encodeURIComponent(query)}`, { timeout: 40000 });
        if (ticket !== state.searchRun) return;
        results.replaceChildren(...data.drivers.map((driver) => new Option(driver.label, driver.id)));
        $("driver-search-note").textContent = data.drivers.length
          ? `找到 ${data.drivers.length >= 60 ? "60 個以上" : `${data.drivers.length} 個`}驅動，點選一個使用。`
          : "找不到相符的驅動。可以換個關鍵字（例如只輸入型號數字），或上傳廠商的驅動檔。";
      } catch (error) {
        if (ticket === state.searchRun) $("driver-search-note").textContent = error.message;
      }
    }, 300);
  }

  function pickSearchResult() {
    const option = $("driver-results").selectedOptions[0];
    if (!option) return;
    const select = $("printer-driver");
    let existing = [...select.options].find((item) => item.value === option.value);
    if (!existing) {
      existing = new Option(option.textContent, option.value);
      select.insertBefore(existing, select.options[select.options.length - 1]);
    }
    select.value = option.value;
    driverChanged();
  }

  function dialogError(message) {
    $("printer-dialog-error").textContent = message || "";
    $("printer-dialog-error").hidden = !message;
  }

  async function openAdd(context) {
    state.dialog = { mode: "add", ...context };
    $("printer-dialog-title").textContent = "新增打印機";
    $("printer-dialog-source").textContent = context.description;
    $("printer-info").value = context.label || "";
    $("printer-location").value = "";
    $("printer-default").checked = !state.printers.length;
    $("printer-default-row").hidden = false;
    $("printer-save").textContent = "新增";
    $("driver-query").value = "";
    $("driver-results").replaceChildren();
    $("driver-search-note").textContent = "";
    dialogError("");
    await fillDriverSelect(context.candidates || []);
    $("printer-dialog").showModal();
    $("printer-info").focus();
  }

  async function openEdit(printer) {
    state.dialog = { mode: "edit", printer, candidates: [] };
    $("printer-dialog-title").textContent = "編輯打印機";
    $("printer-dialog-source").textContent = `代號 ${printer.name} · ${printer.connection}`;
    $("printer-info").value = printer.info || "";
    $("printer-location").value = printer.location || "";
    $("printer-default-row").hidden = true;
    $("printer-save").textContent = "儲存";
    $("driver-query").value = "";
    $("driver-results").replaceChildren();
    $("driver-search-note").textContent = "";
    dialogError("");
    const network = /^(ipp|ipps|dnssd)/i.test(printer.uri);
    if (network) state.dialog.candidates = [{ id: "everywhere", label: "免驅動（打印機內建標準）" }];
    await fillDriverSelect(state.dialog.candidates, { keepCurrent: printer.make_model || "目前設定" });
    $("printer-dialog").showModal();
    $("printer-info").focus();
  }

  async function savePrinter(event) {
    event.preventDefault();
    const context = state.dialog;
    if (!context) return;
    let driver = $("printer-driver").value;
    if (driver === "__search") driver = $("driver-results").value;
    if (context.mode === "add" && !driver) return dialogError("請選擇驅動程式。可以在搜尋框輸入品牌和型號。");
    const save = $("printer-save");
    save.disabled = true;
    save.textContent = context.mode === "add" ? "正在新增…" : "正在儲存…";
    dialogError("");
    try {
      if (context.mode === "add") {
        const payload = { ...context.request, driver, info: $("printer-info").value, location: $("printer-location").value, default: $("printer-default").checked };
        await api("/api/admin/printers", { method: "POST", body: payload, timeout: 90000 });
        $("printer-dialog").close();
        notice(`已新增「${$("printer-info").value}」。建議在「打印機」頁列印一張測試頁確認。`);
        location.hash = "#printers";
        if (currentView() === "printers") await loadPrinters();
      } else {
        const payload = { info: $("printer-info").value, location: $("printer-location").value };
        if (driver) payload.driver = driver;
        await api(`/api/admin/printers/${encodeURIComponent(context.printer.name)}`, { method: "PATCH", body: payload, timeout: 90000 });
        $("printer-dialog").close();
        notice("設定已儲存。");
        await loadPrinters();
      }
    } catch (error) {
      dialogError(error.message);
    } finally {
      save.disabled = false;
      save.textContent = context.mode === "add" ? "新增" : "儲存";
    }
  }

  /* ---------- Detection ---------- */

  function driverSummary(drivers) {
    const first = drivers?.[0];
    if (!first) return make("span", "lu-driver-line none", "沒有自動找到驅動，新增時可以搜尋或上傳。");
    if (first.id === "everywhere") return make("span", "lu-driver-line ok", "可免驅動列印");
    if (first.match === "exact") return make("span", "lu-driver-line ok", `找到相符驅動：${first.label}`);
    if (first.match === "similar") return make("span", "lu-driver-line maybe", `找到相近型號：${first.label}`);
    return make("span", "lu-driver-line maybe", "可使用通用驅動");
  }

  function renderScan() {
    const scanning = !!state.scan?.scanning;
    $("scan").disabled = scanning;
    $("scan").textContent = state.scan ? "重新檢測" : "開始檢測";
    $("scan-status").hidden = !scanning;
    const box = $("scan-results");
    box.replaceChildren();
    if (!state.scan || scanning) return;
    if (state.scan.error) box.append(make("div", "alert-message warning", state.scan.error));
    const devices = state.scan.devices || [];
    box.append(table(["打印機", "連線", "驅動程式", ""], devices.map((device) => [
      stack(device.label || device.service_name || "打印機", device.make_model && device.make_model !== device.label ? device.make_model : ""),
      connectionKinds[device.kind] || "網路",
      driverSummary(device.drivers),
      actions(device.added_as ? label(`已加入（${device.added_as}）`, "success") : button("新增", "cbi-button-add", () => openAdd({
        description: `${connectionKinds[device.kind] || "網路"} · ${device.make_model || device.label || ""}`,
        label: device.label || device.service_name,
        candidates: device.drivers || [],
        request: { source: "discovered", device_id: device.id },
      }))),
    ]), "沒有找到打印機。請確認打印機已開機、連上同一個網路；也可以用下方「輸入 IP 位址」。"));
  }

  async function pollScan() {
    window.clearTimeout(state.scanTimer);
    state.scan = await api("/api/admin/scan", { timeout: 40000 });
    renderScan();
    if (state.scan.scanning && currentView() === "add") state.scanTimer = window.setTimeout(() => pollScan().catch((error) => notice(error.message, "error")), 1500);
  }

  async function startScan() {
    await api("/api/admin/scan", { method: "POST" });
    state.scan = { scanning: true, devices: [] };
    renderScan();
    state.scanTimer = window.setTimeout(() => pollScan().catch((error) => {
      state.scan = null;
      renderScan();
      notice(error.message, "error");
    }), 1500);
  }

  async function probe(event) {
    event.preventDefault();
    const submit = event.submitter || $("probe-form").querySelector("button");
    const box = $("probe-result");
    await run(submit, async () => {
      box.replaceChildren(make("p", "lu-progress", "正在檢測這個位址…"));
      box.firstChild.prepend(make("span", "spinning"));
      try {
        const result = await api("/api/admin/probe", { method: "POST", body: { host: $("probe-host").value }, timeout: 90000 });
        renderProbe(result);
      } catch (error) {
        box.replaceChildren();
        throw error;
      }
    });
  }

  function renderProbe(result) {
    const box = $("probe-result");
    const panel = make("div", "lu-probe");
    if (!result.connections.length) {
      panel.append(make("p", "", "這個位址沒有可用的打印服務。請確認 IP 位址正確，而且打印機已開機。"));
      box.replaceChildren(panel);
      return;
    }
    const title = make("p");
    title.append(make("strong", "", result.make_model ? `找到：${result.make_model}` : "這個位址有打印服務回應，但沒有提供型號。"));
    panel.append(title, driverSummary(result.drivers));
    panel.append(table(["連線方式", "位址", ""], result.connections.map((connection) => [
      connection.label,
      connection.uri,
      actions(button("用這個連線新增", "cbi-button-add", () => openAdd({
        description: `${connection.label} · ${connection.uri}`,
        label: result.make_model || result.host.replace(/[[\]]/g, ""),
        candidates: (result.drivers || []).filter((driver) => driver.id !== "everywhere" || ["ipp", "ipps"].includes(connection.protocol)),
        request: { source: "manual", protocol: connection.protocol, host: result.host, port: connection.port, path: connection.path },
      }))),
    ]), ""));
    box.replaceChildren(panel);
  }

  function manualAdd(event) {
    event.preventDefault();
    const protocol = $("manual-protocol").value;
    const host = $("manual-host").value.trim();
    const port = $("manual-port").value ? Number($("manual-port").value) : null;
    const path = $("manual-path").value.trim();
    const candidates = ["ipp", "ipps"].includes(protocol) ? [{ id: "everywhere", label: "免驅動（打印機內建標準）" }] : [];
    openAdd({
      description: `${$("manual-protocol").selectedOptions[0].textContent} · ${host}`,
      label: host,
      candidates,
      request: { source: "manual", protocol, host, port, path },
    }).catch((error) => notice(error.message, "error"));
  }

  async function renderUploads() {
    const uploads = await uploadedDrivers();
    $("ppd-list").replaceChildren(uploads.length ? table(["已上傳的驅動檔", ""], uploads.map((upload) => [
      upload.label,
      actions(button("刪除", "cbi-button-remove", async () => {
        if (!await confirmAction("刪除驅動檔", `要刪除「${upload.label}」嗎？已經加入的打印機不受影響。`, "刪除")) return;
        await api(`/api/admin/ppds/${encodeURIComponent(upload.id.replace(/^upload:/, ""))}`, { method: "DELETE" });
        notice("驅動檔已刪除。");
        await renderUploads();
      })),
    ]), "") : "");
  }

  async function uploadPpd(event) {
    event.preventDefault();
    const file = $("ppd-file").files[0];
    if (!file) return;
    const submit = event.submitter || $("ppd-form").querySelector("button");
    await run(submit, async () => {
      const form = new FormData();
      form.append("file", file);
      const result = await api("/api/admin/ppds", { method: "POST", body: form, timeout: 60000 });
      notice(`已上傳「${result.label}」。新增打印機時可以在驅動清單選擇它。`);
      $("ppd-form").reset();
      await renderUploads();
    });
  }

  async function loadAdd() {
    await Promise.all([refreshPrinters(), renderUploads()]);
    if (state.scan?.scanning) await pollScan();
    else renderScan();
    // Warm the driver list in the background; the first read takes a few seconds.
    api("/api/admin/drivers?q=", { timeout: 60000 }).catch(() => {});
  }

  /* ---------- Jobs ---------- */

  async function loadJobs() {
    const data = await api("/api/admin/jobs");
    $("jobs-table").replaceChildren(table(["編號", "文件", "打印機", "送出者", "狀態", "時間", ""], data.jobs.map((job) => [
      String(job.id),
      job.title,
      job.printer,
      job.user || "—",
      label(job.state_label, job.state === 8 ? "important" : job.state === 9 ? "success" : job.state === 7 ? "" : "notice"),
      when(job.created_at),
      actions(job.can_cancel ? button("取消", "cbi-button-remove", async () => {
        if (!await confirmAction("取消任務", `要取消「${job.title}」嗎？`, "取消任務")) return;
        await api(`/api/admin/jobs/${job.id}/cancel`, { method: "POST" });
        notice("任務已取消。");
        await loadJobs();
      }) : null),
    ]), "目前沒有打印任務。"));
  }

  /* ---------- Fonts ---------- */

  async function loadFonts() {
    const data = await api("/api/admin/fonts");
    $("missing-fonts").replaceChildren(table(["字體", "文件數", "最近一次", "目前代替成"], data.missing.map((font) => [
      font.name,
      String(font.count),
      when(font.last_seen),
      font.used ? `${font.used}${font.similar ? "（相近）" : ""}` : "—",
    ]), "最近沒有缺少的字體。"));
    $("font-list").replaceChildren(data.fonts.length ? table(["已加入的字體", "大小", ""], data.fonts.map((font) => [
      stack(font.families.join("、") || font.name, font.name),
      `${Math.max(0.1, font.size / 1048576).toFixed(1)} MB`,
      actions(button("刪除", "cbi-button-remove", async () => {
        if (!await confirmAction("刪除字體", `要刪除「${font.families[0] || font.name}」嗎？之後的文件會改用其他字體。`, "刪除")) return;
        await api(`/api/admin/fonts/${encodeURIComponent(font.id)}`, { method: "DELETE" });
        notice("字體已刪除。");
        await loadFonts();
      })),
    ]), "") : "");
  }

  async function uploadFont(event) {
    event.preventDefault();
    const file = $("font-file").files[0];
    if (!file) return;
    const submit = event.submitter || $("font-form").querySelector("button");
    await run(submit, async () => {
      const form = new FormData();
      form.append("file", file);
      const result = await api("/api/admin/fonts", { method: "POST", body: form, timeout: 240000 });
      notice(`已加入「${result.families.join("、") || result.name}」，立即生效。打印頁上已放入的文件，要重新放入才會看到新的預覽。`);
      $("font-form").reset();
      await loadFonts();
    });
  }

  /* ---------- System ---------- */

  async function loadSystem() {
    $("must-change").hidden = !state.session.must_change;
  }

  async function changePassword(event) {
    event.preventDefault();
    const submit = event.submitter || $("password-form").querySelector("button");
    await run(submit, async () => {
      if ($("password-new").value !== $("password-again").value) throw new Error("兩次輸入的新密碼不一樣。");
      await api("/api/admin/password", { method: "POST", body: { current: $("password-current").value, new: $("password-new").value } });
      $("password-form").reset();
      const wasInitial = state.session.must_change;
      state.session.must_change = false;
      state.session.initial_password = false;
      notice("密碼已更新。");
      if (wasInitial) location.hash = "#status";
      route();
    });
  }

  async function login(event) {
    event.preventDefault();
    const submit = event.submitter || $("login-form").querySelector("button");
    await run(submit, async () => {
      const result = await api("/api/admin/login", { method: "POST", body: { password: $("login-password").value } });
      $("login-password").value = "";
      state.session = { logged_in: true, must_change: result.must_change, configured: true };
      route();
    });
  }

  async function logout() {
    await api("/api/admin/logout", { method: "POST" });
    state.session = await api("/api/admin/session");
    $("printer-dialog").open && $("printer-dialog").close();
    showLogin();
  }

  $("login-form").addEventListener("submit", login);
  $("logout").addEventListener("click", () => run($("logout"), logout));
  $("scan").addEventListener("click", () => run($("scan"), startScan));
  $("probe-form").addEventListener("submit", probe);
  $("manual-form").addEventListener("submit", manualAdd);
  $("ppd-form").addEventListener("submit", uploadPpd);
  $("font-form").addEventListener("submit", uploadFont);
  $("refresh-jobs").addEventListener("click", () => run($("refresh-jobs"), loadJobs));
  $("password-form").addEventListener("submit", changePassword);
  $("printer-form").addEventListener("submit", savePrinter);
  $("printer-cancel").addEventListener("click", () => $("printer-dialog").close());
  $("printer-dialog").addEventListener("close", () => { state.dialog = null; });
  $("printer-driver").addEventListener("change", driverChanged);
  $("driver-query").addEventListener("input", searchDrivers);
  $("driver-results").addEventListener("change", pickSearchResult);
  window.addEventListener("hashchange", route);
  start();
})();
