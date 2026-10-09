"""CUPS adapter. All calls use a fresh connection; credentials stay on this host."""
import getpass
import json
import math
import os
import ipaddress
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager, ExitStack
from pathlib import Path

from .option_text import choice_label, option_label, print_presets
from .printer_profiles import MODE_LABELS, PrinterProfiles, companion_name, is_companion_name, validate_profile


class CupsError(RuntimeError):
    pass


PRINTER_STATES = {3: "待命", 4: "打印中", 5: "已暫停"}
JOB_STATES = {3: "等待打印", 4: "已暫停", 5: "打印中", 6: "已停止", 7: "已取消", 8: "失敗", 9: "完成"}
STATE_REASONS = {
    "media-empty": "缺紙", "media-needed": "缺紙", "media-jam": "卡紙", "toner-low": "碳粉快用完",
    "toner-empty": "碳粉已用完", "marker-supply-low": "墨水／碳粉快用完", "marker-supply-empty": "墨水／碳粉已用完",
    "door-open": "機蓋未關", "cover-open": "機蓋未關", "input-tray-missing": "紙匣未裝好",
    "output-area-full": "出紙槽已滿", "offline": "打印機離線", "offline-report": "打印機離線",
    "connecting-to-device": "正在連線打印機", "cups-missing-filter": "缺少驅動程式元件",
    "paused": "已暫停", "shutdown": "已關機", "timed-out": "連線逾時", "other": "打印機回報錯誤",
    "other-warning": "打印機有警告", "other-report": "打印機狀態訊息",
    "media-low": "紙張快用完", "marker-waste-almost-full": "廢墨盒快滿了", "marker-waste-full": "廢墨盒已滿",
    "spool-area-full": "打印機記憶體已滿",
}
# Job keywords (RFC 8011 and CUPS) matched exactly; anything else is shown as CUPS sent it.
JOB_REASONS = {
    "job-canceled-by-operator": "被管理員取消", "job-canceled-at-device": "在打印機上被取消",
    "aborted-by-system": "打印服務中止了這份任務", "job-completed-with-errors": "印完了，但打印機回報錯誤",
    "job-completed-with-warnings": "印完了，但打印機有警告", "document-format-error": "打印機無法處理文件內容",
    "document-unprintable-error": "文件無法打印", "unsupported-document-format": "打印機不支援這種文件格式",
    "document-access-error": "讀不到文件", "job-data-insufficient": "文件資料不完整",
    "submission-interrupted": "文件傳送中斷", "printer-stopped": "打印機已暫停",
    "resources-are-not-ready": "打印機還沒準備好", "cups-held-for-authentication": "打印機要求登入",
}
# The state label already says these.
JOB_REASON_NOISE = {
    "none", "job-incoming", "job-queued", "job-queued-for-marker", "job-printing", "job-transforming",
    "job-interpreting", "job-outgoing", "job-fetchable", "job-restartable", "queued-in-device",
    "processing-to-stop-point", "job-completed-successfully", "job-hold-until-specified", "job-canceled-by-user",
}
ACTIVE_JOB_STATES = {3, 4, 5, 6}
# Supply levels a driver or the printer reports while printing (CUPS "marker" attributes).
MARKER_ATTRIBUTES = ["marker-names", "marker-colors", "marker-levels", "marker-low-levels", "marker-types", "marker-change-time"]
MARKER_COLORS = {"#000000": "黑色", "#00FFFF": "青色", "#FF00FF": "洋紅色", "#FFFF00": "黃色", "#808080": "灰色"}
LOW_SUPPLY_PERCENT = 15
GENERIC_SUPPLY_REASONS = {"碳粉快用完", "墨水／碳粉快用完"}
CONNECTIONS = {"usb": "USB 連接線", "dnssd": "網路（自動找到）", "ipp": "網路", "ipps": "網路（加密）",
               "socket": "網路（9100 埠）", "lpd": "網路（舊式）", "hp": "HP 專用連線", "parallel": "並列埠"}
DEVICE_SCHEMES = {"usb", "hp", "socket", "lpd", "ipp", "ipps", "parallel"}
# Backends that only print a generic placeholder or are discovered elsewhere.
SKIPPED_BACKENDS = ["dnssd", "file", "cups-pdf", "cups-brf", "smb", "beh", "implicitclass", "serial", "bluetooth",
                    "fax", "hpfax", "ipp", "ipps", "http", "https", "lpd", "socket", "driverless", "driverless-fax"]

# Values also go into a process-local lpoptions file for LibreOffice. They must
# remain single tokens, even when a driver reports a malformed choice.
OPTION_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
OPTION_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/,+-]{0,127}\Z")
RESERVED_OPTIONS = {
    "copies", "job-sheets", "page-ranges", "orientation-requested", "media",
    "sides", "print-color-mode", "fit-to-page", "landscape", "scaling",
    "document-format", "raw", "destination", "printer", "ppd", "device-uri",
    "auth-info", "auth-info-required", "requesting-user-name", "job-originating-user-name",
    "job-hold-until", "job-priority", "job-name", "job-id", "job-uuid",
}
ADMIN_OPTION = re.compile(
    r"(?:printer-|job-originating-|notify-|auth-|cups(?:IPPSupplies|SNMPSupplies|Auth)|"
    r"(?:br)?sleep|(?:br)?inactivity|(?:br)?autopower|poweroff|maintenance|cleaning)", re.I)


def safe_print_option(key, value=None):
    """Only per-job, single-token options may reach CUPS or native lpoptions."""
    if not isinstance(key, str) or not OPTION_KEY.fullmatch(key):
        return False
    if key.lower() in RESERVED_OPTIONS or ADMIN_OPTION.match(key):
        # printer-resolution is an IPP job setting despite its printer prefix.
        if key != "printer-resolution":
            return False
    return value is None or (isinstance(value, str) and bool(OPTION_VALUE.fullmatch(value)))


def _reasons(values, keep_unknown=True):
    """Printer warnings in everyday words; unknown codes are only useful to the administrator."""
    result = []
    for reason in _values(values):
        base = re.sub(r"-(?:error|warning|report)$", "", str(reason))
        text = STATE_REASONS.get(str(reason), STATE_REASONS.get(base, str(reason) if keep_unknown else None))
        if base not in {"none", ""} and text and text not in result:
            result.append(text)
    return result[:6]


def _job_reasons(values):
    result = []
    for reason in _values(values):
        reason = str(reason)
        text = JOB_REASONS.get(reason, reason)
        if reason not in JOB_REASON_NOISE and reason and text not in result:
            result.append(text)
    return result[:6]


def _job_printer_reasons(values):
    # cups-* keywords are the scheduler's bookkeeping, not something the printer said.
    return [reason for reason in _reasons(values) if reason != "已暫停" and not reason.startswith("cups-")]


def _printer_errors(values):
    """RFC 8011: a keyword without -warning or -report is an error."""
    return any(not re.search(r"-(?:warning|report)$", str(reason)) and not str(reason).startswith("cups-")
               and str(reason) not in {"none", "", "paused", "connecting-to-device"} for reason in _values(values))


