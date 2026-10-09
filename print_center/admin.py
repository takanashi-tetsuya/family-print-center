"""Administrator login and printer setup helpers: driver matching, PPD checks and probing."""
import gzip
import hashlib
import http.client
import functools
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

SESSION_LIFETIME = 8 * 3600
SESSION_IDLE = 3600
FAILURE_WINDOW = 15 * 60
FAILURES_PER_ADDRESS = 5
FAILURES_TOTAL = 50
PASSWORD_MIN = 8
PASSWORD_MAX = 128
QUEUE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class LoginBlocked(Exception):
    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = max(1, int(seconds))


def _write_private(path, text):
    """Write atomically with owner-only permissions; secrets never pass through umask defaults."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class AdminAuth:
    """One administrator password, stored as a salted hash in DATA_DIR/admin.json.

    Sessions live in Flask's signed cookie. A random epoch stored with the hash is
    copied into the session, so changing the password signs out every browser.
    """

    def __init__(self, data_dir, password=None, logger=None):
        self.path = Path(data_dir) / "admin.json"
        self.initial_path = Path(data_dir) / "admin-initial-password"
        self.lock = threading.Lock()
        self.failures = {}
        self.logger = logger
        self.record = self._load(password)

    def _load(self, password):
        try:
            record = json.loads(self.path.read_text())
            if isinstance(record, dict) and isinstance(record.get("hash"), str) and isinstance(record.get("epoch"), str):
                return record
            raise ValueError("invalid administrator record")
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # Refuse logins rather than silently resetting to a new password.
            if self.logger:
                self.logger.error("管理後台設定檔 %s 損壞；刪除後重新啟動可產生新的初始密碼。", self.path)
            return None
        generated = not password
        password = password or secrets.token_urlsafe(9)
        record = {"hash": generate_password_hash(password), "epoch": secrets.token_hex(8), "must_change": generated}
        if generated:
            _write_private(self.initial_path, password + "\n")
            if self.logger:
                self.logger.warning("管理後台初始密碼已寫入 %s，登入後請立即更改。", self.initial_path)
        _write_private(self.path, json.dumps(record))
        return record

    def _prune(self, now):
        for address in list(self.failures):
            recent = [moment for moment in self.failures[address] if now - moment < FAILURE_WINDOW]
            if recent:
                self.failures[address] = recent
            else:
                del self.failures[address]

    def _attempt(self, address):
        """Count the attempt before hashing so parallel guesses cannot skip the limit."""
        now = time.time()
        with self.lock:
            self._prune(now)
            mine = self.failures.get(address, [])
            if len(mine) >= FAILURES_PER_ADDRESS:
                raise LoginBlocked(FAILURE_WINDOW - (now - mine[0]))
            everyone = [moment for moments in self.failures.values() for moment in moments]
            if len(everyone) >= FAILURES_TOTAL:
                raise LoginBlocked(FAILURE_WINDOW - (now - min(everyone)))
            self.failures.setdefault(address, []).append(now)
            return self.record

    def _verify(self, record, password):
        return (record is not None and isinstance(password, str) and 0 < len(password) <= PASSWORD_MAX
                and check_password_hash(record["hash"], password))

    def login(self, session, password, address):
        record = self._attempt(address)
        if not self._verify(record, password):
            return False
        with self.lock:
            self.failures.pop(address, None)
        self._start(session, record["epoch"])
        return True

    def _start(self, session, epoch):
        now = int(time.time())
        session["admin"] = {"epoch": epoch, "since": now, "seen": now}
        # A new privilege level gets a new form token.
        session["csrf"] = secrets.token_urlsafe(32)

    def logout(self, session):
        session.pop("admin", None)
        session["csrf"] = secrets.token_urlsafe(32)

    def current(self, session):
        info = session.get("admin")
        record = self.record
        if not isinstance(info, dict) or record is None:
            return None
        now = int(time.time())
        since, seen = info.get("since"), info.get("seen")
        if (info.get("epoch") != record["epoch"] or type(since) is not int or type(seen) is not int
                or now - since > SESSION_LIFETIME or now - seen > SESSION_IDLE or seen > now + 60):
            session.pop("admin", None)
            return None
        if now - seen > 60:
            session["admin"] = {**info, "seen": now}
        return {"must_change": bool(record.get("must_change"))}

    def change_password(self, session, current, new, address):
        record = self._attempt(address)
        if not self._verify(record, current):
            raise ValueError("目前的密碼不正確。")
        with self.lock:
            self.failures.pop(address, None)
        if not isinstance(new, str) or not PASSWORD_MIN <= len(new) <= PASSWORD_MAX:
            raise ValueError(f"新密碼需要 {PASSWORD_MIN} 到 {PASSWORD_MAX} 個字元。")
        if new == current:
            raise ValueError("新密碼不能與目前的密碼相同。")
        if len(set(new)) < 4:
            raise ValueError("新密碼太簡單，請混合不同的字元。")
        updated = {"hash": generate_password_hash(new), "epoch": secrets.token_hex(8), "must_change": False}
        _write_private(self.path, json.dumps(updated))
        self.record = updated
        self.initial_path.unlink(missing_ok=True)
        self._start(session, updated["epoch"])


def clean_text(value, limit=127):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("文字欄位格式不正確。")
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()
    if len(value) > limit:
        raise ValueError(f"文字欄位最多 {limit} 個字。")
    return value


def queue_name(label, existing):
    """CUPS queue names are ASCII and case-insensitive; the friendly name goes in info."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", label or "").strip("_")[:40]
    if not base:
        base = "printer"
    elif not base[0].isalpha():
        base = "printer_" + base
    taken = {name.lower() for name in existing}
    name, number = base, 2
    while name.lower() in taken:
        name = f"{base}_{number}"
        number += 1
    return name


