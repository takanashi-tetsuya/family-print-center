"use strict";

// Actual preview controls against the shared app fixture; image completion is deliberately reordered.
const assert = require("node:assert/strict");
const { environment } = require("./browser_fixture.js");

const ready = (key, name, pages, native = false) => ({ key, name, size: 2048, upload: { id: key.repeat(32), status: "ready", pages, native }, uploading: false, sent: false });

function open() {
  const records = new Map();
  const page = environment("", "http:", async (url) => ({ ok: true, status: 200, json: async () => records.get(url) || { ok: true } }));
  page.get("preview-file").tagName = "SELECT";
  page.get("preview-page-number").tagName = "INPUT";
  const images = [];
  const create = page.document.createElement;
  page.document.createElement = (tag) => {
    const element = create(tag);
    if (tag === "img") images.push(element);
    return element;
  };
  const frame = create("div");
  let displayed = page.get("pdf-preview");
  const lookup = page.document.getElementById;
  page.document.getElementById = (id) => id === "pdf-preview" ? displayed : lookup(id);
  const prototype = Object.getPrototypeOf(displayed);
  prototype.closest = () => frame;
  prototype.replaceWith = function (replacement) {
    assert.equal(this, displayed);
    this.remove();
    frame.append(replacement);
    displayed = replacement;
  };
  frame.append(displayed);
  return { ...page, records, images, frame, image: () => displayed };
}

async function key(page, value) {
  for (const handler of page.get("preview-page-number").events.get("keydown") || []) {
    await handler({ key: value, preventDefault() {}, target: page.get("preview-page-number") });
  }
}

async function jump(page, value, action = "Enter") {
  const input = page.get("preview-page-number");
  page.document.activeElement = input;
  input.value = value;
  await input.emit("input");
  if (action === "blur") { page.document.activeElement = null; await input.emit("blur"); }
  else await key(page, action);
}

