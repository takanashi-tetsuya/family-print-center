"use strict";

// Checks the real paper helpers and select controls; no network or printing.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { environment } = require("./browser_fixture.js");

async function helpers(page) {
  const context = vm.createContext({ document: page.document, Option: function (text, value) { const option = page.document.createElement("option"); option.textContent = text; option.value = value; return option; } });
  const modules = new Map();
  function load(file) {
    if (modules.has(file)) return modules.get(file);
    const module = new vm.SourceTextModule(fs.readFileSync(file, "utf8"), { context, identifier: file });
    modules.set(file, module);
    module.linkRequests(module.moduleRequests.map((request) => load(path.resolve(path.dirname(file), request.specifier), context)));
    return module;
  }
  const module = load(path.resolve(__dirname, "../print_center/static/app/paper.js"));
  module.instantiate();
  await module.evaluate();
  return module.namespace;
}

(async () => {
  const page = environment();
  const paper = await helpers(page);
  const offered = ["na_number-10_4.125x9.5in", "na_index-4x6_4x6in", "iso_a5_148x210mm", "iso_a4_210x297mm", "na_letter_8.5x11in", "iso_a4_210x297mm.borderless", "om_card_100x150mm", "custom", "iso_a4_210x297mm"];
  paper.setPaperOptions("media", offered, "na_letter_8.5x11in");
  const select = page.get("media");
  assert.equal(select.options[0].value, "");
  assert.equal(select.options[0].textContent, "自動");
  assert.equal(select.value, "na_letter_8.5x11in");
  assert.deepEqual(select.children.filter((child) => child.tagName === "OPTGROUP").map((child) => child.label), ["常用紙張", "信封與明信片", "相片紙", "其他尺寸"]);
  assert.equal(select.options[1].value, "iso_a4_210x297mm");
  assert.equal(select.options.at(-1).value, "custom");
  assert.match(select.options.find((option) => option.value === "na_letter_8.5x11in").textContent, /Letter（215\.9 × 279\.4 mm）/);
  assert.match(select.options.find((option) => option.value.endsWith("borderless")).textContent, /A4.*無邊框/);
  assert.equal(select.options.filter((option) => option.value === "iso_a4_210x297mm").length, 1);
  assert.deepEqual(new Set(select.options.map((option) => option.value).filter(Boolean)), new Set(offered));

  const dimensions = paper.mediaDimensions("na_letter_8.5x11in");
  assert.ok(Math.abs(dimensions.width_mm - 215.9) < 1e-8);
  assert.ok(Math.abs(dimensions.height_mm - 279.4) < 1e-8);
  assert.equal(paper.mediaDimensions("B5"), null); // The driver might mean either ISO or JIS B5.
  assert.equal(paper.mediaDimensions("iso_b5_176x250mm").width_mm, 176);
  assert.equal(paper.mediaDimensions("jis_b5_182x257mm").width_mm, 182);
  assert.equal(paper.mediaDimensions("unknown-value"), null);
  assert.equal(paper.mediaDimensions("custom", { width_mm: 110, height_mm: 220 }).height_mm, 220);
  assert.equal(paper.mediaDimensions("custom", { width_mm: 0, height_mm: 220 }), null);
  assert.match(paper.paperLabel("oe_3-5x-5-borderless_3.5x5in"), /^L 版相片（88\.9 × 127 mm） · 無邊框$/);
  assert.match(paper.paperLabel("om_101-6x-180-6mm-borderless_101.6x180.6mm"), /^其他尺寸（101\.6 × 180\.6 mm） · 無邊框$/);
  assert.match(paper.paperLabel("om_b-5-borderless_182x257mm"), /^B5（JIS）（182 × 257 mm） · 無邊框$/);
  assert.match(paper.paperLabel("om_195x-270mm-borderless_195x270mm"), /^16K（195 × 270 mm） · 無邊框$/);

  paper.setPaperOptions("media", ["a4", "A4", "iso_a4_210x297mm"], "A4");
  assert.equal(select.value, "A4"); // Aliases remain valid: no rewriting an existing cookie's value.
  paper.setPaperOptions("media", ["a4"], "unsupported-size");
  assert.equal(select.value, "");
  assert.equal(select.disabled, false);
  paper.setPaperOptions("media", [], "a4");
  assert.equal(select.disabled, true);
  assert.equal(select.options.length, 1);

  // Capabilities keep Auto as the default, and polling preserves a manual choice.
  const caps = { sides: ["one-sided"], color_modes: ["monochrome"], media: offered.filter((value) => value !== "custom"), print_options: [] };
  page.response.printers = [{ name: "paper-printer", is_default: true, capabilities: caps }];
  await page.refreshPrinters();
  assert.equal(select.value, "");
  select.value = "na_letter_8.5x11in";
  await select.emit("change");
  await page.refreshPrinters();
  assert.equal(select.value, "na_letter_8.5x11in");
  page.openAdvancedSettings("paper");
  assert.equal(page.get("advanced-media").value, "na_letter_8.5x11in");
  assert.equal(page.get("advanced-media").options[0].value, "");
  page.closeAdvancedSettings();
  console.log("PASS: paper groups, common-size ordering, mm dimensions, borderless distinction, advertised-value preservation, Auto, custom sizes, saved aliases and polling.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
