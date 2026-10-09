import hmac
import ipaddress
import io
import math
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, send_file, session
import pymupdf
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from . import admin_views
from .config import settings, source_code_url
from .conversion import ConversionError, FORMATS, QueueFull, UploadStore
from .cups_client import CupsClient, CupsError, OPTION_VALUE, safe_print_option
from .discovery import Discovery
from .fonts import FontStore
from .native_office import NativeOfficePrinter


def validate_options(payload, printer, pages):
    copies = payload.get("copies", 1)
    if type(copies) is not int or not 1 <= copies <= 99:
        raise ValueError("打印份數必須為 1 到 99 的整數。")
    ranges = payload.get("page_ranges", "")
    if not isinstance(ranges, str) or len(ranges) > 500:
        raise ValueError("頁碼格式不正確，例如 1-3,5。")
    ranges = ranges.replace(" ", "")
    if ranges:
        for part in ranges.split(","):
            if not re.fullmatch(r"[1-9]\d*(?:-[1-9]\d*)?", part):
                raise ValueError("頁碼格式不正確，例如 1-3,5。")
            values = [int(number) for number in part.split("-")]
            limit = pages if pages is not None else 100000
            if values[0] > values[-1] or values[-1] > limit:
                raise ValueError(f"頁碼必須介於 1 到 {limit}，且起始頁不能大於結束頁。")
    caps = printer["capabilities"]
    options = {"copies": str(copies), "job-sheets": "none,none"}
    for field, supported, option in (("sides", "sides", "sides"), ("color_mode", "color_modes", "print-color-mode"), ("media", "media", "media")):
        value = payload.get(field, "")
        if not isinstance(value, str):
            raise ValueError("此打印機不支援所選的紙張、雙面或色彩設定。")
        if field == "media" and value == "custom":
            dimensions, bounds = payload.get("custom_media"), caps.get("custom_media")
            if not isinstance(dimensions, dict) or not isinstance(bounds, dict):
                raise ValueError("此打印機未提供自訂紙張範圍。")
            width, height = dimensions.get("width_mm"), dimensions.get("height_mm")
            if any(type(number) not in {int, float} or (type(number) is float and not math.isfinite(number)) for number in (width, height)):
                raise ValueError("自訂紙張的寬度與高度必須是毫米數值。")
            if not (bounds["min_width"] <= width <= bounds["max_width"] and bounds["min_height"] <= height <= bounds["max_height"]):
                raise ValueError("自訂紙張尺寸超出此打印機支援的範圍。")
            formatted = [f"{number:.2f}".rstrip("0").rstrip(".") for number in (width, height)]
            options[option] = f"Custom.{formatted[0]}x{formatted[1]}mm"
        elif value not in {"", "auto"}:
            if value not in caps.get(supported, []) or not OPTION_VALUE.fullmatch(value):
                raise ValueError("此打印機不支援所選的紙張、雙面或色彩設定。")
            options[option] = value
    orientation = payload.get("orientation", "")
    directions = {"portrait": "3", "landscape": "4", "reverse-portrait": "6", "reverse-landscape": "5"}
    if not isinstance(orientation, str) or orientation not in {"", "auto", *directions}:
        raise ValueError("紙張方向設定不正確。")
    if orientation in directions:
        options["orientation-requested"] = directions[orientation]
    if "fit_to_page" in payload:
        fit = payload["fit_to_page"]
        if type(fit) is not bool:
            raise ValueError("縮放設定不正確。")
        options["print-scaling"] = "fit" if fit else "none"
    advanced = payload.get("advanced_options", {})
    if not isinstance(advanced, dict) or len(advanced) > 150:
        raise ValueError("進階打印設定格式不正確。")
    supported_options = {item["key"]: {choice["value"] for choice in item.get("choices", [])}
                         for item in caps.get("print_options", [])}
    main_driver_equivalents = {"duplex": "sides", "colormodel": "print-color-mode",
                               "pagesize": "media", "pageregion": "media"}
    for key, value in advanced.items():
        if key not in supported_options or not safe_print_option(key) or not isinstance(value, str):
            raise ValueError("此打印機不支援所選的進階設定。")
        if value in {"", "auto"}:
            continue
        if not safe_print_option(key, value) or value not in supported_options[key]:
            raise ValueError("此打印機不支援所選的進階設定值。")
        if main_driver_equivalents.get(key.lower()) in options:
            raise ValueError("紙張、雙面或色彩在兩個地方都設定了，請把其中一處改回「自動」。")
        if key in options and options[key] != value:
            raise ValueError("重複的打印設定互相矛盾。")
        options[key] = value
        for driver_key, driver_value in caps.get("option_aliases", {}).get(key, {}).get(value, {}).items():
            if not safe_print_option(driver_key, driver_value):
                raise ValueError("打印機的進階設定資料不正確。")
            if driver_key in options and options[driver_key] != driver_value:
                raise ValueError("重複的打印設定互相矛盾。")
            options[driver_key] = driver_value
    if ranges:
        options["page-ranges"] = ranges
    return options


