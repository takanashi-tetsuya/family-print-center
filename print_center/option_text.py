"""Plain-language names for driver options and common print modes.

Drivers describe their controls in English ("Toner Save", "MP Tray"). The family
page shows these names instead; anything unknown keeps the driver's own text.
"""
import re

CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")

OPTION_NAMES = {
    "mediatype": "紙張種類", "brmediatype": "紙張種類", "papertype": "紙張種類", "stpmediatype": "紙張種類",
    "inputslot": "進紙紙匣", "papersource": "進紙紙匣", "mediasource": "進紙紙匣", "stpinputslot": "進紙紙匣",
    "outputbin": "出紙位置", "outputtray": "出紙位置",
    "resolution": "清晰度（數字越大越清晰）", "setresolution": "清晰度（數字越大越清晰）",
    "jclresolution": "清晰度（數字越大越清晰）", "stpresolution": "清晰度（數字越大越清晰）",
    "printresolution": "清晰度（數字越大越清晰）",
    "tonersave": "省碳粉", "tonersavemode": "省碳粉", "tonersaving": "省碳粉", "brtonersave": "省碳粉",
    "economode": "省墨模式", "econode": "省墨模式", "ecomode": "省墨模式", "inksave": "省墨模式", "inksaving": "省墨模式",
    "cupsprintquality": "列印品質", "printquality": "列印品質", "quality": "列印品質", "stpquality": "列印品質",
    "outputmode": "列印品質", "hpprintquality": "列印品質",
    "colormodel": "色彩", "colormode": "色彩", "printcolormode": "色彩", "brcolormode": "色彩", "stpcolormodel": "色彩",
    "duplex": "雙面列印", "pagesize": "紙張大小", "mediasize": "紙張大小",
    "collate": "多份時逐份列印", "staple": "裝訂", "stapling": "裝訂", "stapler": "裝訂", "stapleposition": "裝訂位置",
    "punch": "打孔", "holepunch": "打孔", "fold": "摺頁",
    "brightness": "亮度", "density": "濃淡", "darkness": "濃淡", "tonerdensity": "濃淡", "printdensity": "濃淡",
    "brdensity": "濃淡", "stpdensity": "濃淡", "contrast": "對比", "saturation": "色彩鮮豔度", "gamma": "明暗曲線",
    "halftone": "灰階表現方式", "halftoning": "灰階表現方式", "screening": "灰階表現方式", "dithering": "灰階表現方式",
    "mirror": "鏡像列印", "mirrorprint": "鏡像列印", "reverseorder": "從最後一頁開始印", "borderless": "無邊框",
    "manualduplex": "手動雙面", "watermark": "浮水印", "smoothing": "平滑文字", "brsmoothing": "平滑文字",
    "skipblankpage": "跳過空白頁", "skipblankpages": "跳過空白頁", "brskipblank": "跳過空白頁",
    "improveprintoutput": "改善列印效果", "brimprove": "改善列印效果", "reducepapercurl": "減少紙張捲曲",
    "improvetonerfixing": "加強碳粉附著", "tonerfixing": "加強碳粉附著", "bridirectional": "雙向列印（較快）",
    "inktype": "墨水種類", "cartridge": "墨水匣", "printingdirection": "列印方向",
}