# --- Driver matching ---------------------------------------------------------

MAKE_ALIASES = {
    "hewlett-packard": "hp", "hewlett packard": "hp", "hp inc": "hp",
    "kyocera mita": "kyocera", "kyocera document solutions": "kyocera",
    "konica minolta business solutions": "konica minolta", "minolta-qms": "minolta",
    "fujifilm business innovation": "fujifilm", "oki data": "oki", "okidata": "oki", "oki data corp": "oki",
    "lexmark international": "lexmark", "samsung electronics": "samsung", "seiko epson": "epson",
    "epson corporation": "epson", "brother industries": "brother", "canon inc": "canon",
    "ricoh company": "ricoh", "dell inc": "dell", "utax/ta": "utax", "ta triumph-adler": "triumph-adler",
}
GENERIC_POSTSCRIPT = "drv:///sample.drv/generic.ppd"
GENERIC_PCL = "drv:///sample.drv/generpcl.ppd"
DRIVER_NOISE = re.compile(
    r"(?:,|\s-\s|\s+(?:foomatic/|for cups|using |cups\+gutenprint|hpcups|hpijs|pxl|pcl|ps\b|postscript|br-script|"
    r"brscript|kpdl|cups\b|\(recommended\))).*$", re.I)


@functools.lru_cache(maxsize=4096)
def normalize_make(value):
    text = re.sub(r"\s+", " ", str(value or "").strip().lower()).strip(" .,")
    text = re.sub(r",?\s+(?:inc|corp|corporation|co\.?,?\s*ltd|ltd|limited)\.?$", "", text)
    if text in MAKE_ALIASES:
        return MAKE_ALIASES[text]
    for alias, make in MAKE_ALIASES.items():
        if text.startswith(alias + " "):
            return make
    return text


def parse_device_id(value):
    """IEEE 1284 device ID: MFG:Brother;MDL:MFC-L2685DW;CMD:PJL,PCL,URF;"""
    names = {"MANUFACTURER": "MFG", "MODEL": "MDL", "COMMAND SET": "CMD", "DESCRIPTION": "DES"}
    fields = {}
    for part in str(value or "").split(";"):
        key, separator, item = part.partition(":")
        key = names.get(key.strip().upper(), key.strip().upper())
        if separator and key and item.strip() and key not in fields:
            fields[key] = item.strip()
    return fields


@functools.lru_cache(maxsize=256)
def _make_prefixes(make):
    """Squashed spellings of a brand, longest first: hewlettpackard, hp."""
    normalized = normalize_make(make)
    names = {str(make or "").strip().lower(), normalized} | {alias for alias, target in MAKE_ALIASES.items() if target == normalized}
    return tuple(sorted({re.sub(r"[^a-z0-9]", "", name) for name in names} - {""}, key=len, reverse=True))


