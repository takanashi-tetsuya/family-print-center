"use strict";

// Several files in one list: uploads one at a time within the server's queue, then prints them in order.
const assert = require("node:assert/strict");
const { environment } = require("./browser_fixture.js");

const caps = { sides: ["one-sided", "two-sided-long-edge"], color_modes: ["monochrome"], media: ["a4"], print_options: [] };
const printers = [{ name: "brother", is_default: true, state: 3, accepting: true, capabilities: caps, default_mode: "driver", print_modes: [{ id: "driver", capabilities: caps, state: 3, accepting: true }] }];

// A stand-in for the print center API that records the order and overlap of requests.
function server() {
  const srv = { log: [], records: new Map(), uploadPosts: 0, refuse: new Set(), failPrint: new Set(), prints: [], busy: { uploads: 0, prints: 0 }, peak: { uploads: 0, prints: 0 } };
  const reply = (status, body, headers = {}) => ({ ok: status < 400, status, headers: { get: (key) => headers[key] ?? null }, json: async () => body });
  const pause = () => new Promise((resolve) => setImmediate(resolve));
  srv.byName = (name) => [...srv.records.values()].find((record) => record.name === name);
  srv.finish = (name, patch) => Object.assign(srv.byName(name), patch);
  srv.respond = async (url, options = {}) => {
    const method = options.method || "GET";
    const path = new URL(url, "http://example.test").pathname;
    srv.log.push(`${method} ${path}`);
    if (path === "/api/printers") return reply(200, { printers, default: "brother" });
    if (path === "/api/jobs") return reply(200, { jobs: [] });
    if (path === "/api/uploads" && method === "POST") {
      srv.uploadPosts += 1;
      srv.busy.uploads += 1;
      srv.peak.uploads = Math.max(srv.peak.uploads, srv.busy.uploads);
      await pause();
      srv.busy.uploads -= 1;
      if (srv.refuse.has(srv.uploadPosts)) return reply(429, { error: "目前等待轉換的文件較多，請稍後再試。" }, { "Retry-After": "5" });
      const id = srv.uploadPosts.toString(16).padStart(32, "0");
      srv.records.set(id, { id, name: options.body.get("file").name, status: "queued", pages: null, native: false });
      return reply(202, { id, status: "queued" });
    }
    const upload = path.match(/^\/api\/uploads\/([0-9a-f]{32})$/);
    if (upload && method === "DELETE") { srv.records.delete(upload[1]); return reply(200, { ok: true }); }
    if (upload) return reply(200, { ...srv.records.get(upload[1]) });
    if (path === "/api/print") {
      const payload = JSON.parse(options.body);
      srv.busy.prints += 1;
      srv.peak.prints = Math.max(srv.peak.prints, srv.busy.prints);
      await pause();
      srv.busy.prints -= 1;
      srv.prints.push(payload);
      if (srv.failPrint.has(srv.records.get(payload.upload_id).name)) return reply(400, { error: "打印機已暫停或不接受任務，請先恢復打印機。" });
      return reply(200, { job_id: 100 + srv.prints.length });
    }
    return reply(404, { error: "not found" });
  };
  return srv;
}

async function settle() { for (let turn = 0; turn < 40; turn++) await new Promise((resolve) => setImmediate(resolve)); }
const file = (name, size = 2048) => new File([new Uint8Array(size)], name, { lastModified: 1700000000000 });
const item = (page, name) => page.state.items.find((entry) => entry.name === name);
const meta = (page, name) => item(page, name).row.meta.textContent;
const label = (page) => page.get("print-button").querySelector("span").textContent;

async function open(srv) {
  const page = environment("", "http:", srv.respond);
  page.state.status = { cups: { available: true }, formats: ["pdf", "docx", "png"], max_upload_mb: 1, max_pending_uploads: 2 };
  await page.refreshPrinters();
  return page;
}

