"""Print Office originals through LibreOffice UNO without exporting a PDF."""
import hashlib
import json
from ctypes.util import find_library
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .conversion import PROFILE_XML, office_environment
from .cups_client import CupsError
from .page_layout import PageLayoutError


class NativeOfficePrinter:
    def __init__(self, config, cups, fonts=None):
        self.config = config
        self.cups = cups
        self.fonts = fonts
        self.lock = threading.Lock()

    def _prepare_shim(self):
        deployment = Path(__file__).resolve().parent.parent / "deploy"
        installed = deployment / "cups_options_shim.so"
        if installed.is_file():
            return installed
        # Development can use the same bridge without installing anything globally.
        source = deployment / "cups_options_shim.c"
        compiler = shutil.which("cc") or shutil.which("gcc")
        cups_library = find_library("cups") if compiler else None
        if not compiler or not cups_library or not source.is_file():
            raise CupsError("Word、Excel 等文件的直接列印功能還沒安裝好。請先把文件另存成 PDF 再上傳，或請管理員改用完整版容器。")
        runtime = Path(self.config["DATA_DIR"]) / ".runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        target = runtime / f"cups-options-{digest}.so"
        if target.is_file():
            return target
        with tempfile.TemporaryDirectory(prefix="cups-bridge-", dir=runtime) as temporary:
            output = Path(temporary) / "cups_options_shim.so"
            try:
                result = subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                                         str(source), "-o", str(output), f"-l:{cups_library}", "-ldl"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CupsError("無法準備 Office 列印設定元件，請使用完整容器部署。") from exc
            if result.returncode or not output.is_file():
                raise CupsError("Word、Excel 等文件的直接列印功能安裝失敗。請先把文件另存成 PDF 再上傳，或請管理員改用完整版容器。")
            output.chmod(0o700)
            output.replace(target)
        return target

    @staticmethod
    def _write_options(path, options):
        # Length-prefixed records preserve driver choices without shell/CUPS escaping.
        records = []
        for key, value in options.items():
            if key in {"copies", "page-ranges"}:
                continue
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", key):
                raise CupsError("Office 列印設定名稱不正確。")
            if not isinstance(value, str) or any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise CupsError("Office 列印設定值不正確。")
            encoded_key, encoded_value = key.encode("utf-8"), value.encode("utf-8")
            if not 1 <= len(encoded_value) <= 2048:
                raise CupsError("Office 列印設定值不正確。")
            records.append(struct.pack(">HH", len(encoded_key), len(encoded_value)) + encoded_key + encoded_value)
        content = b"FPCO1\n" + b"".join(records)
        # 150 UI fields can each expand to verified PPD aliases in addition to
        # the basic settings. Keep the byte bound independent of field count.
        if len(records) > 512 or len(content) > 131072:
            raise CupsError("Office 列印設定過多。")
        with path.open("xb") as stream:
            path.chmod(0o600)
            stream.write(content)

    def print_file(self, printer, source, title, options, request_id, *, margins=None, paper=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", printer):
            raise CupsError("這台打印機的代號含有不支援的字元，請管理員在管理後台重新新增。")
        with self.lock, tempfile.TemporaryDirectory(prefix="office-print-", dir=self.config["DATA_DIR"]) as temporary:
            shim = self._prepare_shim()
            root = Path(temporary)
            home = root / "home"
            (home / ".cups").mkdir(parents=True)
            profile = root / "profile"
            (profile / "user").mkdir(parents=True)
            (profile / "user" / "registrymodifications.xcu").write_text(PROFILE_XML)
            # User-local CUPS options apply only to this LibreOffice process, never to shared queues.
            # Submit settings at the spooler boundary. Marking them in lpoptions
            # would let the renderer consume some options and CUPS consume them twice.
            (home / ".cups" / "lpoptions").write_text(f"Default {printer}\n")
            options_path = root / "cups-options.bin"
            self._write_options(options_path, options)
            receipt = root / "cups-submission.txt"
            job_name = f"FP-{request_id} {title}"[:220]
            request = {"source": str(source.resolve()), "printer": printer, "options": options,
                       "profile": str(profile.resolve()), "job_name": job_name,
                       "cups_receipt": str(receipt.resolve())}
            if margins is not None:
                request.update(margins=margins, paper=paper)
            path = root / "request.json"
            path.write_text(json.dumps(request))
            helper = Path(__file__).resolve().parent.parent / "deploy" / "office_worker.py"
            env = office_environment(HOME=str(home), CUPS_SERVER=f"{self.cups.host}:{self.cups.port}",
                                     FPC_CUPS_SHIM=str(shim.resolve()),
                                     FPC_CUPS_OPTIONS_FILE=str(options_path.resolve()), FPC_CUPS_RECEIPT=str(receipt.resolve()),
                                     FPC_CUPS_PRINTER=printer, FPC_CUPS_JOB_PREFIX=f"FP-{request_id} ",
                                     # The administrator's uploaded fonts, exactly as the preview used them.
                                     **(self.fonts.environment() if self.fonts is not None else {}))
            try:
                process = subprocess.Popen(["/usr/bin/python3", str(helper), str(path)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=env, start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=self.config["CONVERSION_TIMEOUT"])
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise CupsError("Office 打印準備逾時。請先查看任務列表，確認是否已提交。") from None
                if process.returncode == 2:
                    try:
                        layout_result = json.loads(stdout)
                    except (ValueError, TypeError):
                        layout_result = {}
                    error = layout_result.get("layout_error") if isinstance(layout_result, dict) else None
                    if isinstance(error, str) and 0 < len(error) <= 300:
                        raise PageLayoutError(error)
                if process.returncode:
                    raise CupsError("LibreOffice 無法直接打印這份文件，請確認文件可開啟、沒有密碼且打印機在線。")
            except OSError as exc:
                raise CupsError("Word、Excel 等文件的直接列印功能還沒安裝好。請先把文件另存成 PDF 再上傳。") from exc
            # UNO's Wait means submission to the spooler, not completion on paper.
            # The bridge gives the actual libcups job ID. This also works when
            # CUPS privacy policy excludes job titles from Get-Jobs responses.
            try:
                result = json.loads(stdout)
            except (ValueError, TypeError):
                result = {}
            if isinstance(result, dict) and result.get("submitted") is True:
                job_id = result.get("job_id")
                if type(job_id) is int and 0 < job_id <= 2147483647:
                    return job_id
            for attempt in range(12):
                matching = [job for job in self.cups.jobs()["jobs"]
                            if job["printer"] == printer and job["title"].startswith(f"FP-{request_id} ")]
                if matching:
                    return max(job["id"] for job in matching)
                time.sleep(0.25)
            raise CupsError("Office 已完成打印提交，但無法確認任務編號。請查看任務列表，避免重複打印。")