def model_key(make, model):
    text = re.sub(r"\b(?:series|printer)\b", " ", str(model or "").lower())
    text = re.sub(r"[^a-z0-9]", "", text)
    # "Hewlett-Packard HP LaserJet" and "hp-laserjet_pro" repeat the brand; strip every leading copy.
    prefixes = _make_prefixes(make)
    stripped = True
    while stripped:
        stripped = False
        for prefix in prefixes:
            if text.startswith(prefix) and len(text) > len(prefix):
                text, stripped = text[len(prefix):], True
                break
    return text


def build_driver_index(ppds):
    index = {}
    for name, attrs in ppds.items():
        # CUPS exposes generated IPP PPDs alongside installed drivers. Their
        # matching model name is not evidence of a vendor/model-specific driver.
        if name.startswith("driverless:") or re.search(r"\bdriverless\b|IPP Everywhere", str(attrs.get("ppd-make-and-model") or ""), re.I):
            continue
        ids = parse_device_id(attrs.get("ppd-device-id"))
        make = normalize_make(attrs.get("ppd-make") or ids.get("MFG"))
        label = str(attrs.get("ppd-make-and-model") or name)
        keys = {model_key(make, ids.get("MDL"))}
        keys.add(model_key(make, DRIVER_NOISE.sub("", label)))
        for product in attrs.get("ppd-product") or []:
            keys.add(model_key(make, str(product).strip("()")))
        keys.discard("")
        index.setdefault(make, []).append({"id": name, "label": label, "keys": keys,
                                           "language": str(attrs.get("ppd-natural-language") or "")})
    return index


def _common_prefix(left, right):
    size = 0
    for a, b in zip(left, right):
        if a != b:
            break
        size += 1
    # M402 and M404 share "m40", but they are different models.
    while size and any(size < len(text) and text[size].isdigit() and text[size - 1].isdigit() for text in (left, right)):
        size -= 1
    return size


def device_identity(device):
    """Collect make, model keys and command set from whatever the source reported."""
    ids = parse_device_id(device.get("device_id"))
    make_model = str(device.get("make_model") or device.get("model_hint") or device.get("label") or "")
    make = normalize_make(ids.get("MFG") or make_model.split(" ", 1)[0])
    keys = {model_key(make, ids.get("MDL")), model_key(make, DRIVER_NOISE.sub("", make_model)), model_key(make, ids.get("DES"))}
    keys.discard("")
    commands = {item.strip().upper() for item in ids.get("CMD", "").split(",") if item.strip()}
    commands |= {item.strip().upper() for item in str(device.get("pdl") or "").split(",") if item.strip()}
    return make, keys, commands


def driver_candidates(device, index, ppds, limit=6):
    make, keys, commands = device_identity(device)
    found = []
    if device.get("driverless"):
        found.append({"id": "everywhere", "label": "免驅動 IPP（相容）", "match": "driverless", "score": 90})
    for entry in index.get(make, []) if keys else []:
        if keys & entry["keys"]:
            score, match = 200, "exact"
        else:
            best = 0
            for key in keys:
                for candidate in entry["keys"]:
                    # Same series and model number, different suffix: M404dn ~ M404-M405.
                    prefix = _common_prefix(key, candidate)
                    ratio = prefix / max(len(key), len(candidate))
                    if prefix >= 5 and ratio >= 0.7 and re.search(r"\d", key[:prefix]):
                        best = max(best, ratio)
            if not best:
                continue
            score, match = 40 + int(20 * best), "similar"
        label = entry["label"].lower()
        score += 6 if "(recommended)" in label else 0
        score += 2 if entry["language"] in {"en", ""} else -3
        score -= 2 if "simplified" in label else 0
        found.append({"id": entry["id"], "label": entry["label"], "match": match, "score": score})
    postscript = any(re.search(r"POSTSCRIPT|^PS\d?$|BR-?SCRIPT|KPDL", command) for command in commands)
    pcl = any(command.startswith(("PCL", "PXL")) or "PCL" in command for command in commands)
    if postscript or not commands:
        found.append({"id": GENERIC_POSTSCRIPT, "label": "通用 PostScript 驅動", "match": "generic", "score": 96 if postscript and device.get("driverless") else 12})
    if pcl or not commands:
        found.append({"id": GENERIC_PCL, "label": "通用 PCL 雷射驅動", "match": "generic", "score": (95 if postscript else 96) if pcl and device.get("driverless") else 11 if postscript else 13})
    result, seen = [], set()
    for item in sorted(found, key=lambda item: (-item["score"], item["label"].lower())):
        # The same driver is often installed twice (e.g. vendor and system copies).
        if item["label"].lower() in seen or (item["id"] not in ppds and item["id"] != "everywhere"):
            continue
        seen.add(item["label"].lower())
        result.append(item)
        if len(result) >= limit:
            break
    return result