def create_app(config=None, cups_client=None, discovery=None):
    app = Flask(__name__)
    app.config.update(settings())
    if config:
        app.config.update(config)
    app.config["SOURCE_CODE_URL"] = source_code_url(app.config["SOURCE_CODE_URL"])
    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not app.config.get("SECRET_KEY"):
        key_path = data_dir / "session.key"
        try:
            # Created private from the start: anyone who reads this key can forge an admin session.
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.config["SECRET_KEY"] = key_path.read_text().strip()
    # Multipart boundaries/headers are allowed a small overhead over the actual file limit.
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_UPLOAD_MB"] * 1024 * 1024 + 64 * 1024
    cups = cups_client or CupsClient()
    if hasattr(cups, "configure_profiles"):
        cups.configure_profiles(data_dir)
    fonts = FontStore(data_dir)
    uploads = UploadStore(app.config, fonts=fonts)
    native_printer = NativeOfficePrinter(app.config, cups, fonts=fonts)
    discovery = discovery or Discovery(app.config["DISCOVERY_SECONDS"],
                                       sources=[cups.devices] if hasattr(cups, "devices") else [])
    app.extensions.update(cups_client=cups, uploads=uploads, discovery=discovery, native_printer=native_printer, fonts=fonts)

    def token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("請提供正確的請求內容。")
        return value

    @app.before_request
    def protect_mutations():
        # Accept LAN names/addresses only; a public DNS name must be explicitly configured.
        # This also blocks DNS rebinding through an attacker-controlled public hostname.
        hostname = urlsplit(request.host_url).hostname or ""
        allowed = hostname.lower() in app.config["ALLOWED_HOSTS"] or hostname.lower() == "localhost"
        allowed = allowed or hostname.lower().endswith((".local", ".lan", ".home.arpa"))
        allowed = allowed or bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", hostname))
        try:
            address = ipaddress.ip_address(hostname)
            allowed = allowed or address.is_private or address.is_loopback or address.is_link_local
        except ValueError:
            pass
        if not allowed:
            return jsonify(error="請透過家庭區域網路的位址開啟打印中心。"), 403
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            expected = session.get("csrf", "")
            provided = request.headers.get("X-CSRF-Token", "")
            if not expected or not provided.isascii() or not hmac.compare_digest(expected, provided):
                return jsonify(error="頁面驗證已過期，請重新整理後再試。"), 403
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify(error="不接受跨網站操作。"), 403
            origin = request.headers.get("Origin")
            if origin:
                try:
                    parts = urlsplit(origin)
                    if parts.scheme != request.scheme or parts.netloc != request.host:
                        return jsonify(error="不接受跨網站操作。"), 403
                except ValueError:
                    return jsonify(error="不接受跨網站操作。"), 403
        if time.time() - uploads.last_cleanup > 60:
            uploads.cleanup()

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(error):
        return jsonify(error=f"文件太大，上限為 {app.config['MAX_UPLOAD_MB']} MB。"), 413

    @app.errorhandler(CupsError)
    def cups_error(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(QueueFull)
    def queue_full(error):
        # Conversion runs one file at a time; the browser waits and sends the file again.
        response = jsonify(error=str(error))
        response.headers["Retry-After"] = "5"
        return response, 429

    @app.errorhandler(ConversionError)
    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(KeyError)
    def missing(error):
        return jsonify(error="找不到這份文件，可能已到期或移除。"), 404

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="請求無法處理。"), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception("Unhandled request error")
        return jsonify(error="服務暫時無法完成操作，請稍後再試。"), 500

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=token(), source_code_url=app.config["SOURCE_CODE_URL"])

    @app.get("/api/status")
    def status():
        return jsonify(cups=cups.status(), conversion=uploads.capabilities(), formats=FORMATS,
                       max_upload_mb=app.config["MAX_UPLOAD_MB"], max_pending_uploads=app.config["MAX_PENDING_UPLOADS"],
                       csrf_token=token(), demo=False)

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    @app.get("/api/printers")
    def printers():
        return jsonify(cups.printers())

    @app.get("/api/jobs")
    def jobs():
        result = cups.jobs()
        for job in result["jobs"]:
            job["title"] = re.sub(r"^FP-[A-Za-z0-9_-]{16,100} ", "", job.get("title", "打印任務"))
        return jsonify(result)

    def job_action(job_id, allowed, refused, action):
        known = next((job for job in cups.jobs()["jobs"] if job["id"] == job_id), None)
        if not known:
            return jsonify(error="找不到此打印任務。"), 404
        if not known.get(allowed):
            return jsonify(error=refused), 409
        action(job_id)
        return jsonify(ok=True)

    @app.post("/api/jobs/<int:job_id>/cancel")
    def cancel(job_id):
        return job_action(job_id, "can_cancel", "任務已完成或取消。", cups.cancel_job)

    @app.post("/api/jobs/<int:job_id>/hold")
    def hold(job_id):
        return job_action(job_id, "can_hold", "此任務已暫停或已結束。", cups.hold_job)

    @app.post("/api/jobs/<int:job_id>/release")
    def release(job_id):
        return job_action(job_id, "can_release", "此任務沒有暫停。", cups.release_job)

    @app.post("/api/uploads")
    def upload():
        file = request.files.get("file")
        if file is None:
            raise ValueError("請先選擇一份文件。")
        upload_id = uploads.create(file)
        return jsonify(id=upload_id, status="queued"), 202

    @app.get("/api/uploads/<upload_id>")
    def upload_status(upload_id):
        return jsonify(uploads.public(upload_id))

    @app.get("/api/uploads/<upload_id>/pdf")
    def preview(upload_id):
        record = uploads.get(upload_id)
        if record["status"] != "ready":
            return jsonify(error="文件尚未準備好。"), 409
        return send_file(uploads.directory(upload_id) / "document.pdf", mimetype="application/pdf", download_name="preview.pdf", conditional=True)

    @app.get("/api/uploads/<upload_id>/pages/<int:page_number>/preview.png")
    def page_preview(upload_id, page_number):
        record = uploads.get(upload_id)
        if record["status"] != "ready":
            return jsonify(error="文件尚未準備好。"), 409
        if not 1 <= page_number <= record["pages"]:
            raise ValueError("預覽頁碼超出文件範圍。")
        with pymupdf.open(uploads.directory(upload_id) / "document.pdf") as document:
            page = document[page_number - 1]
            width, height = page.rect.width, page.rect.height
            scale = min(1200 / width, 4096 / height, math.sqrt(4_000_000 / (width * height)), 2.0)
            matrix = pymupdf.Matrix(scale, scale)
            bounds = (page.rect * matrix).irect
            while bounds.width > 1200 or bounds.height > 4096 or bounds.width * bounds.height > 4_000_000:
                scale *= min(1200 / bounds.width, 4096 / bounds.height,
                             math.sqrt(4_000_000 / (bounds.width * bounds.height))) * 0.999
                matrix = pymupdf.Matrix(scale, scale)
                bounds = (page.rect * matrix).irect
            bitmap = page.get_pixmap(matrix=matrix, alpha=False)
            result = bitmap.tobytes("png")
        return send_file(io.BytesIO(result), mimetype="image/png", download_name=f"page-{page_number}.png")

    @app.delete("/api/uploads/<upload_id>")
    def delete(upload_id):
        uploads.delete(upload_id)
        return jsonify(ok=True)

    @app.post("/api/print")
    def print_document():
        payload = body()
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", request_id):
            raise ValueError("缺少有效的打印請求編號，請重新整理頁面。")
        with uploads.lock:
            record = uploads.get(payload.get("upload_id"))
            if record["status"] != "ready":
                return jsonify(error="文件尚未準備好，請等待轉換完成。"), 409
            if request_id in record["requests"]:
                if record["requests"][request_id] is None:
                    return jsonify(error="這次提交的結果尚未確認，請先查看任務列表；為避免重複打印，同一請求不會再次送出。"), 409
                return jsonify(job_id=record["requests"][request_id], reused=True)
            if record["id"] in uploads.active_prints:
                return jsonify(error="這份文件正在提交另一個任務，請稍後再試。"), 409
            printer = next((printer for printer in cups.printers()["printers"] if printer["name"] == payload.get("printer")), None)
            if not printer:
                raise ValueError("請選擇已加入的打印機。")
            mode = payload.get("print_mode", "auto")
            if hasattr(cups, "resolve_print_mode"):
                printer = cups.resolve_print_mode(printer, mode)
            elif mode != "auto":
                raise ValueError("此打印機不支援所選的打印模式。")
            if not printer["accepting"] or printer["state"] == 5:
                raise ValueError("打印機已暫停或不接受任務，請先恢復打印機。")
            # Native Office pagination can change with printer paper settings.
            options = validate_options(payload, printer, None if record.get("native") else record["pages"])
            if hasattr(cups, "validate_job_options"):
                cups.validate_job_options(printer["name"], options)
            record["requests"][request_id] = None
            uploads.write(record)
            uploads.active_prints.add(record["id"])
        try:
            if record.get("native"):
                job_id = native_printer.print_file(printer["name"], uploads.directory(record["id"]) / f"input.{record['extension']}", record["name"], options, request_id)
            else:
                job_id = cups.print_file(printer["name"], uploads.directory(record["id"]) / "document.pdf", record["name"], options)
            with uploads.lock:
                current = uploads.get(record["id"])
                current["requests"][request_id] = job_id
                uploads.write(current)
        finally:
            with uploads.lock:
                uploads.active_prints.discard(record["id"])
        return jsonify(job_id=job_id)

    admin_views.register(app, cups, discovery, token, body)

    return app
