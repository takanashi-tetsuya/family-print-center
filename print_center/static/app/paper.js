// Paper names and dimensions are shared by the quick settings and page setup.
import { $, make } from "./core.js";

const sizes = {
  a0: [841, 1189], a1: [594, 841], a2: [420, 594], a3: [297, 420], a4: [210, 297],
  a5: [148, 210], a6: [105, 148], a7: [74, 105], a8: [52, 74], a9: [37, 52], a10: [26, 37],
  letter: [215.9, 279.4], legal: [215.9, 355.6], executive: [184.15, 266.7],
  ledger: [431.8, 279.4], tabloid: [279.4, 431.8], statement: [139.7, 215.9],
  c5: [162, 229], c6: [114, 162], dl: [110, 220], monarch: [98.425, 190.5],
  number10: [104.775, 241.3], env10: [104.775, 241.3], envc6: [114, 162], envdl: [110, 220],
  isob5: [176, 250], isob6: [125, 176], jisb5: [182, 257], jisb6: [128, 182],
  hagaki: [100, 148], postcard: [100, 148], oufuku: [148, 200],
  l: [89, 127], photol: [89, 127], "2l": [127, 178],
  "4x6": [101.6, 152.4], index4x6: [101.6, 152.4], "5x7": [127, 177.8], "5x8": [127, 203.2], index5x8: [127, 203.2], "8x10": [203.2, 254],
  you4: [105, 235], chou3: [120, 235], chou4: [90, 205], kaku2: [240, 332],
};
const names = {
  letter: "Letter", legal: "Legal", executive: "Executive", ledger: "Ledger", tabloid: "Tabloid", statement: "Statement",
  b5: "B5", b6: "B6", isob5: "B5（ISO）", isob6: "B6（ISO）", jisb5: "B5（JIS）", jisb6: "B6（JIS）",
  c4: "C4 信封", c5: "C5 信封", c6: "C6 信封", dl: "DL 信封", monarch: "Monarch 信封", number9: "9 號信封", number10: "10 號信封", number11: "11 號信封", number12: "12 號信封", env10: "10 號信封", envc5: "C5 信封", envc6: "C6 信封", envdl: "DL 信封",
  hagaki: "明信片", postcard: "明信片", oufuku: "往返明信片", l: "L 版相片", photol: "L 版相片", "2l": "2L 版相片",
  "4x6": "4 × 6 吋相片", index4x6: "4 × 6 吋相片", "5x7": "5 × 7 吋相片", "5x8": "5 × 8 吋相片", index5x8: "5 × 8 吋相片", "8x10": "8 × 10 吋相片",
  "16k": "16K", govtletter: "8 × 10 吋相片", foolscap: "Foolscap", fanfoldgermanlegal: "Foolscap", you4: "洋形 4 號信封", chou3: "長形 3 號信封", chou4: "長形 4 號信封", kaku2: "角形 2 號信封",
};
const commonOrder = ["a4", "a5", "a3", "a6", "letter", "legal", "executive", "b5", "isob5", "jisb5", "b6", "isob6", "jisb6", "tabloid", "ledger", "statement", "16k", "foolscap", "fanfoldgermanlegal"];
const groupLabels = { common: "常用紙張", envelope: "信封與明信片", photo: "相片紙", other: "其他尺寸" };