CHOICE_NAMES = {
    "on": "開啟", "true": "開啟", "yes": "開啟", "enable": "開啟", "enabled": "開啟",
    "off": "關閉", "false": "關閉", "no": "關閉", "disable": "關閉", "disabled": "關閉", "none": "不使用",
    "printerdefault": "打印機預設", "default": "打印機預設", "printersetting": "打印機預設", "usedefault": "打印機預設",
    "auto": "自動", "autoselect": "自動", "automatic": "自動", "automaticallyselect": "自動",
    "plain": "普通紙", "plainpaper": "普通紙", "thin": "薄紙", "thinpaper": "薄紙", "thick": "厚紙", "thickpaper": "厚紙",
    "thicker": "加厚紙", "thickerpaper": "加厚紙", "thickerpaper2": "加厚紙", "heavy": "厚紙", "heavypaper": "厚紙",
    "bond": "書寫紙（較厚）", "bondpaper": "書寫紙（較厚）", "recycled": "再生紙", "recycledpaper": "再生紙",
    "env": "信封", "envelope": "信封", "envelopes": "信封", "envthick": "厚信封", "envthin": "薄信封",
    "label": "標籤貼紙", "labels": "標籤貼紙", "transparency": "投影片", "transparencies": "投影片", "ohp": "投影片",
    "glossy": "光面相片紙", "glossypaper": "光面相片紙", "glossyphoto": "光面相片紙", "glossyphotopaper": "光面相片紙",
    "photo": "相片紙", "photopaper": "相片紙", "matte": "霧面相片紙", "mattepaper": "霧面相片紙",
    "mattephotopaper": "霧面相片紙", "cardstock": "卡紙", "card": "卡紙", "letterhead": "信紙",
    "preprinted": "已印刷的紙", "colored": "色紙", "colorpaper": "色紙", "postcard": "明信片", "hagaki": "明信片",
    "inkjetpaper": "噴墨紙", "inkjet": "噴墨紙",
    "draft": "草稿（較快、省墨）", "fast": "快速", "economy": "省墨", "eco": "省墨", "low": "草稿（較快、省墨）",
    "fastdraft": "草稿（較快、省墨）", "normal": "標準", "standard": "標準", "medium": "標準",
    "high": "高品質", "fine": "高品質", "highquality": "高品質", "best": "最佳（較慢）", "bestquality": "最佳（較慢）",
    "bestphoto": "最佳相片（最慢）", "photoquality": "相片品質",
    "mptray": "多用途紙匣", "multipurpose": "多用途紙匣", "multipurposetray": "多用途紙匣", "bypasstray": "多用途紙匣",
    "manual": "手動進紙", "manualfeed": "手動進紙", "rear": "後方進紙", "upper": "上層紙匣", "lower": "下層紙匣",
    "middle": "中層紙匣", "cassette": "紙匣", "envelopefeeder": "信封進紙器",
    "gray": "黑白", "grey": "黑白", "grayscale": "黑白", "greyscale": "黑白", "black": "黑白", "blackwhite": "黑白",
    "blackandwhite": "黑白", "monochrome": "黑白", "mono": "黑白", "color": "彩色", "colour": "彩色",
    "rgb": "彩色", "cmyk": "彩色（四色）", "rgbcolor": "彩色",
    "faceup": "正面朝上", "facedown": "正面朝下", "light": "較淡", "lighter": "較淡", "lightest": "最淡",
    "dark": "較深", "darker": "較深", "darkest": "最深",
    "longedge": "雙面・長邊翻頁", "duplexnotumble": "雙面・長邊翻頁", "shortedge": "雙面・短邊翻頁",
    "duplextumble": "雙面・短邊翻頁",
}


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def option_label(key, text=""):
    """Driver option name in everyday words, or the driver's own (possibly localized) text."""
    text = str(text or "").strip()
    if text and CJK.search(text):
        return text
    return OPTION_NAMES.get(_squash(key)) or OPTION_NAMES.get(_squash(text)) or text or key


def choice_label(value, text=""):
    text = str(text or "").strip()
    if text and CJK.search(text):
        return text
    # A driver's own description wins over guessing from its internal value.
    for source in [text or value]:
        squashed = _squash(source)
        if squashed in CHOICE_NAMES:
            return CHOICE_NAMES[squashed]
        tray = re.fullmatch(r"(?:tray|cassette|drawer|bin)(\d{1,2})", squashed)
        if tray:
            return f"紙匣 {int(tray[1])}"
        dpi = re.fullmatch(r"(\d{2,4})(?:x(\d{2,4}))?dpi", squashed)
        if dpi:
            return f"{dpi[1]} × {dpi[2]} DPI" if dpi[2] and dpi[2] != dpi[1] else f"{dpi[1]} DPI"
    return text or value


