"use strict";

// Run the actual browser script against a small DOM fixture. No network or print jobs are sent.
const assert = require("node:assert/strict");
const { environment } = require("./browser_fixture.js");

const driverCaps = { sides: ["one-sided", "two-sided-long-edge"], color_modes: ["color", "monochrome"], media: ["a4", "letter"], custom_media: { min_width: 80, max_width: 300, min_height: 90, max_height: 400 }, print_options: [{ key: "Resolution", group: "quality", choices: [{ value: "300dpi" }, { value: "600dpi" }] }, { key: "MediaType", group: "paper", choices: [{ value: "Plain" }, { value: "Photo" }] }] };
const ippCaps = { sides: ["one-sided"], color_modes: ["monochrome"], media: ["a4"], print_options: [{ key: "printer-resolution", group: "quality", choices: [{ value: "300dpi" }, { value: "600dpi" }] }] };
function printers() { return [{ name: "brother", is_default: true, state: 3, accepting: true, capabilities: driverCaps, default_mode: "driver", print_modes: [{ id: "driver", label: "驅動（本地驗證）", capabilities: driverCaps, state: 3, accepting: true }, { id: "driverless", capabilities: ippCaps, state: 3, accepting: true }] }, { name: "epson", state: 3, accepting: true, capabilities: ippCaps, default_mode: "driverless", print_modes: [{ id: "driverless", capabilities: ippCaps, state: 3, accepting: true }] }]; }
async function load(cookie = "", protocol) { const page = environment(cookie, protocol); page.response.printers = printers(); page.response.default = "brother"; await page.refreshPrinters(); return page; }
function selectedOption(page, key) { return page.cups().find((input) => input.dataset.cupsOption === key); }
async function mode(page, value) { page.get("advanced-print-mode").value = value; await page.get("advanced-print-mode").emit("change"); }
function apply(page) { page.applyAdvancedSettings({ preventDefault() {} }); }
function switchPrinter(page, name) { const button = page.get("printer-list").children.find((element) => element.children?.some((child) => child.children?.some((label) => label.textContent === name))); assert.ok(button); return button.emit("click"); }
function decoded(cookie) { return JSON.parse(Buffer.from(cookie.split("=")[1], "base64url").toString("utf8")); }

