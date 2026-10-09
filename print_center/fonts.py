"""Fonts that Office documents ask for but the server lacks, and the administrator's own font files.

LibreOffice reports, while it lays out a preview, which font each requested name
was drawn with. Names the server has, and substitutes with identical character
widths, are dropped; the rest become a quiet note on the document and a count on
the admin page. Uploaded fonts live in the data directory and reach LibreOffice
through a small fontconfig file that also includes the system configuration.
"""
import hashlib
import json
import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape

from .office_runtime import office_environment

LOGGER = logging.getLogger(__name__)
FONT_EXTENSIONS = {"ttf", "otf", "ttc", "otc"}
# TrueType, Apple TrueType, OpenType/CFF and font collections.
FONT_SIGNATURES = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")
FONT_ID = re.compile(r"[0-9a-f]{24}")
FONT_LIMIT_MB = 64
MAX_FONTS = 60
TOTAL_LIMIT_MB = 512
MISSING_LIMIT = 40
MISSING_DAYS = 90
ALIAS_FILES = (Path("/etc/fonts/conf.d/60-family-print-center.conf"),
               Path(__file__).resolve().parent.parent / "deploy" / "fonts" / "60-family-print-center.conf")
# Substitutes with the same character widths (fontconfig's metric aliases): lines
# and pages break exactly as in Office, so nothing is worth mentioning.
SAME_WIDTHS = {
    "arial": {"liberationsans", "arimo"},
    "arialnarrow": {"liberationsansnarrow"},
    "helvetica": {"liberationsans", "arimo", "nimbussans", "nimbussansl", "texgyreheros"},
    "timesnewroman": {"liberationserif", "tinos"},
    "times": {"liberationserif", "tinos", "nimbusroman", "nimbusromanno9l", "texgyretermes"},
    "couriernew": {"liberationmono", "cousine"},
    "courier": {"liberationmono", "cousine", "nimbusmonops", "nimbusmonol", "texgyrecursor"},
    "calibri": {"carlito"},
    "cambria": {"caladea"},
    "georgia": {"gelasio"},
}
# Bullets in Word lists: LibreOffice draws these symbol fonts with its own OpenSymbol.
SYMBOL_FONTS = {"symbol", "wingdings", "wingdings2", "wingdings3", "webdings", "mtextra"}
# One design under another platform's name or in its GB2312/GBK editions: drawing one with
# another is the requested font itself. Other names our mapping tries first (華文楷体 → 楷体)
# are different designs and still worth a note.
SAME_FONTS = (
    frozenset({"標楷體", "dfkai-sb", "biaukai"}),
    frozenset({"楷体", "kaiti", "楷体_gb2312", "kaiti_gb2312"}),
    frozenset({"仿宋", "fangsong", "仿宋_gb2312", "fangsong_gb2312"}),
    frozenset({"方正黑体简体", "fzhei-b01s", "方正黑体_gbk", "fzhei-b01"}),
    frozenset({"方正书宋简体", "fzshusong-z01s", "方正书宋_gbk", "fzshusong-z01"}),
    frozenset({"方正小标宋简体", "fzxiaobiaosong-b05s", "方正小标宋_gbk", "fzxiaobiaosong-b05"}),
    frozenset({"方正楷体简体", "fzkai-z03s", "方正楷体_gbk", "fzkai-z03"}),
    frozenset({"方正仿宋简体", "fzfangsong-z02s", "方正仿宋_gbk", "fzfangsong-z02"}),
)


def normalize(name):
    # fontconfig compares family names ignoring case and spaces.
    return "".join(name.split()).casefold()


def same_font(key):
    """Every normalized name of the design `key` names, or nothing when it has no other names."""
    return next((group for group in SAME_FONTS if key in group), frozenset())


def clean_name(value):
    if not isinstance(value, str):
        return ""
    text = "".join(character for character in value if character.isprintable()).strip()
    return text if 0 < len(text) <= 80 else ""


class FontError(ValueError):
    pass