TONER_SAVE = re.compile(r"toner.?sav|econo|eco.?mode|ink.?sav|save.?toner|save.?ink", re.I)
DRIVER_QUALITY = re.compile(r"quality|outputmode", re.I)
DRIVER_MEDIA_TYPE = re.compile(r"media.?type|paper.?type", re.I)
ENABLED = {"on", "true", "yes", "1", "enable", "enabled"}
DRAFT = {"draft", "fast", "fastdraft", "economy", "eco", "low", "3"}
FINE = ["best", "bestquality", "highest", "high", "highquality", "fine", "5"]
GLOSSY = ["photographicglossy", "glossy", "glossyphoto", "glossyphotopaper", "glossypaper", "photographic",
          "photographichighgloss", "photographicsemigloss", "photo", "photopaper", "photographicmatte"]


def _find(options, pattern):
    return [item for item in options if pattern(item["key"])]


def _choice(option, wanted):
    """First choice whose value (or label) matches the wanted names, in their order of preference."""
    names = {}
    for choice in option["choices"]:
        for source in (choice["value"], choice.get("label", "")):
            names.setdefault(_squash(source), choice["value"])
    for name in wanted:
        if name in names:
            return names[name]
    return None


def print_presets(options, colors):
    """Quick choices built only from what this printer actually offers.

    Each preset fills in the ordinary controls, so the person can still see and
    change every individual setting afterwards.
    """
    by_key = {item["key"]: item for item in options}
    colors = set(colors or [])

    def quality(names):
        if "print-quality" in by_key:
            value = _choice(by_key["print-quality"], names)
            if value:
                return {"print-quality": value}
        for item in _find(options, lambda key: DRIVER_QUALITY.search(key) and key != "print-quality"):
            value = _choice(item, names)
            if value:
                return {item["key"]: value}
        return {}

    economy = quality(sorted(DRAFT))
    for item in _find(options, TONER_SAVE.search):
        value = _choice(item, sorted(ENABLED))
        if value:
            economy[item["key"]] = value
            break
    fine = quality(FINE)
    if not fine and "printer-resolution" in by_key:
        def dots(value):
            numbers = [int(n) for n in re.findall(r"\d+", value)] or [0]
            # Equal totals (2400x600 vs 1200) prefer the ordinary square resolution.
            return numbers[0] * numbers[-1] * (6.4516 if value.endswith("dpcm") else 1), numbers[0] == numbers[-1]
        fine = {"printer-resolution": max((choice["value"] for choice in by_key["printer-resolution"]["choices"]), key=dots)}

    photo = {}
    media_types = [by_key["media-type"]] if "media-type" in by_key else _find(options, DRIVER_MEDIA_TYPE.search)
    for item in media_types:
        value = _choice(item, GLOSSY)
        if value:
            photo[item["key"]] = value
            break
    if photo:
        photo.update(quality(["bestphoto", "photoquality", "photo", *FINE]))
        if "print-content-optimize" in by_key and _choice(by_key["print-content-optimize"], ["photo"]):
            photo["print-content-optimize"] = "photo"

    presets = [{"id": "standard", "label": "一般", "basic": {}, "advanced": {}}]
    mono = {"color_mode": "monochrome"} if {"monochrome", "color"} <= colors else {}
    if economy or mono:
        presets.append({"id": "economy", "label": "省墨", "basic": mono, "advanced": economy})
    if fine:
        presets.append({"id": "fine", "label": "精美", "basic": {}, "advanced": fine})
    if photo and colors & {"color", "auto"}:
        presets.append({"id": "photo", "label": "相片", "basic": {"color_mode": "color"} if "color" in colors else {},
                        "advanced": photo})
    for preset in presets:
        parts = ["黑白"] if preset["basic"].get("color_mode") == "monochrome" else []
        for key, value in preset["advanced"].items():
            option = by_key[key]
            label = next(choice["label"] for choice in option["choices"] if choice["value"] == value)
            if key == "printer-resolution":
                label = "最高清晰度"
            parts.append(label if key in {"print-quality", "media-type", "printer-resolution"} or
                         DRIVER_QUALITY.search(key) or DRIVER_MEDIA_TYPE.search(key) else f"{option['label']}：{label}")
        preset["summary"] = "、".join(parts) or "交給打印機決定，最常用"
    return presets if len(presets) > 1 else []