(async () => {
  let page = await load();
  assert.equal(page.state.printMode, "auto");
  assert.equal(page.printModeLabel("driver"), "驅動（本地驗證）");
  for (const id of ["sides", "color-mode", "media", "orientation"]) assert.equal(page.get(id).value, "");
  assert.match(page.cookieWrites.at(-1), /Path=\/; Max-Age=31536000; SameSite=Lax$/);
  page.get("sides").value = "two-sided-long-edge"; await page.get("sides").emit("change");
  page.get("copies").value = "3"; await page.get("copies").emit("input");
  page.get("orientation").value = "landscape"; await page.get("orientation").emit("change");
  page.get("media").value = "custom"; await page.get("media").emit("change");
  assert.equal(page.get("advanced-media").value, "custom");
  page.get("custom-width").value = "100"; page.get("custom-height").value = "200";
  selectedOption(page, "Resolution").value = "600dpi";
  apply(page);
  assert.equal(page.state.mediaChoice, "custom");
  assert.equal(page.state.advancedOptions.Resolution, "600dpi");
  page = await load(page.cookie());
  assert.equal(page.get("copies").value, "3");
  assert.equal(page.get("sides").value, "two-sided-long-edge");
  assert.equal(page.get("orientation").value, "landscape");
  assert.equal(page.state.customMedia.width_mm, 100);
  assert.equal(page.state.advancedOptions.Resolution, "600dpi");

  // A mode is only a dialog draft until Apply. Cancel must keep both state and cookie intact.
  const beforeCancel = page.cookie();
  page.openAdvancedSettings("device"); await mode(page, "driverless");
  assert.ok(selectedOption(page, "printer-resolution"));
  assert.equal(selectedOption(page, "Resolution"), undefined);
  selectedOption(page, "printer-resolution").value = "300dpi";
  page.closeAdvancedSettings();
  assert.equal(page.state.printMode, "auto");
  assert.equal(page.state.advancedOptions.Resolution, "600dpi");
  assert.equal(page.cookie(), beforeCancel);

  page.openAdvancedSettings("device"); await mode(page, "driverless");
  selectedOption(page, "printer-resolution").value = "300dpi"; apply(page);
  assert.equal(page.state.printMode, "driverless");
  assert.equal(page.state.advancedOptions["printer-resolution"], "300dpi");
  assert.equal(page.get("sides").value, "");
  assert.equal(page.get("copies").value, "1");
  page.openAdvancedSettings("device"); await mode(page, "auto"); apply(page);
  assert.equal(page.state.advancedOptions.Resolution, "600dpi");
  assert.equal(page.get("copies").value, "3");
  assert.equal(page.state.mediaChoice, "custom");
  assert.equal(page.get("copies-minus").disabled, false);
  assert.equal(page.get("copies-plus").disabled, false);

  page.openAdvancedSettings("device"); await mode(page, "driverless");
  await page.get("advanced-reset").emit("click");
  assert.equal(page.get("advanced-print-mode").value, "auto");
  assert.equal(selectedOption(page, "Resolution").value, "");
  assert.equal(page.get("advanced-media").value, "");
  page.closeAdvancedSettings();
  assert.equal(page.state.advancedOptions.Resolution, "600dpi");

  await switchPrinter(page, "epson");
  assert.equal(page.get("copies").value, "1");
  assert.match(page.printModeLabel("auto"), /目前使用免驅動 IPP/);
  page.get("copies").value = "2"; await page.get("copies").emit("input");
  page = await load(page.cookie(), "https:");
  assert.equal(page.state.selectedPrinter, "epson");
  assert.equal(page.get("copies").value, "2");
  assert.match(page.cookieWrites.at(-1), /; Secure$/);
  await switchPrinter(page, "brother");
  assert.equal(page.get("copies").value, "3");
  assert.equal(page.state.advancedOptions.Resolution, "600dpi");

  // Polling updates capabilities without replaying saved preferences over current input or modal drafts.
  page.get("copies").value = "4";
  await page.refreshPrinters();
  assert.equal(page.get("copies").value, "4");
  page.openAdvancedSettings(); selectedOption(page, "Resolution").value = "300dpi";
  await page.refreshPrinters();
  assert.equal(selectedOption(page, "Resolution").value, "300dpi");
  await page.get("advanced-settings").emit("cancel");
  assert.equal(page.state.printMode, "auto");
  assert.equal(page.state.advancedOptions.Resolution, "600dpi");

  // Removed choices and custom-media support fall back to Auto, rather than being sent to another driver.
  page.response.printers[0] = { ...page.response.printers[0], capabilities: ippCaps, default_mode: "driverless", print_modes: [{ id: "driverless", capabilities: ippCaps, state: 3, accepting: true }] };
  await page.refreshPrinters();
  assert.equal(page.state.mediaChoice, "");
  assert.equal(page.state.customMedia, null);
  assert.equal(page.state.advancedOptions.Resolution, undefined);

  for (const cookie of ["fpc_print_preferences=bad%encoding", "fpc_print_preferences=" + Buffer.from(JSON.stringify({ v: 1, e: Math.floor(Date.now() / 1000) - 1, p: [], l: "brother" })).toString("base64url")]) {
    const invalid = environment(cookie);
    assert.equal(invalid.preferences.printers.size, 0);
    assert.match(invalid.cookieWrites[0], /Max-Age=0/);
  }

  // Cookie remains under browser limits and has only the explicitly permitted printing fields.
  page = await load();
  page.state.csrf = "must-never-save-csrf"; page.state.items = [{ key: "file-1", name: "must-never-save-file", size: 1, upload: { id: "must-never-save-upload", status: "ready", pages: 1 } }];
  page.get("page-ranges").value = "3-5";
  for (let i = 0; i < 12; i++) {
    const fixture = printers()[0]; fixture.name = `printer-${i}`;
    page.state.printers.push(fixture); page.state.selectedPrinter = fixture.name; page.renderCapabilities(true);
    page.state.advancedOptions = Object.fromEntries(Array.from({ length: 20 }, (_, index) => [`Option${index}`, "x".repeat(40)]));
    page.rememberPrintPreferences();
    assert.ok(Buffer.byteLength(page.cookieWrites.at(-1)) < 4096);
  }
  const payload = JSON.stringify(decoded(page.cookie()));
  assert.doesNotMatch(payload, /must-never-save|page.ranges|csrf|upload|file/);
  assert.equal(decoded(page.cookie()).l, "printer-11");

  // One large active driver must keep basic choices, even after inactive profiles are evicted.
  page = await load();
  const largeCaps = { ...driverCaps, print_options: Array.from({ length: 40 }, (_, index) => ({ key: `LongDriverOption${index}`, choices: [{ value: `Choice${index}-` + "x".repeat(160) }, { value: "default" }] })) };
  page.response.printers[0].print_modes[0].capabilities = largeCaps;
  await page.refreshPrinters();
  page.state.printMode = "driver";
  page.renderCapabilities();
  page.get("copies").value = "7";
  page.get("sides").value = "two-sided-long-edge";
  page.get("color-mode").value = "color";
  page.get("orientation").value = "landscape";
  page.get("media").value = "custom";
  page.state.mediaChoice = "custom";
  page.state.customMedia = { width_mm: 100, height_mm: 200 };
  page.state.advancedOptions = Object.fromEntries(largeCaps.print_options.map((option) => [option.key, option.choices[0].value]));
  page.rememberPrintPreferences();
  const bounded = decoded(page.cookie());
  assert.ok(Buffer.byteLength(page.cookieWrites.at(-1)) < 4096);
  assert.equal(bounded.l, "brother");
  assert.equal(bounded.p[0].m, "driver");
  const savedActive = bounded.p[0].s.find((saved) => saved.m === "driver");
  assert.deepEqual(savedActive.b, [7, "two-sided-long-edge", "color", "custom", "landscape"]);
  assert.deepEqual(savedActive.c, [100, 200]);
  assert.ok(savedActive.a.length > 0 && savedActive.a.length < largeCaps.print_options.length);
  const restored = environment(page.cookie());
  restored.response.printers = page.response.printers;
  restored.response.default = "brother";
  await restored.refreshPrinters();
  assert.equal(restored.state.selectedPrinter, "brother");
  assert.equal(restored.state.printMode, "driver");
  assert.equal(restored.get("copies").value, "7");
  assert.equal(restored.get("sides").value, "two-sided-long-edge");
  assert.equal(restored.get("orientation").value, "landscape");
  assert.equal(restored.state.mediaChoice, "custom");
  assert.equal(restored.state.customMedia.width_mm, 100);
  assert.equal(Object.keys(restored.state.advancedOptions).length, savedActive.a.length);
  console.log("PASS: Auto defaults, cookie reload and expiry, printer/mode isolation, custom media, modal cancel/apply, capability changes, poll preservation, secure attributes, oversized-driver fallback, cookie size and sensitive-field exclusion.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