def automatic_driver(device, index, ppds):
    """Choose an exact model or an explicitly announced print language only."""
    _, _, commands = device_identity(device)
    candidates = driver_candidates(device, index, ppds)
    for candidate in candidates:
        if candidate["match"] == "exact":
            return candidate["id"]
    postscript = any(re.search(r"POSTSCRIPT|^PS\d?$|BR-?SCRIPT|KPDL", command) for command in commands)
    pcl = any(command.startswith(("PCL", "PXL")) or "PCL" in command for command in commands)
    for candidate in candidates:
        if ((candidate["id"] == GENERIC_POSTSCRIPT and postscript)
                or (candidate["id"] == GENERIC_PCL and pcl)):
            return candidate["id"]
    return "everywhere" if device.get("driverless") else None


def search_drivers(ppds, query, limit=60):
    words = [word for word in re.split(r"\s+", str(query or "").strip().lower()) if word][:6]
    if not words:
        return []
    compact = [re.sub(r"[^a-z0-9]", "", word) for word in words]
    result = []
    for name, attrs in ppds.items():
        label = str(attrs.get("ppd-make-and-model") or name)
        if name.startswith("driverless:") or re.search(r"\bdriverless\b|IPP Everywhere", label, re.I):
            continue
        text = label.lower()
        squashed = re.sub(r"[^a-z0-9]", "", text)
        if all(word in text or (key and key in squashed) for word, key in zip(words, compact)):
            result.append({"id": name, "label": label,
                           "recommended": "(recommended)" in text,
                           "language": str(attrs.get("ppd-natural-language") or "")})
    result.sort(key=lambda item: (not item["recommended"], item["language"] not in {"en", ""}, item["label"].lower()))
    unique, seen = [], set()
    for item in result:
        if item["label"].lower() not in seen:
            seen.add(item["label"].lower())
            unique.append({"id": item["id"], "label": item["label"]})
    return unique[:limit]


# --- Uploaded PPD files ------------------------------------------------------

PPD_LIMIT = 2 * 1024 * 1024
# Filters and Foomatic command lines run programs inside CUPS. Uploaded files may
# only describe a printer; anything that needs a filter must be installed as a package.
PPD_FORBIDDEN = re.compile(rb"(?im)^\*\s*(?:cupsFilter2?|cupsPreFilter|cupsPortMonitor|Include|cupsCommands|"
                           rb"APPrinterIconPath|cupsICCProfile|cupsSNMPSupplies)\b|foomatic")
UPLOAD_ID = re.compile(r"upload:([0-9a-f]{24})\Z")


def validate_ppd(data):
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                data = stream.read(PPD_LIMIT + 1)
        except (OSError, EOFError) as exc:
            raise ValueError("驅動檔無法解壓縮。") from exc
    if len(data) > PPD_LIMIT:
        raise ValueError("驅動檔太大，上限為 2 MB。")
    if not data.startswith(b"*PPD-Adobe:"):
        raise ValueError("這不是打印機驅動描述檔（PPD）。")
    if b"\x00" in data:
        raise ValueError("驅動檔內容不正確。")
    if PPD_FORBIDDEN.search(data):
        raise ValueError("此驅動檔需要額外的過濾程式，無法直接上傳；請改為在伺服器安裝廠商提供的驅動套件。")
    text = data.decode("latin-1")
    nickname = re.search(r'(?m)^\*(?:NickName|ModelName):\s*"([^"\r\n]{1,200})"', text)
    if not nickname:
        raise ValueError("驅動檔缺少型號名稱（NickName）。")
    return data, re.sub(r"[\x00-\x1f\x7f]", "", nickname[1]).strip()