def _printer_message(value):
    """The printer's own words, verbatim apart from control characters and passwords in URIs."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"(//)[^/@\s]*@", r"\1", text).strip()[:300]


def _supply_label(name, color, kind):
    colors = [value.upper() for value in re.findall(r"#[0-9A-Fa-f]{6}", str(color or ""))]
    hint = f"{kind} {name}".lower()
    noun = "墨水" if "ink" in hint else "碳粉" if "toner" in hint else ""
    if noun and len(colors) == 1 and colors[0] in MARKER_COLORS:
        return MARKER_COLORS[colors[0]] + noun
    if noun and len(colors) > 1:
        return "彩色" + noun
    if kind == "opc":
        return "感光鼓"
    return name or noun or "耗材"


def _supplies(attrs):
    """Levels as last reported; unknown levels (negative values) and waste tanks are left out."""
    names, colors, kinds, lows = (_values(attrs.get(key)) for key in ("marker-names", "marker-colors", "marker-types", "marker-low-levels"))
    result = []
    for index, level in enumerate(_values(attrs.get("marker-levels"))):
        try:
            level = int(level)
        except (TypeError, ValueError):
            continue
        kind = str(kinds[index]) if index < len(kinds) else ""
        # Waste tanks fill up rather than run out; printers report those as marker-waste-* reasons.
        if level < 0 or "waste" in kind:
            continue
        try:
            low_mark = int(lows[index]) if index < len(lows) else 0
        except (TypeError, ValueError):
            low_mark = 0
        level = min(level, 100)
        name = _printer_message(names[index] if index < len(names) else "")[:60]
        color = str(colors[index]) if index < len(colors) else ""
        result.append({"label": _supply_label(name, color, kind), "name": name, "level": level,
                       "color": color.upper() if re.fullmatch(r"#[0-9A-Fa-f]{6}", color) else "",
                       "low": level <= max(LOW_SUPPLY_PERCENT, low_mark if 0 < low_mark < 100 else 0)})
    return result[:12]


def _supply_time(attrs):
    try:
        return int(attrs.get("marker-change-time") or 0)
    except (TypeError, ValueError):
        return 0


def _values(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return [] if value is None else [value]


def _resolution(value):
    if isinstance(value, (list, tuple)) and len(value) == 3:
        x, y, unit = value
        if type(x) is not int or type(y) is not int or type(unit) is not int:
            return None
        if not (0 < x <= 100000 and 0 < y <= 100000) or unit not in {3, 4}:
            return None
        suffix = "dpi" if unit == 3 else "dpcm"
        return f"{x}{suffix}" if x == y else f"{x}x{y}{suffix}"
    if isinstance(value, str) and re.fullmatch(r"[1-9]\d{0,5}(?:x[1-9]\d{0,5})?(?:dpi|dpcm)", value):
        x_y, suffix = (value[:-3], "dpi") if value.endswith("dpi") else (value[:-4], "dpcm")
        parts = x_y.split("x")
        return f"{parts[0]}{suffix}" if len(parts) == 2 and parts[0] == parts[1] else value
    return None


def _choice_label(key, value):
    labels = {
        "print-quality": {"3": "草稿（較快、省墨）", "4": "標準", "5": "最佳（較慢）"},
        "media-type": {"stationery": "普通紙", "photographic": "相片紙", "photographic-glossy": "光面相片紙",
                       "photographic-matte": "霧面相片紙", "photographic-high-gloss": "高光澤相片紙", "photographic-semi-gloss": "半光澤相片紙",
                       "stationery-letterhead": "信紙", "stationery-coated": "塗層紙", "stationery-heavyweight": "厚紙",
                       "stationery-lightweight": "薄紙", "stationery-recycled": "再生紙", "cardstock": "卡紙",
                       "envelope": "信封", "labels": "標籤貼紙", "transparency": "投影片", "disc": "光碟"},
        "media-source": {"auto": "自動", "main": "主紙匣", "alternate": "備用紙匣", "manual": "手動進紙",
                         "tray-1": "紙匣 1", "tray-2": "紙匣 2", "tray-3": "紙匣 3", "rear": "後方進紙",
                         "by-pass-tray": "多用途紙匣", "envelope": "信封進紙器", "photo": "相片紙匣"},
        "output-bin": {"auto": "自動", "face-down": "正面朝下", "face-up": "正面朝上", "top": "上方出紙槽", "rear": "後方出紙槽"},
        "finishings": {"3": "不使用", "4": "釘書針裝訂", "5": "打孔", "6": "加封面", "7": "裝訂", "8": "騎馬釘（中間釘）", "9": "對摺"},
        "print-scaling": {"fit": "縮小到剛好放得下", "fill": "放大填滿整張紙（邊緣可能被裁掉）", "none": "原尺寸，不縮放",
                          "auto-fit": "太大時才縮小", "auto": "自動"},
        "print-content-optimize": {"auto": "自動", "text": "文字", "graphics": "圖表", "photo": "照片", "text-and-graphics": "文字和圖表"},
        "print-rendering-intent": {"auto": "自動", "absolute": "照原色（校色用）", "relative": "準確（適合文件）",
                                   "perceptual": "自然（適合照片）", "saturation": "鮮豔（適合圖表）"},
    }
    if key == "printer-resolution":
        return value.replace("x", " × ").replace("dpi", " DPI").replace("dpcm", " 點／厘米")
    return labels.get(key, {}).get(value, value)


def _select(key, label, group, choices, default=""):
    clean, seen = [], set()
    for value, text in choices:
        if safe_print_option(key, value) and value not in seen:
            clean.append({"value": value, "label": str(text or value)[:200]})
            seen.add(value)
    if not clean:
        return None
    return {"key": key, "label": label, "group": group, "type": "select", "choices": clean,
            "default": default if default in seen else ""}


def _ipp_options(attrs):
    result = []
    specifications = [
        ("printer-resolution", "清晰度（數字越大越清晰）", "quality"),
        ("print-quality", "列印品質", "quality"),
        ("media-type", "紙張種類", "paper"),
        ("media-source", "進紙紙匣", "paper"),
        ("output-bin", "出紙位置", "device"),
        ("finishings", "裝訂、打孔", "device"),
        ("print-content-optimize", "文件內容", "quality"),
        ("print-rendering-intent", "色彩風格", "quality"),
        ("print-scaling", "縮放", "layout"),
    ]
    for key, label, group in specifications:
        supported = attrs.get(f"{key}-supported")
        if key == "print-scaling" and not supported:
            supported = ["fit", "fill", "none", "auto-fit"]
        if key == "printer-resolution":
            # pycups returns either a list of resolution triples or a single triple.
            values = [supported] if _resolution(supported) else _values(supported)
            values = [normalized for value in values if (normalized := _resolution(value))]
            default = _resolution(attrs.get(f"{key}-default")) or ""
        else:
            values = [str(value) for value in _values(supported) if isinstance(value, str) or type(value) is int]
            default = str(attrs.get(f"{key}-default", ""))
        option = _select(key, label, group, [(value, _choice_label(key, value)) for value in values], default)
        if option:
            result.append(option)
    # Applied by the CUPS PDF filter on the server, so they work with any printer.
    common = [
        ("number-up", "一張紙印幾頁", [(str(n), "1 頁（正常）" if n == 1 else f"{n} 頁（縮小並排）") for n in (1, 2, 4, 6, 9, 16)]),
        ("number-up-layout", "一張多頁時的排列", [(value, label) for value, label in (
            ("lrtb", "由左到右、由上到下"), ("lrbt", "由左到右、由下到上"),
            ("rltb", "由右到左、由上到下"), ("rlbt", "由右到左、由下到上"),
            ("tblr", "由上到下、由左到右"), ("tbrl", "由上到下、由右到左"),
            ("btlr", "由下到上、由左到右"), ("btrl", "由下到上、由右到左"))]),
        ("page-set", "只印單數或雙數頁", [("all", "全部頁面"), ("odd", "只印單數頁（1、3、5…）"),
                                          ("even", "只印雙數頁（2、4、6…）")]),
        ("outputorder", "出紙順序", [("normal", "從第一頁開始"), ("reverse", "從最後一頁開始")]),
        ("page-border", "一張多頁時加框線", [("none", "不加"), ("single", "細線"), ("single-thick", "粗線"),
                                              ("double", "雙細線"), ("double-thick", "雙粗線")]),
        ("multiple-document-handling", "印多份時", [
            ("separate-documents-collated-copies", "一份一份印（1-2-3、1-2-3）"),
            ("separate-documents-uncollated-copies", "同一頁印完再印下一頁（1-1、2-2、3-3）")]),
        ("booklet", "做成小冊子（對摺後像一本書）", [("on", "開啟")]),
        ("mirror", "鏡像列印（左右反轉，燙畫轉印紙用）", [("true", "開啟")]),
    ]
    for key, label, choices in common:
        supported = attrs.get(f"{key}-supported")
        if supported:
            allowed = {str(value) for value in _values(supported)}
            choices = [(value, text) for value, text in choices if value in allowed]
        option = _select(key, label, "layout", choices, str(attrs.get(f"{key}-default", "")))
        if option:
            result.append(option)
    return result


def _custom_media(attrs):
    """Use advertised ranges only; fixed page sizes do not imply custom paper."""
    collections = _values(attrs.get("media-size-supported")) + _values(attrs.get("media-col-database"))
    for collection in collections:
        if not isinstance(collection, dict):
            continue
        size = collection.get("media-size", collection)
        if not isinstance(size, dict):
            continue
        ranges = []
        for axis in ("x-dimension", "y-dimension"):
            value = size.get(axis)
            bounds = value if isinstance(value, (list, tuple)) and len(value) == 2 else [value, value]
            if not all(type(n) is int and n > 0 for n in bounds) or bounds[0] > bounds[1]:
                break
            ranges.append(bounds)
        if len(ranges) == 2 and any(bounds[0] < bounds[1] for bounds in ranges):
            return {"min_width": ranges[0][0] / 100, "max_width": ranges[0][1] / 100,
                    "min_height": ranges[1][0] / 100, "max_height": ranges[1][1] / 100, "unit": "mm"}
    bounds = {}
    for media in _values(attrs.get("media-supported")):
        if not isinstance(media, str):
            continue
        match = re.fullmatch(r"custom_(min|max)_[^_]*?([\d.]+)x([\d.]+)(mm|in)", media)
        if not match:
            continue
        try:
            multiplier = 25.4 if match[4] == "in" else 1
            width, height = float(match[2]) * multiplier, float(match[3]) * multiplier
        except ValueError:
            continue
        if all(math.isfinite(n) and n > 0 for n in (width, height)):
            bounds[match[1]] = (width, height)
    if "min" in bounds and "max" in bounds and all(low <= high for low, high in zip(bounds["min"], bounds["max"])):
        return {"min_width": bounds["min"][0], "max_width": bounds["max"][0],
                "min_height": bounds["min"][1], "max_height": bounds["max"][1], "unit": "mm"}
    return None


class CupsClient:
    def __init__(self):
        self.user = os.environ.get("CUPS_USER", getpass.getuser())
        self.host = os.environ.get("CUPS_SERVER", "127.0.0.1")
        self.port = int(os.environ.get("CUPS_PORT", "631"))
        try:
            import cups
            self.cups = cups
        except ImportError:
            self.cups = None

    def configure_profiles(self, data_dir):
        self.profiles = PrinterProfiles(data_dir)

    def _profile_records(self):
        store = getattr(self, "profiles", None)
        return store.snapshot() if store else {}

    def register_profiles(self, name, queues, default_mode="driver"):
        """Register previously prepared queues; never replace a PPD during a job."""
        if not getattr(self, "profiles", None):
            raise ValueError("尚未設定打印模式儲存目錄。")
        validate_profile(name, queues, default_mode)
        def check(conn):
            printers = conn.getPrinters()
            if any(queue not in printers for queue in queues.values()):
                raise ValueError("打印模式使用的隊列尚未建立。")
            uris = {str(printers[queue].get("device-uri") or "") for queue in queues.values()}
            if len(uris) != 1 or not next(iter(uris)):
                raise ValueError("打印模式必須連接同一台打印機。")
        self._call(check)
        self.profiles.register(name, queues, default_mode)

    def resolve_print_mode(self, printer, mode="auto"):
        if not isinstance(mode, str) or mode not in {"auto", "driver", "driverless"}:
            raise ValueError("打印模式不正確。")
        selected_mode = printer["default_mode"] if mode == "auto" else mode
        selected = next((item for item in printer["print_modes"] if item["id"] == selected_mode), None)
        if selected is None:
            raise ValueError("此打印機不支援所選的打印模式。")
        record = self._profile_records().get(printer["name"])
        if record is None and len(printer["print_modes"]) > 1:
            raise ValueError("打印模式設定已變更，請重新整理頁面。")
        queue = record["queues"].get(selected_mode) if record else printer["name"]
        if not queue:
            raise ValueError("此打印模式暫時無法使用，請重新整理頁面。")
        return {**printer, **selected, "label": printer["label"], "name": queue, "print_mode": selected_mode}

    def _group_printers(self, printers, default):
        """Present one physical printer while keeping the mode's capabilities separate."""
        records = self._profile_records()
        by_name = {printer["name"]: printer for printer in printers}
        hidden = {queue for name, record in records.items() for queue in record["queues"].values() if queue != name}
        grouped = []
        logical_default = default
        for printer in printers:
            name = printer["name"]
            if name in hidden or is_companion_name(name):
                continue
            record = records.get(name)
            queues = record["queues"] if record else {printer.pop("_mode", "driver"): name}
            modes = []
            for mode in ("driver", "driverless"):
                source = by_name.get(queues.get(mode))
                if source is not None:
                    driver_name = source.get("_driver_name", "")
                    label = MODE_LABELS[mode]
                    if mode == "driver" and re.search(r"\bgeneric\b.*\bPCL\b", driver_name, re.I):
                        label = "通用 PCL 驅動"
                    elif mode == "driver" and re.search(r"\bgeneric\b.*\bPostScript\b", driver_name, re.I):
                        label = "通用 PostScript 驅動"
                    modes.append({"id": mode, "label": label, "driver_name": driver_name,
                                  "capabilities": source["capabilities"], "state": source["state"],
                                  "state_label": source["state_label"], "accepting": source["accepting"],
                                  "reasons": source["reasons"], "message": source["message"]})
            if not modes:
                continue
            preferred = record["default_mode"] if record else modes[0]["id"]
            selected = next((item for item in modes if item["id"] == preferred), modes[0])
            printer.update({key: selected[key] for key in ("capabilities", "state", "state_label", "accepting", "reasons", "message")})
            # Levels arrive with print jobs, so whichever mode printed last knows best.
            reported = max((by_name[queue] for queue in queues.values() if queue in by_name), key=lambda source: source.get("_supplies_time", 0))
            printer["supplies"] = reported.get("supplies", [])
            printer.pop("_supplies_time", None)
            printer.pop("_mode", None)
            printer.pop("_driver_name", None)
            printer.update(default_mode=selected["id"], print_modes=modes)
            printer["is_default"] = default in queues.values()
            if printer["is_default"]:
                logical_default = name
            grouped.append(printer)
        return {"printers": grouped, "default": logical_default}

    def _logical_queue_name(self, queue):
        return next((name for name, record in self._profile_records().items() if queue in record["queues"].values()), queue)

    def _connection(self):
        if not self.cups:
            raise CupsError("尚未安裝 pycups，請使用專案提供的容器部署。")
        try:
            # libcups username/password callbacks are thread-local, including in Waitress.
            self.cups.setUser(self.user)
            secret = os.environ.get("CUPS_PASSWORD_FILE")
            if secret:
                self.cups.setPasswordCB(lambda _prompt: Path(secret).read_text().strip())
            else:
                # Never let libcups ask for a password on the server's terminal.
                self.cups.setPasswordCB(lambda _prompt: "")
            return self.cups.Connection(host=self.host, port=self.port)
        except Exception as exc:
            raise CupsError("連不上打印服務，請稍後再試；若持續發生，請管理員檢查服務是否已啟動。") from exc

    def _call(self, callback, message="打印服務暫時無法完成這個操作，請確認打印機已開機後再試。"):
        try:
            return callback(self._connection())
        except (CupsError, ValueError):
            raise
        except Exception as exc:
            # Do not reflect internal server messages, paths or passwords into the UI.
            raise CupsError(message) from exc

    def status(self):
        try:
            self._call(lambda conn: conn.getPrinters())
            return {"available": True, "error": None}
        except CupsError as exc:
            return {"available": False, "error": str(exc)}

    def _native_ppd(self):
        # Real pycups 2.0.4 PPD getters leak native references. Keep native PPDs
        # out of the long-running web process; in-memory test adapters are safe.
        constructor = getattr(self.cups, "PPD", None)
        return isinstance(constructor, type) and constructor.__module__ == "cups"

    def _ppd_worker(self, operation, temporary, **data):
        payload = json.dumps({"operation": operation, "path": os.fsdecode(temporary), **data},
                             ensure_ascii=False)
        if len(payload.encode("utf-8")) > 4 * 1024 * 1024:
            raise CupsError("打印機設定資料過大，請管理員檢查。")
        # PPD parsing requires no CUPS connection, credentials or administrator
        # settings. Do not inherit those into the disposable native process.
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}}
        environment.update(PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "print_center.ppd_worker"], input=payload,
                text=True, encoding="utf-8", stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=10, check=False,
                cwd=Path(__file__).resolve().parents[1], env=environment,
            )
            if result.returncode or len(result.stdout.encode("utf-8")) > 4 * 1024 * 1024:
                raise ValueError("PPD worker failed")
            reply = json.loads(result.stdout)
            if not isinstance(reply, dict) or reply.get("operation") != operation:
                raise ValueError("Invalid PPD worker reply")
            return reply
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
            raise CupsError("無法核對打印機的驅動設定，請稍後再試；若持續發生，請管理員檢查服務。") from error

    def _ppd_options(self, conn, name, attrs, options):
        """Read the driver's job controls without touching queue defaults."""
        temporary = None
        try:
            if not hasattr(self.cups, "PPD"):
                return options, {}
            temporary = conn.getPPD(name)
            if self._native_ppd():
                attributes = {key: attrs[key] for key in
                              ("media-supported", "sides-supported", "print-color-mode-supported")
                              if key in attrs}
                reply = self._ppd_worker("options", temporary, attrs=attributes, options=options)
                controls, aliases = reply.get("options"), reply.get("aliases")
                if not isinstance(controls, list) or not isinstance(aliases, dict):
                    raise ValueError("Invalid PPD controls")
                return controls, aliases
            return self._ppd_options_local(temporary, attrs, options)
        except Exception:
            # IPP-only queues and failed PPD reads retain the IPP controls.
            return options, {}
        finally:
            if isinstance(temporary, (str, bytes, os.PathLike)):
                try:
                    Path(os.fsdecode(temporary)).unlink(missing_ok=True)
                except OSError:
                    pass

    def _ppd_options_local(self, temporary, attrs, options):
        """PPD calculation used by the isolated worker and in-memory adapters."""
        try:
            ppd = self.cups.PPD(temporary)
            groups, installed = [], {}

            def collect(items, inherited_installable=False):
                for group in items:
                    group_name = str(getattr(group, "name", ""))
                    installable = inherited_installable or "installable" in group_name.lower().replace(" ", "")
                    group_options = getattr(group, "options", [])
                    if installable:
                        for option in group_options:
                            installed[str(option.keyword).lower()] = str(option.defchoice).lower()
                    else:
                        groups.append((str(getattr(group, "text", "") or group_name or "打印機設定"), group_options))
                    collect(getattr(group, "subgroups", []), installable)

            collect(ppd.optionGroups)
            constraints = getattr(ppd, "constraints", [])
            aliases = {}
            # PPD choice names such as HQ1200 do not reliably describe the DPI.
            # Read their actual PostScript resolution, rather than guessing.
            source = Path(os.fsdecode(temporary)).read_text(encoding="utf-8", errors="replace")
            resolution_codes = {}
            for match in re.finditer(r'^\*(Resolution|SetResolution|JCLResolution)\s+([^/\s:]+)(?:/[^:]*)?:\s*"([\s\S]*?)"', source, re.M):
                dimensions = re.search(r"/HWResolution\s*\[\s*(\d+)\s+(\d+)\s*\]", match[3])
                if dimensions:
                    resolution_codes[(match[1], match[2])] = _resolution((int(dimensions[1]), int(dimensions[2]), 3))

            def blocked(key, value):
                for constraint in constraints:
                    left = (str(constraint.option1).lower(), str(constraint.choice1).lower())
                    right = (str(constraint.option2).lower(), str(constraint.choice2).lower())
                    for selected, fixed in ((left, right), (right, left)):
                        if (selected[0] == key.lower() and (not selected[1] or selected[1] == value.lower())
                                and fixed[0] in installed and (not fixed[1] or installed[fixed[0]] == fixed[1])):
                            return True
                return False

            result = list(options)
            seen = {item["key"].lower() for item in result}
            primary = set()
            if attrs.get("media-supported"):
                primary.update({"pagesize", "pageregion"})
            if attrs.get("sides-supported"):
                primary.add("duplex")
            if attrs.get("print-color-mode-supported"):
                primary.add("colormodel")
            equivalent = {"Resolution": "printer-resolution", "cupsPrintQuality": "print-quality",
                          "MediaType": "media-type", "InputSlot": "media-source", "OutputBin": "output-bin"}
            forbidden_source_values = set()
            for group_label, group_options in groups:
                for item in group_options:
                    key = item.keyword
                    if not safe_print_option(key) or key.lower() in seen:
                        continue
                    choices = []
                    for choice in item.choices:
                        value = choice.get("choice")
                        if not isinstance(value, str) or value == "Custom":
                            continue
                        if blocked(key, value):
                            if key.lower() == "inputslot":
                                forbidden_source_values.add(re.sub(r"[^a-z0-9]", "", value.lower()))
                            continue
                        choices.append((value, choice.get("text", value)))
                    if key in {"Resolution", "SetResolution", "JCLResolution"}:
                        generic_resolution = next((option for option in result if option["key"] == "printer-resolution"), None)
                        if generic_resolution:
                            available = {choice["value"] for choice in generic_resolution["choices"]}
                            remaining = []
                            for value, text in choices:
                                normalized = resolution_codes.get((key, value)) or _resolution(value)
                                if normalized in available:
                                    aliases.setdefault("printer-resolution", {}).setdefault(normalized, {})[key] = value
                                else:
                                    remaining.append((value, text))
                            choices = remaining
                    elif key in equivalent:
                        generic_option = next((option for option in result if option["key"] == equivalent[key]), None)
                        if generic_option:
                            available = {re.sub(r"[^a-z0-9]", "", choice["value"].lower()): choice["value"] for choice in generic_option["choices"]}
                            known_names = {
                                "cupsPrintQuality": {"draft": "3", "low": "3", "normal": "4", "high": "5", "best": "5"},
                                "InputSlot": {"mptray": "bypasstray", "multipurpose": "bypasstray"},
                                "MediaType": {"plain": "stationery", "plainpaper": "stationery"},
                            }.get(key, {})
                            remaining = []
                            for value, text in choices:
                                normalized = re.sub(r"[^a-z0-9]", "", value.lower())
                                normalized = known_names.get(normalized, normalized)
                                if normalized in available:
                                    aliases.setdefault(equivalent[key], {}).setdefault(available[normalized], {key: value})
                                else:
                                    remaining.append((value, text))
                            choices = remaining
                    if key.lower() in {"pagesize", "pageregion"} & primary:
                        continue
                    if key.lower() == "duplex" and "duplex" in primary:
                        standard = {"none": "one-sided", "off": "one-sided", "false": "one-sided",
                                    "duplexnotumble": "two-sided-long-edge", "longedge": "two-sided-long-edge",
                                    "duplextumble": "two-sided-short-edge", "shortedge": "two-sided-short-edge"}
                        choices = [(value, text) for value, text in choices if value.lower() not in standard]
                    if key.lower() == "colormodel" and "colormodel" in primary:
                        supported_colors = attrs["print-color-mode-supported"]
                        standard_mono = {"gray", "grayscale", "black"} if "monochrome" in supported_colors else set()
                        color_values = [value.lower() for value, _ in choices if value.lower() in {"rgb", "cmyk", "color"}]
                        primary_color = (str(item.defchoice).lower() if str(item.defchoice).lower() in color_values else next(iter(color_values), "")) if "color" in supported_colors else ""
                        choices = [(value, text) for value, text in choices if value.lower() not in standard_mono and value.lower() != primary_color]
                    generic = next((option for option in result if option["key"] == equivalent.get(key)), None)
                    # Keep vendor controls whose choices include unique modes such as HQ1200.
                    if generic and {value for value, _ in choices} == {choice["value"] for choice in generic["choices"]}:
                        continue
                    label = option_label(key, item.text)
                    if any(option["label"] == label for option in result):
                        label += "（更多選項）"
                    choices = [(value, choice_label(value, text)) for value, text in choices]
                    category = "quality" if re.search(r"resolution|quality|toner|density|color|colour", key, re.I) else (
                        "paper" if re.search(r"media|inputslot|tray|paper|pagesize|pageregion", key, re.I) else "device")
                    option = _select(key, label, category, choices, item.defchoice)
                    if option:
                        result.append(option)
                        seen.add(key.lower())
            if forbidden_source_values:
                for option in result:
                    if option["key"] == "media-source":
                        option["choices"] = [choice for choice in option["choices"]
                                             if re.sub(r"[^a-z0-9]", "", choice["value"].lower()) not in forbidden_source_values]
                        if option["default"] not in {choice["value"] for choice in option["choices"]}:
                            option["default"] = ""
            return [option for option in result if option["choices"]], aliases
        except Exception:
            # Driverless queues may have no PPD, and a broken PPD must not hide IPP controls.
            return options, {}

    def printers(self):
        def read(conn):
            default = conn.getDefault()
            printers = []
            for name, summary in conn.getPrinters().items():
                attrs = conn.getPrinterAttributes(name)
                sides = attrs.get("sides-supported", ["one-sided"])
                colors = attrs.get("print-color-mode-supported")
                if not colors:
                    colors = ["monochrome", "color"] if attrs.get("color-supported", False) else ["monochrome"]
                media = [value for value in _values(attrs.get("media-supported"))
                         if isinstance(value, str) and OPTION_VALUE.fullmatch(value)
                         and not value.startswith(("custom_min_", "custom_max_"))]
                print_options, aliases = self._ppd_options(conn, name, attrs, _ipp_options(attrs))
                state = int(summary.get("printer-state", 3))
                supplies = _supplies(attrs)
                low = any(supply["low"] for supply in supplies)
                printers.append({
                    "name": name, "label": summary.get("printer-info") or name,
                    "_mode": "driverless" if re.search(r"driverless|IPP Everywhere", str(attrs.get("printer-make-and-model") or summary.get("printer-make-and-model") or ""), re.I) else "driver",
                    "_driver_name": str(attrs.get("printer-make-and-model") or summary.get("printer-make-and-model") or "")[:200],
                    "location": summary.get("printer-location", ""),
                    "state": state, "state_label": PRINTER_STATES.get(state, "未知"),
                    "accepting": bool(attrs.get("printer-is-accepting-jobs", True)),
                    "message": _printer_message(attrs.get("printer-state-message", summary.get("printer-state-message"))),
                    "is_default": name == default,
                    # "paused" is already the state label; repeating it adds nothing. A named low supply
                    # replaces the generic "ink or toner low" reason.
                    "reasons": [reason for reason in _reasons(attrs.get("printer-state-reasons", summary.get("printer-state-reasons")), False)
                                if reason != "已暫停" and not (low and reason in GENERIC_SUPPLY_REASONS)],
                    "supplies": supplies, "_supplies_time": _supply_time(attrs),
                    "capabilities": {
                        "sides": list(sides), "color_modes": list(colors), "media": list(media),
                        "default_sides": attrs.get("sides-default", "one-sided"),
                        "default_color": attrs.get("print-color-mode-default", colors[0]),
                        "default_media": attrs.get("media-default", media[0] if media else ""),
                        "print_options": print_options,
                        "option_aliases": aliases,
                        "presets": print_presets(print_options, colors),
                        "custom_media": _custom_media(attrs),
                    },
                })
            return self._group_printers(printers, default)
        return self._call(read)

    def jobs(self):
        def read(conn):
            result = []
            # CUPS lists the oldest jobs first; a server-side limit would hide new ones.
            jobs = conn.getJobs(which_jobs="all", my_jobs=True,
                                requested_attributes=["job-id", "job-name", "job-printer-uri", "job-state", "time-at-creation",
                                                      "time-at-processing", "time-at-completed", "job-originating-user-name", "job-state-reasons",
                                                      "job-printer-state-message", "job-printer-state-reasons"])
            ordered = sorted(jobs.items(), reverse=True)
            # Every unfinished job stays visible, however many newer ones finished since.
            selected = [(job_id, attrs) for index, (job_id, attrs) in enumerate(ordered)
                        if index < 50 or int(attrs.get("job-state", 3)) in ACTIVE_JOB_STATES]
            printers = conn.getPrinters() if any(int(attrs.get("job-state", 3)) in ACTIVE_JOB_STATES for _, attrs in selected) else {}
            for job_id, attrs in selected:
                state = int(attrs.get("job-state", 3))
                queue = urllib.parse.unquote(attrs.get("job-printer-uri", "").rsplit("/", 1)[-1])
                reasons = _job_reasons(attrs.get("job-state-reasons"))
                stopped = False
                if state in ACTIVE_JOB_STATES:
                    # A waiting or printing job is held up by whatever the printer reports now.
                    printer = printers.get(queue, {})
                    stopped = int(printer.get("printer-state", 3)) == 5
                    if state in {3, 4} and not stopped:
                        message, printer_reasons, attention = "", [], False
                    else:
                        message = printer.get("printer-state-message", "")
                        printer_reasons = _job_printer_reasons(printer.get("printer-state-reasons"))
                        attention = stopped or state == 6 or _printer_errors(printer.get("printer-state-reasons"))
                        if stopped:
                            printer_reasons.insert(0, JOB_REASONS["printer-stopped"])
                elif state in {7, 9} and not reasons:
                    message, printer_reasons, attention = "", [], False
                else:
                    # What the printer said when the job ended.
                    message = attrs.get("job-printer-state-message", "")
                    printer_reasons = _job_printer_reasons(attrs.get("job-printer-state-reasons"))
                    attention = True
                result.append({
                    "id": job_id, "title": attrs.get("job-name", "打印任務"),
                    "printer": self._logical_queue_name(queue),
                    "state": state, "state_label": JOB_STATES.get(state, "未知"),
                    "reasons": list(dict.fromkeys(printer_reasons + reasons))[:6],
                    "message": _printer_message(message),
                    "can_cancel": state in ACTIVE_JOB_STATES,
                    "can_hold": state in {3, 5, 6}, "can_release": state == 4, "attention": attention,
                    "printer_stopped": stopped,
                    # A paused job that had started prints again from the first page.
                    "started": bool(attrs.get("time-at-processing")),
                    "created_at": attrs.get("time-at-creation"),
                    "completed_at": attrs.get("time-at-completed") or None,
                    "user": attrs.get("job-originating-user-name", self.user),
                })
            return {"jobs": result}
        return self._call(read)

    def validate_job_options(self, printer, options):
        """Validate the whole driver selection before either print path."""
        temporary = None
        try:
            if not hasattr(self.cups, "PPD"):
                return
            try:
                temporary = self._connection().getPPD(printer)
            except Exception:
                # IPP-only queues have no PPD; capability validation still applies.
                return
            if self._native_ppd():
                reply = self._ppd_worker("validate", temporary, options=options)
                # Fetching a PPD succeeded: an unreadable PPD or a failed worker
                # cannot count as having checked its conflicts before printing.
                if reply.get("available") is not True or type(reply.get("conflict")) is not bool:
                    raise CupsError("無法核對打印機的驅動設定，請稍後再試。")
                if reply["conflict"]:
                    raise ValueError("所選的紙張、紙匣或列印設定不能同時使用，請調整其中一項，或改回「自動」。")
            else:
                self._validate_ppd_options_local(temporary, options)
        finally:
            if isinstance(temporary, (str, bytes, os.PathLike)):
                try:
                    Path(os.fsdecode(temporary)).unlink(missing_ok=True)
                except OSError:
                    pass

    def _validate_ppd_options_local(self, temporary, options):
        """Return False only when the native constructor cannot read a PPD."""
        try:
            ppd = self.cups.PPD(temporary)
        except Exception:
            return False
        all_options = {}

        def collect(groups):
            for group in groups:
                for item in group.options:
                    all_options[item.keyword] = item
                collect(group.subgroups)

        collect(ppd.optionGroups)
        ppd.markDefaults()
        aliases = {
            "sides": ("Duplex", {"one-sided": ["None", "Off", "False"],
                                  "two-sided-long-edge": ["DuplexNoTumble", "LongEdge"],
                                  "two-sided-short-edge": ["DuplexTumble", "ShortEdge"]}),
            "print-color-mode": ("ColorModel", {"monochrome": ["Gray", "Grayscale", "Black"], "color": ["RGB", "CMYK", "Color"]}),
            "print-quality": ("cupsPrintQuality", {"3": ["Draft", "Low"], "4": ["Normal"], "5": ["High", "Best"]}),
            "media-source": ("InputSlot", {"auto": ["Auto"], "main": ["Main", "Cassette", "Tray1"],
                                             "tray-1": ["Tray1"], "tray-2": ["Tray2"], "tray-3": ["Tray3"],
                                             "manual": ["Manual", "MPTray"], "by-pass-tray": ["MPTray", "MultiPurpose"]}),
            "media-type": ("MediaType", {"stationery": ["Plain", "PlainPaper"], "photographic-glossy": ["Glossy", "GlossyPaper"],
                                           "photographic-matte": ["Matte", "MattePaper"], "envelope": ["Envelope"]}),
        }
        # Generic aliases first, explicit driver selections last. Explicit
        # DPI aliases come from the PPD's actual HWResolution in capabilities.
        marked = {}
        for key, value in options.items():
            if key == "media" and "PageSize" in all_options and not value.startswith("Custom."):
                media_name = re.sub(r"^(?:iso|na|jis|jpn|oe|om)_", "", value).rsplit("_", 1)[0] if "_" in value else value
                aliases_for_value = [value, media_name, {"index-4x6": "4x6", "photo": "Photo"}.get(media_name, media_name)]
                alias_key = "PageSize"
            elif key in aliases:
                alias_key, mapping = aliases[key]
                aliases_for_value = mapping.get(value, [value])
            else:
                continue
            if alias_key not in all_options:
                continue
            choices = {choice["choice"].lower(): choice["choice"] for choice in all_options[alias_key].choices}
            selected = next((choices[name.lower()] for name in aliases_for_value if name.lower() in choices), None)
            if selected:
                marked[alias_key] = selected
        for key, value in options.items():
            if key in all_options:
                choices = {choice["choice"].lower(): choice["choice"] for choice in all_options[key].choices}
                if value.lower() in choices:
                    marked[key] = choices[value.lower()]
        for key, value in marked.items():
            ppd.markOption(key, value)
        if ppd.conflicts():
            raise ValueError("所選的紙張、紙匣或列印設定不能同時使用，請調整其中一項，或改回「自動」。")
        return True

    def print_file(self, printer, path, title, options):
        return self._call(lambda conn: conn.printFile(printer, str(path), title, options))

    def _own_job(self, conn, job_id, states, action):
        attrs = conn.getJobAttributes(job_id)
        if attrs.get("job-originating-user-name") != self.user:
            raise CupsError(f"只能{action}由家庭打印中心送出的任務。")
        state = int(attrs.get("job-state", 9))
        if state not in states:
            raise CupsError("此任務已結束。" if state > 6 else f"此任務現在無法{action}。")

    def cancel_job(self, job_id):
        def cancel(conn):
            self._own_job(conn, job_id, ACTIVE_JOB_STATES, "取消")
            conn.cancelJob(job_id)
        return self._call(cancel)

    def hold_job(self, job_id):
        """Pausing a job that is already printing stops it; on resume it starts again from the first page."""
        def hold(conn):
            self._own_job(conn, job_id, {3, 5, 6}, "暫停")
            conn.setJobHoldUntil(job_id, "indefinite")
        return self._call(hold)

    def release_job(self, job_id):
        def release(conn):
            self._own_job(conn, job_id, {4}, "繼續")
            conn.setJobHoldUntil(job_id, "no-hold")
        return self._call(release)

    # --- Administration (only reachable through the signed-in admin page) ---

    _ppd_lock = threading.Lock()
    _ppd_cache = None

    def ppds(self):
        """Installed drivers. Listing ~20,000 PPDs takes seconds, so keep it for 10 minutes."""
        with self._ppd_lock:
            if self._ppd_cache and time.monotonic() - self._ppd_cache[0] < 600:
                return self._ppd_cache[1]
        keys = ("ppd-make", "ppd-make-and-model", "ppd-device-id", "ppd-product", "ppd-natural-language")
        found = self._call(lambda conn: conn.getPPDs(), "無法讀取已安裝的驅動程式清單。")
        result = {name: {key: attrs.get(key) for key in keys} for name, attrs in found.items() if isinstance(name, str)}
        with self._ppd_lock:
            type(self)._ppd_cache = (time.monotonic(), result)
        return result

    def check_ppd(self, path):
        if not self.cups or not hasattr(self.cups, "PPD"):
            return
        try:
            self.cups.PPD(str(path))
        except Exception as exc:
            raise ValueError("驅動檔格式不正確，CUPS 無法讀取。") from exc

    def devices(self):
        """Printers attached by USB or announced over SNMP, as found by CUPS backends."""
        def read(conn):
            try:
                found = conn.getDevices(exclude_schemes=SKIPPED_BACKENDS, timeout=10)
            except TypeError:
                found = conn.getDevices(exclude_schemes=SKIPPED_BACKENDS)
            devices = []
            for uri, attrs in found.items():
                scheme, separator, rest = str(uri).partition(":")
                if scheme not in DEVICE_SCHEMES or not separator or not rest.strip("/"):
                    continue
                if scheme in {"socket", "lpd", "ipp", "ipps"}:
                    host = urllib.parse.urlsplit(uri).hostname or ""
                    try:
                        address = ipaddress.ip_address(host)
                        if not (address.is_private or address.is_link_local) or address.is_loopback:
                            continue
                    except ValueError:
                        if not host.lower().endswith((".local", ".lan", ".home.arpa")):
                            continue
                make_model = str(attrs.get("device-make-and-model") or "")
                info = str(attrs.get("device-info") or make_model or uri)
                if make_model.lower() == "unknown" and not attrs.get("device-id"):
                    continue
                devices.append({"label": info[:200], "service_name": "", "uri": uri,
                                "kind": "usb" if scheme in {"usb", "hp", "parallel"} else "network",
                                "driverless": False, "model_hint": make_model[:200],
                                "make_model": make_model[:200], "device_id": str(attrs.get("device-id") or "")[:500],
                                "identity": uri, "priority": 10})
            return devices
        return self._call(read, "無法列出 USB 或網路上的打印機。")

    def admin_printers(self):
        def read(conn):
            default = conn.getDefault()
            result = []
            records = self._profile_records()
            hidden = {queue for name, record in records.items() for queue in record["queues"].values() if queue != name}
            for name, attrs in conn.getPrinters().items():
                if name in hidden or is_companion_name(name):
                    continue
                try:
                    details = conn.getPrinterAttributes(name, requested_attributes=["printer-is-accepting-jobs", *MARKER_ATTRIBUTES])
                    accepting = bool(details.get("printer-is-accepting-jobs", True))
                except Exception:
                    details, accepting = {}, True
                # The other mode's queue may have printed more recently and so hold newer levels.
                for queue in records.get(name, {}).get("queues", {}).values():
                    if queue == name:
                        continue
                    try:
                        other = conn.getPrinterAttributes(queue, requested_attributes=MARKER_ATTRIBUTES)
                    except Exception:
                        continue
                    if _supply_time(other) > _supply_time(details):
                        details = other
                uri = str(attrs.get("device-uri") or "")
                state = int(attrs.get("printer-state", 3))
                reasons = _reasons(attrs.get("printer-state-reasons"))
                result.append({
                    "name": name, "info": attrs.get("printer-info") or "", "location": attrs.get("printer-location") or "",
                    "make_model": (attrs.get("printer-make-and-model") or "").strip(),
                    # Credentials can be embedded in device URIs; never send them to a browser.
                    "uri": re.sub(r"//[^/@]*@", "//", uri),
                    "connection": CONNECTIONS.get(uri.split(":", 1)[0], uri.split(":", 1)[0] or "未知"),
                    "state": state, "state_label": PRINTER_STATES.get(state, "未知"),
                    "message": attrs.get("printer-state-message") or "", "reasons": reasons,
                    "accepting": accepting, "is_default": default in records.get(name, {}).get("queues", {}).values() or name == default,
                    "default_mode": records.get(name, {}).get("default_mode"),
                    "print_modes": list(records.get(name, {}).get("queues", {})),
                    "supplies": _supplies(details), "supplies_time": _supply_time(details),
                })
            return {"printers": sorted(result, key=lambda item: item["name"].lower()), "default": self._logical_queue_name(default)}
        return self._call(read)

    @contextmanager
    def _driverless_ppd(self, uri):
        if not isinstance(uri, str) or not uri.startswith(("ipp://", "ipps://")):
            raise ValueError("免驅動 IPP 模式需要有效的打印服務位址，請重新探索打印機。")
        temporary = None
        try:
            result = subprocess.run(["driverless", uri], capture_output=True, timeout=35, check=False)
            if result.returncode or not result.stdout.startswith(b"*PPD-Adobe:") or len(result.stdout) > 2 * 1024 * 1024:
                raise CupsError("無法取得打印機的 IPP 相容設定，請確認打印機已開機後重新探索。")
            with tempfile.NamedTemporaryFile(suffix=".ppd", delete=False) as stream:
                stream.write(result.stdout)
                temporary = Path(stream.name)
            self.check_ppd(temporary)
            yield temporary
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CupsError("無法取得打印機的 IPP 相容設定，請確認打印機已開機後重新探索。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    _profiles_lock = threading.RLock()

    def add_profiled_printer(self, name, uri, driver, info="", location="", ppd_file=None,
                             make_default=False, driverless=False, ipp_uri=None):
        """Create all profiles once, keeping each mode's PPD fixed thereafter."""
        if not getattr(self, "profiles", None):
            raise ValueError("尚未設定打印模式儲存目錄。")
        with self._profiles_lock:
            current, old_default = self._call(lambda conn: (conn.getPrinters(), conn.getDefault()))
            queues = {"driverless" if driver == "everywhere" else "driver": name}
            if driver != "everywhere" and driverless:
                queues["driverless"] = companion_name(name)
            if any(queue.lower() in {existing.lower() for existing in current} for queue in queues.values()):
                raise ValueError("打印模式的隊列名稱已存在，請換一個打印機代號。")
            attempted = []
            try:
                with ExitStack() as stack:
                    compatibility_ppd = None
                    if "driverless" in queues:
                        compatibility_ppd = stack.enter_context(self._driverless_ppd(ipp_uri or uri))
                    attempted.append(name)
                    self.add_printer(name, uri, driver, info, location,
                                     compatibility_ppd if driver == "everywhere" else ppd_file, make_default)
                    if driver != "everywhere" and driverless:
                        compatible = queues["driverless"]
                        attempted.append(compatible)
                        self.add_printer(compatible, uri, "everywhere", info, location, compatibility_ppd, False)
                self.register_profiles(name, queues, "driver" if "driver" in queues else "driverless")
            except BaseException:
                def rollback(conn):
                    for queue in reversed(attempted):
                        try:
                            conn.deletePrinter(queue)
                        except Exception:
                            pass
                    if old_default:
                        conn.setDefault(old_default)
                try:
                    self._call(rollback)
                except CupsError:
                    pass
                raise
            return {"name": name}

    def add_printer(self, name, uri, driver, info="", location="", ppd_file=None, make_default=False):
        def add(conn):
            options = {"device": uri, "info": info, "location": location}
            if ppd_file:
                options["filename"] = str(ppd_file)
            else:
                # CUPS 2.4 builds an "everywhere" driver from the printer's own IPP answer.
                options["ppdname"] = driver
            conn.addPrinter(name, **options)
            conn.enablePrinter(name)
            conn.acceptJobs(name)
            if make_default or not conn.getDefault():
                conn.setDefault(name)
            return {"name": name}
        return self._call(add, "新增失敗：連不上打印機，或這個驅動不適用。請確認打印機已開機後再試，或換一個驅動。")

    def modify_printer(self, name, info=None, location=None, driver=None, ppd_file=None):
        def modify(conn):
            if ppd_file:
                conn.addPrinter(name, filename=str(ppd_file))
            elif driver:
                conn.addPrinter(name, ppdname=driver)
            if info is not None:
                conn.setPrinterInfo(name, info)
            if location is not None:
                conn.setPrinterLocation(name, location)
            return {"name": name}
        return self._call(modify, "儲存失敗：驅動不適用或打印服務沒有回應。")

    def modify_profiled_printer(self, name, info=None, location=None, driver=None, ppd_file=None,
                                driverless=False, ipp_uri=None):
        """Administrator edits keep a previously configured compatibility queue intact."""
        with self._profiles_lock:
            current = self._call(lambda conn: conn.getPrinters())
            if name not in current:
                raise ValueError("找不到這台打印機。")
            record = self._profile_records().get(name)
            if record:
                queues = dict(record["queues"])
            else:
                model = str(current[name].get("printer-make-and-model") or "")
                initial_mode = "driverless" if re.search(r"driverless|IPP Everywhere", model, re.I) else "driver"
                queues = {initial_mode: name}
            uri = str(current[name].get("device-uri") or "")
            created = []
            original_ppd = None
            changed_driver = None
            changed_labels = []
            try:
                if driver or ppd_file:
                    mode = "driverless" if driver == "everywhere" and not ppd_file else "driver"
                    target = queues.get(mode)
                    if target is None:
                        target = companion_name(name, mode)
                        if target.lower() in {item.lower() for item in current}:
                            raise ValueError("打印模式的隊列名稱已存在，請管理員檢查。")
                        created.append(target)
                        if mode == "driverless":
                            with self._driverless_ppd(ipp_uri or uri) as compatible:
                                self.add_printer(target, uri, "everywhere", current[name].get("printer-info", ""),
                                                 current[name].get("printer-location", ""), compatible, False)
                        else:
                            self.add_printer(target, uri, driver, current[name].get("printer-info", ""),
                                             current[name].get("printer-location", ""), ppd_file, False)
                        queues[mode] = target
                    elif mode == "driver":
                        # Preserve the old PPD until every profile and its map
                        # have been saved; later failures must not leave half
                        # of an administrator edit applied.
                        original_ppd = self._call(lambda conn: conn.getPPD(target))
                        if not isinstance(original_ppd, (str, bytes, os.PathLike)) or not Path(os.fsdecode(original_ppd)).is_file():
                            original_ppd = None
                            raise CupsError("無法保存原有驅動設定，未更改打印機；請管理員檢查 CUPS。")
                        changed_driver = target
                        self.modify_printer(target, driver=driver, ppd_file=ppd_file)
                if driverless and "driverless" not in queues:
                    target = companion_name(name)
                    if target.lower() in {item.lower() for item in current}:
                        raise ValueError("打印模式的隊列名稱已存在，請管理員檢查。")
                    created.append(target)
                    with self._driverless_ppd(ipp_uri or uri) as compatible:
                        self.add_printer(target, uri, "everywhere", current[name].get("printer-info", ""),
                                         current[name].get("printer-location", ""), compatible, False)
                    queues["driverless"] = target
                for queue in queues.values():
                    if info is not None or location is not None:
                        changed_labels.append(queue)
                        self.modify_printer(queue, info=info, location=location)
                self.register_profiles(name, queues, "driver" if "driver" in queues else "driverless")
            except BaseException as exc:
                rollback_failed = False
                if changed_driver and original_ppd:
                    try:
                        self.modify_printer(changed_driver, ppd_file=Path(os.fsdecode(original_ppd)))
                    except (CupsError, ValueError):
                        rollback_failed = True
                for queue in changed_labels:
                    if queue in current:
                        try:
                            self.modify_printer(queue, info=current[queue].get("printer-info", ""),
                                                location=current[queue].get("printer-location", ""))
                        except (CupsError, ValueError):
                            rollback_failed = True
                for queue in created:
                    try:
                        self._call(lambda conn, queue=queue: conn.deletePrinter(queue))
                    except CupsError:
                        rollback_failed = True
                if rollback_failed:
                    raise CupsError("儲存失敗，且未能完全回復原有設定；請管理員檢查 CUPS 後再試。") from exc
                raise
            finally:
                if isinstance(original_ppd, (str, bytes, os.PathLike)):
                    Path(os.fsdecode(original_ppd)).unlink(missing_ok=True)
            return {"name": name}

    def printer_action(self, name, action):
        record = self._profile_records().get(name)
        queues = list(record["queues"].values()) if record else [name]
        preferred = record["queues"][record["default_mode"]] if record else name
        def run(conn):
            if action == "default":
                conn.setDefault(preferred)
            elif action == "pause":
                for queue in queues:
                    conn.disablePrinter(queue)
            elif action == "resume":
                for queue in queues:
                    conn.enablePrinter(queue)
                    conn.acceptJobs(queue)
            elif action == "clear":
                for queue in queues:
                    conn.cancelAllJobs(name=queue, my_jobs=False, purge_jobs=False)
            elif action == "test":
                return {"job_id": conn.printTestPage(preferred)}
            elif action == "delete":
                # Keep the visible queue until all companions are removed, so a
                # failed deletion remains manageable from the administrator page.
                for queue in sorted(queues, key=lambda queue: queue == name):
                    conn.deletePrinter(queue)
            else:
                raise ValueError("不支援這個操作。")
            return {"ok": True}
        result = self._call(run, "操作失敗，請確認打印服務正常後再試。")
        if action == "delete" and record:
            self.profiles.remove(name)
        return result

    def all_jobs(self):
        def read(conn):
            jobs = conn.getJobs(which_jobs="all", my_jobs=False,
                                requested_attributes=["job-id", "job-name", "job-printer-uri", "job-state",
                                                      "time-at-creation", "job-originating-user-name"])
            result = []
            for job_id, attrs in sorted(jobs.items(), reverse=True)[:100]:
                state = int(attrs.get("job-state", 3))
                result.append({
                    "id": job_id, "title": attrs.get("job-name", "打印任務"),
                    "printer": self._logical_queue_name(urllib.parse.unquote(attrs.get("job-printer-uri", "").rsplit("/", 1)[-1])),
                    "state": state, "state_label": JOB_STATES.get(state, "未知"),
                    "can_cancel": state in {3, 4, 5, 6}, "created_at": attrs.get("time-at-creation"),
                    "user": attrs.get("job-originating-user-name", ""),
                })
            return {"jobs": result}
        return self._call(read)

    def cancel_any_job(self, job_id):
        def cancel(conn):
            attrs = conn.getJobAttributes(job_id)
            if int(attrs.get("job-state", 9)) not in {3, 4, 5, 6}:
                raise CupsError("此任務已結束，無需取消。")
            conn.cancelJob(job_id)
        return self._call(cancel)
