"""Bounded document conversion and private, expiring upload storage."""
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps, ImageSequence

from .office_runtime import PRIVATE_ENVIRONMENT, office_environment

OFFICE_EXTENSIONS = {"doc", "docx", "docm", "odt", "rtf", "xls", "xlsx", "xlsm", "ods", "csv", "tsv", "ppt", "pptx", "pptm", "ppsx", "odp", "txt", "md"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp"}
EBOOK_EXTENSIONS = {"epub", "fb2", "cbz", "xps", "oxps"}
FORMATS = sorted({"pdf"} | OFFICE_EXTENSIONS | IMAGE_EXTENSIONS | EBOOK_EXTENSIONS)
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
LOGGER = logging.getLogger(__name__)
PROFILE_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
  <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
  <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
  <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Linguistic/General">
  <!-- Asian text without a language tag uses Traditional Chinese glyphs
       instead of the Simplified/Japanese forms picked under the C locale. -->
  <prop oor:name="DefaultLocale_CJK" oor:op="fuse"><value>zh-TW</value></prop>
 </item>
</oor:items>'''
class ConversionError(ValueError):
    pass


class QueueFull(ConversionError):
    """Every conversion slot is taken; the same upload can be sent again shortly."""


def preview_office(source, target, directory, timeout, environment=None):
    """Preview an original in an isolated UNO process without touching uploads.

    Callers provide a scratch directory. Its private profile/request are always
    removed; only a completed preview PDF and the unchanged original survive.
    Returns what LibreOffice reported about fonts, [[requested, [used, ...]], ...],
    or None when it could not say.
    """
    source, target, directory = Path(source), Path(target), Path(directory)
    target.unlink(missing_ok=True)
    completed = False
    fonts = None
    try:
        with tempfile.TemporaryDirectory(prefix="office-preview-", dir=directory) as temporary:
            scratch = Path(temporary)
            profile = scratch / "office-profile"
            (profile / "user").mkdir(parents=True)
            (profile / "user" / "registrymodifications.xcu").write_text(PROFILE_XML)
            preview_request = scratch / "preview-request.json"
            font_report = scratch / "fonts.json"
            preview_request.write_text(json.dumps({"mode": "preview", "source": str(source.resolve()),
                                                   "profile": str(profile.resolve()), "target": str(target.resolve()),
                                                   "fonts": str(font_report.resolve())}))
            helper = Path(__file__).resolve().parent.parent / "deploy" / "office_worker.py"
            command = ["/usr/bin/python3", str(helper), str(preview_request)]
            try:
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True, env=office_environment(**(environment or {})))
            except OSError as exc:
                raise ConversionError("Office 預覽服務尚未就緒，請管理員執行診斷。") from exc
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise ConversionError("Office 預覽準備逾時，請縮小文件後再試。") from None
            if process.returncode or not target.is_file() or not target.stat().st_size:
                raise ConversionError("LibreOffice 無法預覽此文件，請確認文件可開啟且沒有密碼。")
            try:
                if font_report.is_file() and font_report.stat().st_size <= 512 * 1024:
                    fonts = json.loads(font_report.read_text())
            except (OSError, ValueError):
                fonts = None
            completed = True
    finally:
        if not completed:
            target.unlink(missing_ok=True)
    return fonts if isinstance(fonts, list) else None


class UploadStore:
    def __init__(self, config, fonts=None):
        self.config = config
        self.fonts = fonts
        self.root = Path(config["DATA_DIR"]) / "uploads"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.active_prints = set()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convert")
        self.slots = threading.BoundedSemaphore(config["MAX_PENDING_UPLOADS"])
        self.last_cleanup = 0
        # Interrupted conversions cannot resume automatically after a restart.
        for path in self.root.glob("*/metadata.json"):
            try:
                record = json.loads(path.read_text())
                if record.get("status") in {"queued", "converting"}:
                    record.update(status="error", error="服務曾重新啟動，請重新上傳文件。")
                    self.write(record)
            except (ValueError, OSError):
                LOGGER.warning("Ignoring an unreadable upload record")
        self.cleanup()

    def directory(self, upload_id):
        if not isinstance(upload_id, str) or not ID_PATTERN.fullmatch(upload_id):
            raise KeyError(upload_id)
        return self.root / upload_id

    def get(self, upload_id):
        with self.lock:
            try:
                return json.loads((self.directory(upload_id) / "metadata.json").read_text())
            except (FileNotFoundError, ValueError):
                raise KeyError(upload_id) from None

    def write(self, record):
        with self.lock:
            path = self.directory(record["id"]) / "metadata.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False))
            temporary.replace(path)

    def capabilities(self):
        return {"office": bool(shutil.which("libreoffice") or shutil.which("soffice")), "images": True, "pdf": True, "ebooks": True}

    def cleanup(self):
        with self.lock:
            cutoff = time.time() - self.config["RETENTION_HOURS"] * 3600
            for path in self.root.glob("*/metadata.json"):
                try:
                    record = json.loads(path.read_text())
                    if record.get("created_at", 0) < cutoff and record.get("status") not in {"queued", "converting"} and record.get("id") not in self.active_prints:
                        shutil.rmtree(path.parent)
                except (ValueError, OSError):
                    LOGGER.warning("Upload cleanup skipped an unreadable record")
            self.last_cleanup = time.time()

    def create(self, file):
        # Only generated filenames reach any converter, regardless of original filename.
        name = str(file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(character for character in name if character.isprintable())[:200]
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension not in FORMATS:
            raise ConversionError("此格式尚未支援，請先轉為 PDF。")
        if extension in OFFICE_EXTENSIONS and not self.capabilities()["office"]:
            raise ConversionError("尚未安裝 Office 文件轉換元件，請使用完整容器或先轉成 PDF。")
        if not self.slots.acquire(blocking=False):
            raise QueueFull("目前等待轉換的文件較多，請稍後再試。")
        upload_id = uuid.uuid4().hex
        directory = self.directory(upload_id)
        try:
            self.cleanup()
            directory.mkdir(mode=0o700)
            size = 0
            max_size = self.config["MAX_UPLOAD_MB"] * 1024 * 1024
            if shutil.disk_usage(self.root).free < max_size + 100 * 1024 * 1024:
                raise ConversionError("磁碟剩餘空間不足，請清理舊文件。")
            with (directory / f"input.{extension}").open("wb") as output:
                while chunk := file.stream.read(64 * 1024):
                    size += len(chunk)
                    if size > max_size:
                        raise ConversionError("文件超過上傳大小限制。")
                    output.write(chunk)
            if not size:
                raise ConversionError("不能上傳空白文件。")
            record = {"id": upload_id, "name": name, "extension": extension,
                      "status": "queued", "pages": None, "error": None,
                      "native": extension in OFFICE_EXTENSIONS,
                      "created_at": time.time(), "size": size, "requests": {}}
            self.write(record)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            self.slots.release()
            raise
        if self.config["CONVERSION_SYNC"]:
            self.convert(upload_id)
        else:
            self.executor.submit(self.convert, upload_id)
        return upload_id

    def convert(self, upload_id):
        record = self.get(upload_id)
        directory = self.directory(upload_id)
        source = directory / f"input.{record['extension']}"
        target = directory / "document.pdf"
        fonts = None
        try:
            record["status"] = "converting"
            self.write(record)
            if record.get("native"):
                self._validate_office(source, record["extension"])
                # This PDF is for browser preview only. Printing still opens the original in UNO.
                fonts = self._office_preview(source, target, directory)
            elif record["extension"] == "pdf":
                with source.open("rb") as stream:
                    if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                        raise ConversionError("文件不是有效的 PDF。")
                shutil.copyfile(source, target)
            elif record["extension"] in IMAGE_EXTENSIONS:
                self._image_to_pdf(source, target)
            else:
                self._ebook_to_pdf(source, target)
            with fitz.open(target) as document:
                if document.needs_pass:
                    raise ConversionError("PDF 已加密，請移除密碼後再上傳。")
                if not 0 < len(document) <= self.config["MAX_DOCUMENT_PAGES"]:
                    raise ConversionError("文件頁數超出限制。")
                # Parse each page now to catch malformed uploads before they reach CUPS.
                for page in document:
                    if page.rect.is_empty or page.rect.is_infinite:
                        raise ConversionError("文件包含無效頁面。")
                record.update(status="ready", pages=len(document), preview_url=f"/api/uploads/{upload_id}/pdf")
                record["page_preview_url"] = f"/api/uploads/{upload_id}/pages/1/preview.png"
            if fonts and self.fonts is not None:
                # Printing opens the same original with the same fonts, so the preview's substitutions hold for paper too.
                record["fonts"] = self.fonts.note(fonts)
        except ConversionError as exc:
            record.update(status="error", error=str(exc))
            target.unlink(missing_ok=True)
        except Exception:
            LOGGER.exception("Conversion failed for %s", upload_id)
            record.update(status="error", error="文件無法轉換，可能已損壞或格式不相符，請先轉成 PDF 再試。")
            target.unlink(missing_ok=True)
        finally:
            # Office prints from its original bytes; other sources are removed after PDF preparation.
            if not record.get("native") or record["status"] == "error":
                source.unlink(missing_ok=True)
            shutil.rmtree(directory / "office-profile", ignore_errors=True)
            shutil.rmtree(directory / "converted", ignore_errors=True)
            self.write(record)
            self.slots.release()

    def _image_to_pdf(self, source, target):
        frames = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    # Multi-page TIFF is a document; animated GIF/WebP prints its first frame.
                    iterator = ImageSequence.Iterator(image) if image.format == "TIFF" else [image]
                    total_pixels = 0
                    for frame in iterator:
                        pixels = frame.width * frame.height
                        total_pixels += pixels
                        if total_pixels > self.config["MAX_IMAGE_PIXELS"] or len(frames) >= 100:
                            raise ConversionError("圖片尺寸或頁數過大，請縮小後再上傳。")
                        oriented = ImageOps.exif_transpose(frame)
                        rgba = oriented.convert("RGBA")
                        rgb = Image.new("RGB", rgba.size, "white")
                        rgb.paste(rgba, mask=rgba.getchannel("A"))
                        frames.append(rgb)
            frames[0].save(target, "PDF", save_all=True, append_images=frames[1:], resolution=150)
        finally:
            for frame in frames:
                frame.close()

    @staticmethod
    def _check_archive(source):
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > 10000 or sum(member.file_size for member in members) > 200 * 1024 * 1024:
                raise ConversionError("文件解壓後過大，請縮小內容後再上傳。")
            return {member.filename for member in members}

    def _validate_office(self, source, extension):
        expected = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml",
                    "docm": "word/document.xml", "xlsm": "xl/workbook.xml", "pptm": "ppt/presentation.xml", "ppsx": "ppt/presentation.xml",
                    "odt": "content.xml", "ods": "content.xml", "odp": "content.xml"}
        if extension in expected:
            if expected[extension] not in self._check_archive(source):
                raise ConversionError("文件內容與副檔名不相符，請確認原檔格式。")
        elif extension in {"doc", "xls", "ppt"}:
            with source.open("rb") as stream:
                header = stream.read(16)
            if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and not (extension == "doc" and header.startswith(b"{\\rtf")):
                raise ConversionError("文件不是有效的 Office 原檔。")

    def _ebook_to_pdf(self, source, target):
        if source.suffix.lower() in {".epub", ".cbz", ".xps", ".oxps"}:
            self._check_archive(source)
        with fitz.open(source) as document:
            if document.needs_pass:
                raise ConversionError("電子書需要密碼或受到保護，請提供可開啟的文件。")
            if document.is_reflowable:
                document.layout(width=595, height=842, fontsize=11)
            if not 0 < len(document) <= self.config["MAX_DOCUMENT_PAGES"]:
                raise ConversionError("電子書頁數超出限制。")
            target.write_bytes(document.convert_to_pdf())

    def _office_preview(self, source, target, directory):
        return preview_office(source, target, directory, timeout=self.config["CONVERSION_TIMEOUT"],
                              environment=self.fonts.environment() if self.fonts is not None else None)

    def public(self, upload_id):
        record = self.get(upload_id)
        result = {key: record.get(key) for key in ("id", "name", "status", "pages", "error", "preview_url", "page_preview_url", "native")}
        result["substituted_fonts"] = [{"name": font["name"], "similar": bool(font.get("similar"))}
                                       for font in record.get("fonts") or [] if isinstance(font, dict) and font.get("name")]
        return result

    def delete(self, upload_id):
        with self.lock:
            record = self.get(upload_id)
            if record["status"] in {"queued", "converting"} or upload_id in self.active_prints:
                raise ConversionError("文件正在準備或提交打印，請完成後再移除。")
            shutil.rmtree(self.directory(upload_id))