class PpdStore:
    def __init__(self, data_dir, limit=30):
        self.root = Path(data_dir) / "ppds"
        self.limit = limit

    def _file(self, driver_id):
        match = UPLOAD_ID.fullmatch(driver_id) if isinstance(driver_id, str) else None
        if not match:
            raise ValueError("請選擇已上傳的驅動檔。")
        return self.root / f"{match[1]}.ppd"

    def path(self, driver_id):
        path = self._file(driver_id)
        if not path.is_file():
            raise ValueError("找不到這個已上傳的驅動檔，請重新上傳。")
        return path

    def items(self):
        if not self.root.is_dir():
            return []
        result = []
        for path in sorted(self.root.glob("*.ppd"), key=lambda item: item.stat().st_mtime, reverse=True):
            if not re.fullmatch(r"[0-9a-f]{24}\.ppd", path.name):
                continue
            head = path.read_bytes()[:PPD_LIMIT].decode("latin-1")
            nickname = re.search(r'(?m)^\*(?:NickName|ModelName):\s*"([^"\r\n]{1,200})"', head)
            result.append({"id": f"upload:{path.stem}", "label": nickname[1] if nickname else path.stem})
        return result

    def save(self, data, check=None):
        data, label = validate_ppd(data)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(list(self.root.glob("*.ppd"))) >= self.limit:
            raise ValueError("已上傳的驅動檔太多，請先刪除不再使用的檔案。")
        identifier = secrets.token_hex(12)
        path = self.root / f"{identifier}.ppd"
        _write_private(path, data.decode("latin-1"))
        try:
            if check:
                check(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"id": f"upload:{identifier}", "label": label}

    def delete(self, driver_id):
        self._file(driver_id).unlink(missing_ok=True)


# --- Manual network printers -------------------------------------------------

PROTOCOLS = {"ipp": 631, "ipps": 631, "socket": 9100, "lpd": 515}
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".home", ".internal")
HOST_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*\.?")
PATH = re.compile(r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*")


def allowed_address(address):
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return False
    return address.is_private or address.is_link_local


def resolve_printer_host(value, resolver=socket.getaddrinfo):
    """Return (host for URIs, one checked address). Only home-network targets are allowed."""
    if not isinstance(value, str) or not value.strip() or len(value) > 253:
        raise ValueError("請輸入打印機的 IP 位址或名稱。")
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        address = None
    if address is not None:
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
            text = str(address)
        if not allowed_address(address):
            raise ValueError("只能新增家庭網路內的打印機（例如 192.168.x.x）。")
        label = f"[{text}]" if address.version == 6 else text
        return label, str(address)
    if not HOST_NAME.fullmatch(text):
        raise ValueError("打印機名稱格式不正確。")
    name = text.lower().rstrip(".")
    if "." in name and not name.endswith(LOCAL_SUFFIXES):
        raise ValueError("只能使用家庭網路內的名稱（例如 printer.local），或直接輸入 IP 位址。")
    try:
        results = resolver(name, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ValueError("找不到這個名稱，請改用打印機的 IP 位址。") from exc
    addresses = []
    for *_, sockaddr in results:
        try:
            addresses.append(ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]))
        except ValueError:
            continue
    if not addresses or not all(allowed_address(item) for item in addresses):
        raise ValueError("這個名稱沒有指向家庭網路內的位址。")
    return name, str(addresses[0])


def build_uri(protocol, host, port=None, path=None):
    if protocol not in PROTOCOLS:
        raise ValueError("請選擇連線方式。")
    if port in (None, ""):
        port = PROTOCOLS[protocol]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("連接埠必須是 1 到 65535 的數字。")
    path = (path or "").strip().strip("/")
    if path and (not PATH.fullmatch(path) or len(path) > 128 or ".." in path.split("/")):
        raise ValueError("路徑只可包含英文字母、數字及 . _ - / 符號。")
    if protocol == "socket":
        return f"socket://{host}:{port}"
    if protocol == "lpd":
        if not path:
            raise ValueError("舊式 LPD 連線需要填寫佇列名稱（說明書中常見 lp、BINARY_P1）。")
        return f"lpd://{host}:{port}/{path}"
    return f"{protocol}://{host}:{port}/{path or 'ipp/print'}"


IPP_ATTRIBUTES = ["printer-make-and-model", "printer-device-id", "document-format-supported",
                  "urf-supported", "printer-info", "printer-name"]


def _ipp_value(tag, name, value):
    name, value = name.encode(), value.encode()
    return bytes([tag]) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value


