"use strict";

// Exercise the actual dialog, cookie preferences, and print payload without sending a job.
const assert = require("node:assert/strict");
const { environment } = require("./browser_fixture.js");
const caps = { media: ["iso_a4_210x297mm", "iso_a5_148x210mm"], sides: ["one-sided"], color_modes: ["monochrome"], print_options: [] };
const printers = ["first", "second"].map((name, index) => ({ name, accepting: true, state: 3, is_default: index === 0, capabilities: caps, print_modes: [{ id: "driver", capabilities: caps }, { id: "driverless", capabilities: caps }] }));
async function open(cookie = "") { const page = environment(cookie); page.response.printers = printers; await page.refreshPrinters(); return page; }
const apply = (page) => page.applyAdvancedSettings({ preventDefault() {} });
const plain = (value) => JSON.parse(JSON.stringify(value));

(async () => {
  let page = await open();
  assert.equal(page.state.margins, null);
  page.openAdvancedSettings("paper");
  assert.equal(page.get("margin-preset").value, "auto");
  assert.equal(page.get("margin-top").disabled, true);
  page.get("margin-preset").value = "normal";
  await page.get("margin-preset").emit("change");
  assert.equal(page.get("margin-top").value, "25.4");
  page.closeAdvancedSettings();
  assert.equal(page.state.margins, null); // Cancel does not persist a draft.

  page.openAdvancedSettings("paper");
  page.get("margin-preset").value = "custom";
  await page.get("margin-preset").emit("change");
  for (const [side, value] of Object.entries({ top: 10, right: 20, bottom: 30, left: 0 })) page.get(`margin-${side}`).value = String(value);
  apply(page);
  assert.deepEqual(plain(page.state.margins), { top_mm: 10, right_mm: 20, bottom_mm: 30, left_mm: 0 });
  assert.match(page.get("margin-summary").textContent, /上 10.*下 30.*左 0.*右 20/);
  page = await open(page.cookie());
  assert.equal(page.state.margins.left_mm, 0); // A zero margin is valid and survives a reload.
  assert.equal(page.state.margins.top_mm, 10);
  page.state.selectedPrinter = "second"; page.renderCapabilities(true);
  assert.equal(page.state.margins, null);
  page.state.selectedPrinter = "first"; page.renderCapabilities(true);
  assert.equal(page.state.margins.bottom_mm, 30);

  page.openAdvancedSettings();
  page.get("advanced-print-mode").value = "driverless";
  await page.get("advanced-print-mode").emit("change");
  assert.equal(page.get("margin-preset").value, "auto");
  page.get("margin-preset").value = "narrow";
  await page.get("margin-preset").emit("change");
  page.get("advanced-print-mode").value = "driver";
  await page.get("advanced-print-mode").emit("change");
  assert.equal(page.get("margin-preset").value, "auto");
  page.get("advanced-print-mode").value = "driverless";
  await page.get("advanced-print-mode").emit("change");
  assert.equal(page.get("margin-preset").value, "narrow");
  apply(page);
  assert.equal(page.state.margins.top_mm, 12.7);
  page.openAdvancedSettings();
  page.get("advanced-print-mode").value = "auto";
  await page.get("advanced-print-mode").emit("change");
  apply(page);
  assert.equal(page.state.margins.top_mm, 10); // Each print mode keeps its own margins.

  page.get("orientation").value = "portrait";
  page.openAdvancedSettings();
  page.get("margin-top").value = "";
  apply(page);
  assert.equal(page.get("advanced-settings").open, true);
  assert.match(page.get("advanced-error").textContent, /0 到 100/);
  assert.equal(page.state.margins.top_mm, 10);
  page.get("margin-top").value = "101";
  apply(page);
  assert.equal(page.get("advanced-settings").open, true);
  page.get("margin-top").value = "10";
  page.get("advanced-media").value = "iso_a5_148x210mm";
  page.get("margin-left").value = "90"; page.get("margin-right").value = "90";
  apply(page);
  assert.match(page.get("advanced-error").textContent, /超過紙張大小/);
  page.closeAdvancedSettings();

  // The same bounds must apply to the actual orientation, including reverse landscape.
  page.get("orientation").value = "reverse-landscape";
  page.openAdvancedSettings();
  page.get("advanced-media").value = "iso_a5_148x210mm";
  page.get("margin-top").value = "75"; page.get("margin-bottom").value = "75";
  page.get("margin-left").value = "0"; page.get("margin-right").value = "0";
  apply(page);
  assert.match(page.get("advanced-error").textContent, /超過紙張大小/);
  page.get("margin-top").value = "73.75"; page.get("margin-bottom").value = "73.75";
  apply(page);
  assert.equal(page.get("advanced-settings").open, true); // 0.5 mm remaining is insufficient.
  page.closeAdvancedSettings();

  page.state.items = [{ key: "one", name: "file.pdf", upload: { id: "a".repeat(32), name: "file.pdf", status: "ready", pages: 1 } }];
  page.state.status = { cups: { available: true } };
  page.updatePrintState();
  page.requestPrint({ preventDefault() {} });
  assert.deepEqual(plain(page.state.pendingPrint[0].payload.margins), { top_mm: 10, right_mm: 20, bottom_mm: 30, left_mm: 0 });
  assert.equal(page.get("print-confirmation").open, true);

  // Refuse an incompatible slide batch before earlier PDF files can be printed.
  page.state.pendingPrint = null;
  page.state.items.push({ key: "slides", name: "deck.pptx", upload: { id: "b".repeat(32), name: "deck.pptx", native: true, status: "ready", pages: 1 } });
  page.updatePrintState(); page.requestPrint({ preventDefault() {} });
  assert.equal(page.state.pendingPrint, null);
  assert.match(page.toasts().at(-1), /PowerPoint.*自動/);
  page.state.items.pop();

  page.openAdvancedSettings();
  await page.get("advanced-reset").emit("click");
  assert.equal(page.get("margin-preset").value, "auto");
  assert.equal(page.state.margins.top_mm, 10);
  apply(page);
  assert.equal(page.state.margins, null);
  page.requestPrint({ preventDefault() {} });
  assert.equal("margins" in page.state.pendingPrint[0].payload, false);
  assert.equal((await open(page.cookie())).state.margins, null);
  console.log("PASS: page margins Auto/presets, four sides, zero values, cancel/apply/reset, cookie reload and printer isolation, invalid or excessive margins, and actual print payload.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