async function confirmAndPrint(page) {
  page.requestPrint({ preventDefault() {} });
  assert.equal(page.get("print-confirmation").open, true);
  const ids = Array.from(page.state.pendingPrint, (entry) => entry.payload.request_id);
  await page.submitPrint();
  await settle();
  return ids;
}

(async () => {
  const srv = server();
  srv.refuse.add(2);
  const page = await open(srv);

  // Unusable files never join the list; the rest upload one at a time.
  page.addFiles([file("a.pdf"), file("b.docx"), file("c.png"), file("d.exe"), file("empty.pdf", 0), file("big.pdf", 2 * 1024 * 1024)]);
  assert.deepEqual(Array.from(page.state.items, (entry) => entry.name), ["a.pdf", "b.docx", "c.png"]);
  assert.match(page.toasts().at(-1), /「d\.exe」的格式目前不支援.*「empty\.pdf」是空的.*「big\.pdf」超過 1 MB/);
  await settle();

  // A full conversion queue makes the file wait its turn instead of failing, and nothing jumps ahead of it.
  assert.equal(srv.uploadPosts, 2);
  assert.equal(meta(page, "b.docx"), "2 KB · 排隊中，稍後自動上傳");
  assert.equal(meta(page, "c.png"), "2 KB · 等待上傳");
  assert.ok(item(page, "b.docx").retryAt > Date.now());
  page.pumpUploads(); await settle();
  assert.equal(srv.uploadPosts, 2);
  item(page, "b.docx").retryAt = 0;
  page.pumpUploads(); await settle();
  assert.equal(srv.uploadPosts, 3);
  assert.equal(srv.byName("b.docx").status, "queued");

  // Two files are already waiting for conversion, which is this server's limit.
  assert.equal(item(page, "c.png").upload, null);
  assert.equal(page.get("print-button").disabled, true);
  assert.equal(page.get("stage-text").textContent, "正在準備文件（0 / 3）…");
  srv.finish("a.pdf", { status: "ready", pages: 2 });
  await page.pollUploads(); await settle();
  assert.equal(srv.uploadPosts, 4);
  assert.equal(meta(page, "a.pdf"), "2 KB · 2 頁");
  assert.equal(srv.peak.uploads, 1);

  // A conversion error stays on its file and blocks printing until that file is removed.
  srv.finish("b.docx", { status: "ready", pages: 3, native: true, substituted_fonts: [{ name: "標楷體", similar: true }] });
  srv.finish("c.png", { status: "error", error: "圖片尺寸或頁數過大，請縮小後再上傳。", substituted_fonts: [{ name: "X", similar: true }] });
  await page.pollUploads(); await settle();
  assert.equal(item(page, "c.png").row.error.textContent, "圖片尺寸或頁數過大，請縮小後再上傳。");
  assert.equal(item(page, "c.png").row.error.hidden, false);
  // Missing fonts are a quiet note on that file only, never alongside an error.
  assert.equal(item(page, "b.docx").row.note.textContent, "這份文件用了『標楷體』，已用相近字體代替，排版可能略有不同。");
  assert.equal(item(page, "b.docx").row.note.hidden, false);
  assert.equal(item(page, "a.pdf").row.note.hidden, true);
  assert.equal(item(page, "c.png").row.note.hidden, true);
  assert.match(page.toasts().join(" "), /「c\.png」無法準備/);
  assert.equal(page.get("print-button").disabled, true);
  assert.equal(page.get("print-summary").textContent, "1 份文件無法使用，移除後才能列印");
  assert.equal(page.get("stage-text").textContent, "1 份文件準備失敗，請移除");
  page.removeItem(item(page, "c.png")); await settle();
  assert.ok(srv.log.includes(`DELETE /api/uploads/${(4).toString(16).padStart(32, "0")}`));
  assert.equal(page.get("file-list").children.length, 2);

  // One set of settings for all files; page ranges only for a single file; Office page totals are approximate.
  assert.equal(page.get("print-button").disabled, false);
  assert.equal(label(page), "列印 2 份文件");
  assert.equal(page.get("print-summary").textContent, "2 份文件 · 約 5 頁 · 每份印 1 份");
  assert.equal(page.get("selected-pages").disabled, true);
  assert.equal(page.get("range-many-hint").hidden, false);
  assert.equal(page.state.current, item(page, "a.pdf").key);
  page.selectItem(item(page, "b.docx"));
  assert.match(page.get("preview-caption").textContent, /^第 2 \/ 2 份：b\.docx/);

  page.addFiles([file("e.pdf"), file("a.pdf")]);
  assert.match(page.toasts().at(-1), /「a\.pdf」已經在清單裡了/);
  await settle();
  srv.finish("e.pdf", { status: "ready", pages: 1 });
  await page.pollUploads(); await settle();
  assert.equal(label(page), "列印 3 份文件");

  // Printing goes strictly in order and stops at the first refusal; the refusal stays on that file.
  srv.failPrint.add("b.docx");
  const first = await confirmAndPrint(page);
  assert.equal(new Set(first).size, 3);
  assert.deepEqual(srv.prints.map((payload) => srv.records.get(payload.upload_id).name), ["a.pdf", "b.docx"]);
  assert.equal(srv.peak.prints, 1);
  assert.equal(meta(page, "a.pdf"), "2 KB · 2 頁 · 已送出 #101");
  assert.equal(item(page, "b.docx").row.error.textContent, "沒有送出：打印機已暫停或不接受任務，請先恢復打印機。");
  assert.equal(label(page), "繼續列印剩下 2 份");
  assert.equal(page.get("stage-text").textContent, "有文件沒送出，看看原因");
  assert.match(page.toasts().at(-1), /「b\.docx」沒有送出.*前面 1 份已送出，剩下 2 份還沒送出/);

  // Changed settings make a new request; unchanged settings reuse the refused file's request number.
  page.get("copies").value = "2";
  page.requestPrint({ preventDefault() {} });
  assert.notEqual(page.state.pendingPrint[0].payload.request_id, first[1]);
  page.get("copies").value = "1";
  srv.failPrint.clear();
  const second = await confirmAndPrint(page);
  assert.deepEqual(second.map((id, index) => id === first[index + 1]), [true, false]);
  assert.deepEqual(srv.prints.map((payload) => srv.records.get(payload.upload_id).name), ["a.pdf", "b.docx", "b.docx", "e.pdf"]);
  assert.equal(item(page, "b.docx").row.error.hidden, true);
  assert.equal(page.state.items.every((entry) => !entry.sent && entry.jobId), true);
  assert.equal(label(page), "列印 3 份文件");
  assert.equal(page.get("stage-text").textContent, "已送出，去拿紙吧");

  // A single file keeps the original wording and page ranges.
  const srv2 = server();
  const single = await open(srv2);
  single.addFiles([file("one.pdf")]); await settle();
  assert.equal(single.get("conversion-title").textContent, "文件已收到，等待處理…");
  assert.equal(single.get("selected-pages").disabled, false);
  assert.equal(single.get("range-many-hint").hidden, true);
  assert.equal(single.get("stage-text").textContent, "正在準備文件…");
  const fonts = ["A", "B", "C", "D"].map((name, index) => ({ name, similar: index !== 2 }));
  srv2.finish("one.pdf", { status: "ready", pages: 1, native: true, substituted_fonts: fonts });
  await single.pollUploads(); await settle();
  assert.equal(item(single, "one.pdf").row.note.textContent, "這份文件用了『A』、『B』、『C』等 4 種字體，已用其他字體代替，排版可能略有不同。");
  console.log("PASS: rejected files, one upload at a time, 429 wait and resend, client-side queue limit, conversion errors kept until removed, shared settings, approximate Office pages, ordered printing that stops at a refusal, request number reuse, single-file wording, font notes.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