(async () => {
  const page = open();
  const a = ready("a", "first.pdf", 10);
  const b = ready("b", "second.docx", 3, true);
  const waiting = { key: "c", name: "waiting.pdf", size: 2048, upload: { id: "c".repeat(32), status: "converting" } };
  const failed = { key: "d", name: "failed.pdf", size: 2048, upload: { status: "error", error: "Invalid document" } };
  page.state.items = [a, b, waiting, failed];
  page.selectItem(a);
  assert.equal(page.get("preview-file-controls").hidden, false);
  assert.equal(page.get("preview-file").value, "a");
  assert.deepEqual(Array.from(page.get("preview-file").options, (option) => option.value), ["a", "b", "c", "d"]);
  assert.equal(page.get("previous-file").disabled, true);
  assert.equal(page.get("next-file").disabled, false);
  assert.equal(page.get("preview-page-number").value, "1");
  assert.equal(page.get("preview-page-number").max, "10");
  assert.equal(page.get("preview-page-total").textContent, "/ 10 頁");
  assert.equal(page.get("previous-page").disabled, true);
  assert.equal(page.get("next-page").disabled, false);
  await page.images.at(-1).emit("load");
  assert.equal(page.image().hidden, false);
  assert.equal(page.image().alt, "first.pdf，第 1 頁");

  // An explicit page is committed with Enter or blur; partial typing survives another file finishing.
  await jump(page, "6");
  assert.equal(page.state.previewPage, 6);
  assert.equal(a.previewPage, 6);
  assert.match(page.images.at(-1).src, /\/pages\/6\/preview\.png$/);
  const sixth = page.images.at(-1);
  const input = page.get("preview-page-number");
  input.value = "9";
  await page.removeItem(failed);
  assert.equal(input.value, "9");
  assert.equal(page.state.previewPage, 6);
  assert.equal(page.images.at(-1), sixth);
  await key(page, "Enter");
  assert.equal(page.state.previewPage, 9);
  await jump(page, "10", "blur");
  assert.equal(page.state.previewPage, 10);
  assert.equal(page.get("next-page").disabled, true);
  assert.equal(page.get("previous-page").disabled, false);

  // Malformed/out-of-range values never change the requested page; blur restores the visible page.
  for (const value of ["", "0", "11", "-1", "2.5", "2e0", "abc", "9007199254740992"]) {
    await jump(page, value);
    assert.equal(page.state.previewPage, 10);
    assert.match(input.validationMessage, /1 到 10 的整數/);
    page.document.activeElement = null;
    await input.emit("blur");
    assert.equal(input.value, "10");
    assert.equal(input.validationMessage, "");
  }
  await jump(page, "4", "Escape");
  assert.equal(input.value, "10");
  assert.equal(page.state.previewPage, 10);
  await jump(page, " 02 ");
  assert.equal(page.state.previewPage, 2);
  assert.equal(input.value, "2");

  // Switching files changes only the selected preview, retaining queue order, settings and last pages.
  const order = Array.from(page.state.items, (item) => item.key);
  page.state.advancedOptions = { "print-quality": "5" };
  page.get("copies").value = "2";
  const oldPage = page.images.at(-1);
  page.get("preview-file").value = "b";
  await page.get("preview-file").emit("change");
  assert.equal(page.state.current, "b");
  assert.equal(input.value, "1");
  assert.equal(page.get("preview-page-total").textContent, "/ 3 頁");
  const secondPage = page.images.at(-1);
  await oldPage.emit("load");
  assert.equal(page.image().hidden, true);
  assert.equal(page.get("preview-loading").hidden, false);
  await secondPage.emit("load");
  assert.equal(page.image(), secondPage);
  assert.equal(page.image().alt, "second.docx，第 1 頁");
  await oldPage.emit("error");
  assert.equal(page.image().hidden, false);
  assert.equal(page.get("preview-error").hidden, true);
  await jump(page, "3");
  await page.get("previous-file").emit("click");
  assert.equal(page.state.current, "a");
  assert.equal(page.state.previewPage, 2);
  await page.get("next-file").emit("click");
  assert.equal(page.state.current, "b");
  assert.equal(page.state.previewPage, 3);
  assert.deepEqual(Array.from(page.state.items, (item) => item.key), order);
  assert.equal(page.get("copies").value, "2");
  assert.equal(page.state.advancedOptions["print-quality"], "5");

  // Waiting files stay selectable, with a placeholder, and an earlier pending image stays cancelled.
  const beforeWaiting = page.images.at(-1);
  await page.get("next-file").emit("click");
  assert.equal(page.state.current, "c");
  assert.equal(page.get("preview-panel").hidden, false);
  assert.equal(page.get("preview-page-controls").hidden, true);
  assert.equal(page.get("preview-unavailable").hidden, false);
  assert.match(page.get("preview-unavailable").textContent, /正在準備/);
  assert.equal(page.get("next-file").disabled, true);
  await beforeWaiting.emit("load");
  assert.equal(page.image().hidden, true);
  await page.get("previous-file").emit("click");
  assert.equal(page.state.previewPage, 3);

  // Bounds update if a document's page count changes; rendering never fetches beyond its new last page.
  b.upload.pages = 2;
  await page.removeItem(waiting);
  assert.equal(page.state.previewPage, 2);
  assert.equal(input.max, "2");
  assert.match(page.images.at(-1).src, /\/pages\/2\/preview\.png$/);
  await page.images.at(-1).emit("error");
  assert.equal(page.get("preview-error").hidden, false);
  assert.equal(page.image().hidden, true);

  // Controls and the original file list cannot move selection while a print round is submitting.
  // Completion polling rerenders the same prepared document during a submission round.
  page.selectItem(a);
  page.state.printing = true;
  page.records.set(`/api/uploads/${a.upload.id}`, { ...a.upload });
  a.upload.status = "converting";
  await page.pollUploads();
  assert.equal(page.get("preview-file").disabled, true);
  assert.equal(page.get("previous-file").disabled, true);
  assert.equal(page.get("next-file").disabled, true);
  assert.equal(page.get("preview-page-number").disabled, true);
  assert.equal(a.row.pick.disabled, true);
  page.get("preview-file").value = "b";
  await page.get("preview-file").emit("change");
  assert.equal(page.state.current, "a");
  page.selectItem(b);
  assert.equal(page.state.current, "a");
  await jump(page, "1");
  assert.equal(page.state.previewPage, 2);
  await page.get("next-page").emit("click");
  assert.equal(page.state.previewPage, 2);

  // With only one document the selector disappears; Office originals without a preview explain it.
  page.state.printing = false;
  await page.removeItem(b);
  assert.equal(page.get("preview-file-controls").hidden, true);
  assert.equal(page.get("preview-file").disabled, true);
  const noPreview = ready("e", "original.docx", null, true);
  page.state.items.push(noPreview);
  page.selectItem(noPreview);
  assert.equal(page.get("preview-panel").hidden, false);
  assert.equal(page.get("preview-page-controls").hidden, true);
  assert.match(page.get("preview-unavailable").textContent, /沒有可用預覽/);
  const errorItem = { key: "f", name: "invalid.pdf", size: 100, upload: { status: "error", error: "Cannot convert" } };
  page.state.items.push(errorItem);
  page.selectItem(errorItem);
  assert.match(page.get("preview-unavailable").textContent, /無法準備/);

  console.log("Preview navigation checks passed: file switching, typed page jumps, retained pages, bounds and stale image isolation.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