class FontStore:
    def __init__(self, data_dir, run=subprocess.run):
        self.root = Path(data_dir) / "fonts"
        self.files = self.root / "files"
        self.meta = self.root / "meta"
        self.run = run
        self.lock = threading.RLock()
        self._families = None
        self._aliases = None
        self._write_config()

    # --- fontconfig -------------------------------------------------------

    def _font_files(self):
        if not self.files.is_dir():
            return []
        return sorted(path for path in self.files.iterdir()
                      if FONT_ID.fullmatch(path.stem) and path.suffix[1:] in FONT_EXTENSIONS)

    def _write_config(self):
        """Uploaded fonts are added on top of the system configuration, never instead of it."""
        config = self.root / "fonts.conf"
        with self.lock:
            if not self._font_files():
                config.unlink(missing_ok=True)
                return
            # The private cache directory comes first so LibreOffice, whatever HOME it
            # runs with, finds the scan of the uploaded fonts instead of repeating it.
            text = ('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n<fontconfig>\n'
                    f'  <dir>{escape(str(self.files.resolve()))}</dir>\n'
                    f'  <cachedir>{escape(str((self.root / "cache").resolve()))}</cachedir>\n'
                    '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n</fontconfig>\n')
            temporary = config.with_suffix(".tmp")
            temporary.write_text(text)
            temporary.replace(config)

    def environment(self):
        """Extra environment for every process that lays out documents (LibreOffice, fc-*)."""
        config = self.root / "fonts.conf"
        return {"FONTCONFIG_FILE": str(config.resolve())} if config.is_file() else {}

    def _tool(self, command, timeout):
        executable = shutil.which(command[0])
        if not executable:
            return None
        try:
            return self.run([executable, *command[1:]], capture_output=True, text=True, timeout=timeout,
                            env=office_environment(**self.environment()))
        except (OSError, subprocess.SubprocessError):
            return None

    def _refresh(self):
        self._write_config()
        if self.environment():
            # Builds the cache in fonts/cache; without it fonts still work, only slower to start.
            result = self._tool(["fc-cache", str(self.files)], timeout=180)
            if result is None or result.returncode:
                LOGGER.warning("fc-cache could not index the uploaded fonts")
        else:
            shutil.rmtree(self.root / "cache", ignore_errors=True)
        self._families = None

    def families(self):
        """Every installed family name, mapped to all the names of that font (e.g. 標楷體 ↔ DFKai-SB)."""
        with self.lock:
            if self._families is not None:
                return self._families
            result = self._tool(["fc-list", "--format", "%{family}\n"], timeout=30)
            if result is None or result.returncode:
                return {}
            names = {}
            for line in result.stdout.splitlines():
                group = {normalize(name) for name in line.split(",") if name.strip()}
                for name in group:
                    names.setdefault(name, set()).update(group)
            self._families = {name: frozenset(group) for name, group in names.items()}
            return self._families

    def aliases(self):
        """Names our fontconfig file maps to fonts of the same style: {name: {substitutes}}."""
        if self._aliases is None:
            aliases = {}
            for path in ALIAS_FILES:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for block in re.findall(r"<alias[^>]*>(.*?)</alias>", text, re.S):
                    names = [normalize(name) for name in re.findall(r"<family>([^<]{1,200})</family>", block)]
                    if names:
                        aliases.setdefault(names[0], set()).update(names[1:])
                break
            self._aliases = {name: frozenset(targets) for name, targets in aliases.items()}
        return self._aliases

    # --- what documents asked for --------------------------------------------

    def substitutions(self, mapping):
        """Requested names the server lacks, in document order: [{name, used, similar}]."""
        installed = self.families()
        aliases = self.aliases()
        found = {}
        for entry in mapping if isinstance(mapping, list) else []:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2 or not isinstance(entry[1], (list, tuple)):
                continue
            original = clean_name(entry[0])
            # LibreOffice's own interface font is a ';'-separated fallback list, not a document font.
            if not original or ";" in original:
                continue
            key = normalize(original)
            used = [clean_name(str(item).rpartition("/")[0] or item) for item in entry[1][:10]]
            used = [name for name in used if name]
            used_keys = {normalize(name) for name in used}
            # Every name of the fonts actually drawn with (DFKai-SB is also 標楷體).
            drawn = used_keys.union(*(installed.get(name, ()) for name in used_keys))
            # Installed, including under another of its names; a missing glyph drawn
            # from another font does not make the requested font missing.
            if key in installed or key in drawn or drawn & same_font(key):
                continue
            if used_keys & SAME_WIDTHS.get(key, set()) or (key in SYMBOL_FONTS and "opensymbol" in used_keys):
                continue
            if key not in found:
                # Similar only when the font drawn is one our mapping chose for this name.
                found[key] = {"name": original, "used": used[0] if used else "", "similar": bool(used_keys & aliases.get(key, set()))}
        return list(found.values())[:20]

    def note(self, mapping):
        """Substituted fonts for one converted document, also counted for the admin page. Never raises."""
        try:
            fonts = self.substitutions(mapping)
            if fonts:
                self._record(fonts)
            return fonts
        except Exception:
            LOGGER.exception("Font substitution check failed")
            return []

    def _missing_file(self):
        return self.root / "missing.json"

    def _read_missing(self):
        try:
            items = json.loads(self._missing_file().read_text()).get("fonts", [])
        except (OSError, ValueError, AttributeError):
            return []
        cutoff = time.time() - MISSING_DAYS * 86400
        return [item for item in items if isinstance(item, dict) and clean_name(item.get("name"))
                and isinstance(item.get("last_seen"), (int, float)) and item["last_seen"] >= cutoff]

    def _record(self, fonts):
        with self.lock:
            items = {normalize(item["name"]): item for item in self._read_missing()}
            now = time.time()
            for font in fonts:
                entry = items.setdefault(normalize(font["name"]), {"name": font["name"], "count": 0})
                count = entry.get("count")
                entry.update(count=(count if isinstance(count, int) else 0) + 1, last_seen=now,
                             used=font["used"], similar=font["similar"])
            kept = sorted(items.values(), key=lambda item: item["last_seen"], reverse=True)[:MISSING_LIMIT]
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self._missing_file().with_suffix(".tmp")
            temporary.write_text(json.dumps({"fonts": kept}, ensure_ascii=False))
            temporary.replace(self._missing_file())

    def missing(self):
        """Recently substituted fonts, newest first; ones installed since then drop out by themselves."""
        with self.lock:
            installed = self.families()
            result = []
            for item in self._read_missing():
                key = normalize(item["name"])
                if key in installed or same_font(key) & installed.keys():
                    continue
                result.append({"name": item["name"], "count": item.get("count", 1), "last_seen": item["last_seen"],
                               "used": clean_name(item.get("used")), "similar": bool(item.get("similar"))})
            return result

    # --- uploaded font files --------------------------------------------------

    def items(self):
        result = []
        with self.lock:
            for path in self._font_files():
                try:
                    meta = json.loads((self.meta / f"{path.stem}.json").read_text())
                except (OSError, ValueError):
                    meta = {}
                result.append({"id": path.stem, "name": clean_name(meta.get("name")) or path.name,
                               "families": [name for name in meta.get("families", []) if clean_name(name)][:12],
                               "size": path.stat().st_size, "uploaded_at": meta.get("uploaded_at")})
        return sorted(result, key=lambda item: item["uploaded_at"] or 0, reverse=True)

    def _hashes(self, paths):
        hashes = set()
        for path in paths:
            try:
                hashes.add(json.loads((self.meta / f"{path.stem}.json").read_text()).get("sha256"))
            except (OSError, ValueError, AttributeError):
                continue
        return hashes

    def save(self, file):
        name = clean_name(str(getattr(file, "filename", "") or "").replace("\\", "/").rsplit("/", 1)[-1])
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension not in FONT_EXTENSIONS:
            raise FontError("請選擇 .ttf、.otf 或 .ttc 字體檔。")
        if shutil.which("fc-scan") is None:
            raise FontError("伺服器缺少字體工具（fontconfig），無法加入字體。")
        if len(self._font_files()) >= MAX_FONTS:
            raise FontError("已上傳的字體太多，請先刪除不再需要的字體。")
        for directory in (self.root, self.files, self.meta):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free < (FONT_LIMIT_MB + 100) * 1024 * 1024:
            raise FontError("磁碟剩餘空間不足，請先清理。")
        identifier = secrets.token_hex(12)
        incoming = self.root / f"incoming-{identifier}"
        digest = hashlib.sha256()
        size = 0
        try:
            # Received and checked outside the lock, so a slow upload never holds up conversions.
            with incoming.open("xb") as output:
                incoming.chmod(0o600)
                while chunk := file.stream.read(64 * 1024):
                    size += len(chunk)
                    if size > FONT_LIMIT_MB * 1024 * 1024:
                        raise FontError(f"字體檔超過 {FONT_LIMIT_MB} MB。")
                    digest.update(chunk)
                    output.write(chunk)
            with incoming.open("rb") as stream:
                if stream.read(4) not in FONT_SIGNATURES:
                    raise FontError("這不是字體檔，或檔案已損壞。")
            # fontconfig reads the file the same way LibreOffice will, in its own process.
            scan = self._tool(["fc-scan", "--format", "%{family}\n", str(incoming)], timeout=60)
            families = []
            for line in (scan.stdout.splitlines() if scan is not None and not scan.returncode else []):
                for family in line.split(","):
                    family = clean_name(family)
                    if family and family not in families:
                        families.append(family)
            if not families:
                raise FontError("這個字體檔無法讀取，請換一個檔案。")
            with self.lock:
                existing = self._font_files()
                if len(existing) >= MAX_FONTS:
                    raise FontError("已上傳的字體太多，請先刪除不再需要的字體。")
                if sum(path.stat().st_size for path in existing) + size > TOTAL_LIMIT_MB * 1024 * 1024:
                    raise FontError(f"已上傳的字體合計超過 {TOTAL_LIMIT_MB} MB，請先刪除不再需要的字體。")
                if digest.hexdigest() in self._hashes(existing):
                    raise FontError("這個字體檔已經上傳過了。")
                incoming.replace(self.files / f"{identifier}.{extension}")
                meta = {"name": name, "families": families[:12], "sha256": digest.hexdigest(), "uploaded_at": time.time()}
                (self.meta / f"{identifier}.json").write_text(json.dumps(meta, ensure_ascii=False))
                self._refresh()
        finally:
            incoming.unlink(missing_ok=True)
        return next(item for item in self.items() if item["id"] == identifier)

    def delete(self, font_id):
        if not isinstance(font_id, str) or not FONT_ID.fullmatch(font_id):
            raise FontError("找不到這個字體。")
        with self.lock:
            paths = [path for path in self._font_files() if path.stem == font_id]
            if not paths:
                raise FontError("找不到這個字體，可能已經刪除。")
            for path in paths:
                path.unlink(missing_ok=True)
            (self.meta / f"{font_id}.json").unlink(missing_ok=True)
            self._refresh()