function details(value) {
  const raw = String(value ?? "");
  const lower = raw.toLowerCase();
  const borderless = /borderless|fullbleed/.test(lower);
  const clean = lower.replace(/[._-]?(?:borderless|fullbleed)/g, "");
  const match = clean.match(/(?:^|_)([\d.]+)x([\d.]+)(mm|in)$/);
  const family = clean.match(/^([a-z]+)_/)?.[1] || "";
  const base = clean.replace(/^[a-z]+_/, "").replace(/_[\d.]+x[\d.]+(?:mm|in)$/, "");
  let key = base.replace(/[-_\s]/g, "");
  if (/^b\d+$/.test(key) && ["iso", "jis"].includes(family)) key = family + key;
  let dimensions = null;
  if (match) {
    const factor = match[3] === "in" ? 25.4 : 1;
    const width = Number(match[1]) * factor;
    const height = Number(match[2]) * factor;
    if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) dimensions = [width, height];
  }
  dimensions ||= sizes[key] || null;
  const sameSize = (size) => dimensions && size.every((number, index) => Math.abs(number - dimensions[index]) < 0.05);
  if (/^b[56]$/.test(key)) {
    const explicit = ["iso" + key, "jis" + key].find((name) => sameSize(sizes[name]));
    if (explicit) key = explicit;
  }
  const numericName = /^[\d.x-]+(?:mm|in)?$/.test(base);
  if (numericName && dimensions && !/mm$/.test(base)) {
    const photo = ["photol", "4x6", "5x7", "5x8", "8x10"].find((name) => sameSize(sizes[name]))
      || (sameSize([88.9, 127]) ? "photol" : null);
    if (photo) key = photo;
  }
  if (numericName && sameSize([195, 270])) key = "16k";
  const name = names[key] || (/^(?:a|b)\d+$/.test(key) ? key.toUpperCase() : /^(?:iso|jis)b\d+$/.test(key) ? `${key.slice(3).toUpperCase()}（${key.slice(0, 3).toUpperCase()}）` : numericName ? "其他尺寸" : base.replace(/[-_]/g, " ") || raw);
  const group = /信封|明信片/.test(name) || /^c\d+$/.test(key) || /envelope|^env/.test(key) ? "envelope"
    : /相片/.test(name) || /photo|^\d+(?:\.\d+)?x\d+(?:\.\d+)?(?:in)?$/.test(key) ? "photo"
      : commonOrder.includes(key) || /^(?:a|b|isob|jisb)\d+$/.test(key) ? "common" : "other";
  return { raw, key, name, dimensions, borderless, group };
}

function millimetres(value) {
  return String(Math.round(value * 100) / 100);
}

export function mediaDimensions(value, custom = null) {
  if (value === "custom") return custom && Number.isFinite(custom.width_mm) && Number.isFinite(custom.height_mm) && custom.width_mm > 0 && custom.height_mm > 0 ? { width_mm: custom.width_mm, height_mm: custom.height_mm } : null;
  const dimensions = details(value).dimensions;
  return dimensions ? { width_mm: dimensions[0], height_mm: dimensions[1] } : null;
}

export function paperLabel(value, custom = null) {
  if (!value) return "自動";
  if (value === "custom") return mediaDimensions(value, custom) ? `自訂（${millimetres(custom.width_mm)} × ${millimetres(custom.height_mm)} mm）` : "自訂尺寸…";
  const item = details(value);
  const dimensions = item.dimensions ? `（${item.dimensions.map(millimetres).join(" × ")} mm）` : "";
  return `${item.name}${dimensions}${item.borderless ? " · 無邊框" : ""}`;
}

export function paperGroups(values, custom = null) {
  const groups = Object.fromEntries(Object.keys(groupLabels).map((group) => [group, []]));
  const offered = [...new Set((Array.isArray(values) ? values : []).filter((value) => typeof value === "string" && value))];
  for (const value of offered) {
    if (value === "custom") continue;
    const item = details(value);
    groups[item.group].push({ value, label: paperLabel(value, custom), ...item });
  }
  const rank = (item) => commonOrder.indexOf(item.key) < 0 ? commonOrder.length : commonOrder.indexOf(item.key);
  for (const items of Object.values(groups)) items.sort((left, right) => rank(left) - rank(right)
    || Number(left.borderless) - Number(right.borderless)
    || left.label.localeCompare(right.label, "zh-Hant", { numeric: true })
    || left.value.localeCompare(right.value));
  return { groups: Object.entries(groups).filter(([, items]) => items.length).map(([key, options]) => ({ key, label: groupLabels[key], options })), custom: offered.includes("custom") };
}

export function setPaperOptions(id, values, preferred = "", custom = null) {
  const select = $(id);
  const choices = paperGroups(values, custom);
  select.replaceChildren(new Option("自動", ""));
  for (const group of choices.groups) {
    const element = make("optgroup");
    element.label = group.label;
    for (const item of group.options) element.append(new Option(item.label, item.value));
    select.append(element);
  }
  if (choices.custom) select.append(new Option(paperLabel("custom", custom), "custom"));
  select.value = Array.from(select.options).some((option) => option.value === preferred) ? preferred : "";
  select.disabled = select.options.length <= 1;
}