def ipp_request(uri):
    body = b"\x02\x00" + struct.pack(">HI", 0x000B, 1) + b"\x01"
    body += _ipp_value(0x47, "attributes-charset", "utf-8")
    body += _ipp_value(0x48, "attributes-natural-language", "en")
    body += _ipp_value(0x45, "printer-uri", uri)
    for position, attribute in enumerate(IPP_ATTRIBUTES):
        body += _ipp_value(0x44, "" if position else "requested-attributes", attribute)
    return body + b"\x03"


def parse_ipp(data):
    """Read string attributes of a Get-Printer-Attributes response; ignore everything else."""
    if len(data) < 8:
        raise ValueError("short IPP response")
    status = struct.unpack(">H", data[2:4])[0]
    position, name, values = 8, "", {}
    while position < len(data):
        tag = data[position]
        position += 1
        if tag == 0x03:
            break
        if tag < 0x10:
            continue
        if position + 2 > len(data):
            raise ValueError("truncated IPP response")
        size = struct.unpack(">H", data[position:position + 2])[0]
        position += 2
        if size:
            name = data[position:position + size].decode("utf-8", "replace")
        position += size
        if position + 2 > len(data):
            raise ValueError("truncated IPP response")
        size = struct.unpack(">H", data[position:position + 2])[0]
        position += 2
        value = data[position:position + size]
        position += size
        if tag in {0x41, 0x42, 0x44, 0x45, 0x49} and name in IPP_ATTRIBUTES and len(values.get(name, [])) < 64:
            values.setdefault(name, []).append(value.decode("utf-8", "replace")[:500])
    return status, values


def _ipp_query(address, host, port, path, secure, timeout):
    uri = f"{'ipps' if secure else 'ipp'}://{host}:{port}/{path}"
    if secure:
        context = ssl.create_default_context()
        # Printers ship self-signed certificates; this request only reads the model name.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(address, port, timeout=timeout, context=context)
    else:
        connection = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        connection.request("POST", "/" + path, body=ipp_request(uri),
                           headers={"Content-Type": "application/ipp", "Host": f"{host}:{port}"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return None
        status, values = parse_ipp(data)
        return values if status < 0x0100 else None
    except (OSError, ValueError, http.client.HTTPException, struct.error):
        return None
    finally:
        connection.close()


def _port_open(address, port, timeout):
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_printer(value, resolver=socket.getaddrinfo, timeout=4):
    """Look at a typed address: which print ports answer, and what IPP says the model is."""
    host, address = resolve_printer_host(value, resolver)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ports = dict(zip((631, 443, 9100, 515), pool.map(lambda port: _port_open(address, port, 2), (631, 443, 9100, 515))))
    details, ipp = {}, None
    if ports[631] or ports[443]:
        attempts = [(631, False), (631, True), (443, True)] if ports[631] else [(443, True)]
        for port, secure in attempts:
            if port == 631 and not ports[631]:
                continue
            for path in ("ipp/print", "ipp", "ipp/printer"):
                values = _ipp_query(address, host, port, path, secure, timeout)
                if values is not None:
                    details, ipp = values, ("ipps" if secure else "ipp", port, path)
                    break
            if ipp:
                break
    formats = {item.lower() for item in details.get("document-format-supported", [])}
    driverless = bool(ipp) and (bool(details.get("urf-supported")) or bool(
        formats & {"application/pdf", "image/pwg-raster", "application/pclm"}))
    make_model = (details.get("printer-make-and-model") or [""])[0]
    connections = []
    if ipp:
        connections.append({"protocol": ipp[0], "port": ipp[1], "path": ipp[2], "label": "網路列印（建議）",
                            "uri": build_uri(ipp[0], host, ipp[1], ipp[2])})
    if ports[9100]:
        connections.append({"protocol": "socket", "port": 9100, "path": "", "label": "直接傳送（9100 埠）",
                            "uri": build_uri("socket", host, 9100)})
    if ports[515]:
        connections.append({"protocol": "lpd", "port": 515, "path": "lp", "label": "舊式網路列印（LPD）",
                            "uri": build_uri("lpd", host, 515, "lp")})
    return {"host": host, "make_model": make_model[:200],
            "device_id": (details.get("printer-device-id") or [""])[0][:500],
            "pdl": ",".join(sorted(formats))[:500], "driverless": driverless,
            "connections": connections}


def device_id_for(uri):
    return hashlib.sha256(uri.encode()).hexdigest()[:24]
