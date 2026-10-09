"""Signed-in administrator page: find, add and manage printers."""
import functools
import math
import re

from flask import jsonify, make_response, render_template, request, session

from .admin import (PPD_LIMIT, QUEUE_NAME, AdminAuth, LoginBlocked, PpdStore, build_driver_index, build_uri,
                    automatic_driver, clean_text, driver_candidates, probe_printer, queue_name, resolve_printer_host, search_drivers)
from .cups_client import CupsError
from .fonts import FONT_LIMIT_MB
from .printer_profiles import is_companion_name

PRINTER_ACTIONS = {"default", "pause", "resume", "test", "clear"}


class UnknownPrinter(Exception):
    pass


def register(app, cups, discovery, token, body):
    auth = AdminAuth(app.config["DATA_DIR"], app.config.get("ADMIN_PASSWORD"), app.logger)
    store = PpdStore(app.config["DATA_DIR"])
    probe = app.config.get("PRINTER_PROBE") or probe_printer
    index_cache = {"source": None, "index": {}}
    app.extensions.update(admin_auth=auth, ppd_store=store)

    def guard(allow_initial=False):
        def decorate(view):
            @functools.wraps(view)
            def wrapper(*args, **kwargs):
                state = auth.current(session)
                if state is None:
                    return jsonify(error="請先登入管理後台。", login=True), 401
                if state["must_change"] and not allow_initial:
                    return jsonify(error="請先更改初始密碼，才能管理打印機。", must_change=True), 403
                return view(*args, **kwargs)
            return wrapper
        return decorate

    def blocked(error):
        response = jsonify(error=f"嘗試次數太多，請 {math.ceil(error.seconds / 60)} 分鐘後再試。")
        response.headers["Retry-After"] = str(error.seconds)
        return response, 429

    def drivers_and_index():
        try:
            ppds = cups.ppds()
        except CupsError:
            return {}, {}
        if index_cache["source"] is not ppds:
            index_cache.update(source=ppds, index=build_driver_index(ppds))
        return ppds, index_cache["index"]

    def find_printer(name):
        if not QUEUE_NAME.fullmatch(name):
            raise ValueError("打印機代號格式不正確。")
        printer = next((item for item in cups.admin_printers()["printers"] if item["name"].lower() == name.lower()), None)
        if printer is None:
            raise UnknownPrinter(name)
        return printer

    def pick_driver(value, driverless):
        if value == "everywhere":
            if not driverless:
                raise ValueError("這台打印機不支援免驅動列印，請選擇相符型號的驅動程式。")
            return value, None
        if isinstance(value, str) and value.startswith("upload:"):
            return value, store.path(value)
        if not isinstance(value, str) or value not in cups.ppds():
            raise ValueError("請選擇已安裝的驅動程式。")
        if value.startswith("driverless:") or re.search(r"\bdriverless\b|IPP Everywhere", str(cups.ppds()[value].get("ppd-make-and-model") or ""), re.I):
            raise ValueError("IPP 相容模式請選擇「免驅動 IPP（相容）」。")
        return value, None

    def address():
        return request.remote_addr or "unknown"

    @app.errorhandler(UnknownPrinter)
    def unknown_printer(error):
        return jsonify(error="找不到這台打印機，可能已被刪除。"), 404

    @app.get("/admin")
    def admin_page():
        response = make_response(render_template("admin.html", csrf_token=token(),
                                                source_code_url=app.config["SOURCE_CODE_URL"]))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/admin/session")
    def admin_session():
        state = auth.current(session)
        return jsonify(logged_in=state is not None, must_change=bool(state and state["must_change"]),
                       configured=auth.record is not None, csrf_token=token(),
                       initial_password=state is None and auth.initial_path.exists())

    @app.post("/api/admin/login")
    def admin_login():
        payload = body()
        if auth.record is None:
            return jsonify(error="管理員設定檔損壞，請刪除資料目錄中的 admin.json 後重新啟動服務。"), 503
        try:
            if not auth.login(session, payload.get("password"), address()):
                return jsonify(error="密碼不正確。"), 401
        except LoginBlocked as error:
            return blocked(error)
        return jsonify(ok=True, must_change=auth.current(session)["must_change"], csrf_token=session["csrf"])

    @app.post("/api/admin/logout")
    def admin_logout():
        auth.logout(session)
        return jsonify(ok=True, csrf_token=token())

    @app.post("/api/admin/password")
    @guard(allow_initial=True)
    def admin_password():
        payload = body()
        try:
            auth.change_password(session, payload.get("current"), payload.get("new"), address())
        except LoginBlocked as error:
            return blocked(error)
        return jsonify(ok=True, csrf_token=session["csrf"])

    @app.get("/api/admin/overview")
    @guard()
    def admin_overview():
        status = cups.status()
        printers, jobs = {"printers": [], "default": None}, []
        if status["available"]:
            printers = cups.admin_printers()
            jobs = cups.all_jobs()["jobs"]
        return jsonify(cups=status, printers=printers["printers"], default=printers["default"],
                       active_jobs=sum(1 for job in jobs if job["can_cancel"]), uploads=store.items())

    @app.get("/api/admin/printers")
    @guard()
    def admin_printers():
        return jsonify(cups.admin_printers())

    @app.post("/api/admin/scan")
    @guard()
    def admin_scan():
        discovery.start()
        return jsonify(scanning=True), 202

    @app.get("/api/admin/scan")
    @guard()
    def admin_scan_results():
        snapshot = discovery.snapshot()
        if snapshot["scanning"]:
            return jsonify(snapshot)
        ppds, index = drivers_and_index()
        try:
            existing = {item["uri"]: item["name"] for item in cups.admin_printers()["printers"]}
        except CupsError:
            existing = {}
        for device in snapshot["devices"]:
            device["drivers"] = driver_candidates(device, index, ppds)
            device["recommended_driver"] = automatic_driver(device, index, ppds)
            device["added_as"] = existing.get(device.get("uri"))
        snapshot["devices"].sort(key=lambda device: (device["added_as"] is not None, device.get("label", "").lower()))
        return jsonify(snapshot)

    @app.get("/api/admin/drivers")
    @guard()
    def admin_drivers():
        ppds = cups.ppds()
        return jsonify(drivers=search_drivers(ppds, request.args.get("q", "")[:100]),
                       uploads=store.items(), total=len(ppds))

    @app.get("/api/admin/ppds")
    @guard()
    def admin_uploaded_ppds():
        return jsonify(uploads=store.items())

    @app.post("/api/admin/ppds")
    @guard()
    def admin_upload_ppd():
        file = request.files.get("file")
        if file is None:
            raise ValueError("請選擇要上傳的驅動檔（.ppd）。")
        data = file.stream.read(PPD_LIMIT + 1)
        if len(data) > PPD_LIMIT:
            raise ValueError("驅動檔太大，上限為 2 MB。")
        return jsonify(store.save(data, getattr(cups, "check_ppd", None))), 201

    @app.delete("/api/admin/ppds/<identifier>")
    @guard()
    def admin_delete_ppd(identifier):
        store.delete(f"upload:{identifier}")
        return jsonify(ok=True)

    @app.get("/api/admin/fonts")
    @guard()
    def admin_fonts():
        fonts = app.extensions["fonts"]
        return jsonify(fonts=fonts.items(), missing=fonts.missing(), limit_mb=FONT_LIMIT_MB)

    @app.post("/api/admin/fonts")
    @guard()
    def admin_upload_font():
        file = request.files.get("file")
        if file is None:
            raise ValueError("請選擇要上傳的字體檔（.ttf、.otf 或 .ttc）。")
        return jsonify(app.extensions["fonts"].save(file)), 201

    @app.delete("/api/admin/fonts/<identifier>")
    @guard()
    def admin_delete_font(identifier):
        app.extensions["fonts"].delete(identifier)
        return jsonify(ok=True)

    @app.post("/api/admin/probe")
    @guard()
    def admin_probe():
        result = probe(body().get("host"))
        ppds, index = drivers_and_index()
        result["drivers"] = driver_candidates(result, index, ppds)
        result["recommended_driver"] = automatic_driver(result, index, ppds)
        return jsonify(result)

    @app.post("/api/admin/printers")
    @guard()
    def admin_add_printer():
        payload = body()
        source = payload.get("source")
        if source == "discovered":
            device = discovery.get(payload.get("device_id"))
            uri, label, driverless = device["uri"], device.get("label") or "打印機", bool(device.get("driverless"))
            ipp_uri = device.get("ipp_uri")
        elif source == "manual":
            protocol = payload.get("protocol")
            host, _ = resolve_printer_host(payload.get("host"))
            uri = build_uri(protocol, host, payload.get("port"), payload.get("path"))
            label = clean_text(payload.get("label"), 127) or host.strip("[]")
            driverless = protocol in {"ipp", "ipps"}
            ipp_uri = uri if driverless else None
            device = None
        else:
            raise ValueError("請選擇要新增的打印機。")
        requested_driver = payload.get("driver")
        if requested_driver is None or requested_driver == "auto":
            if device is None:
                device = probe(host)
                device["driverless"] = bool(device.get("driverless")) and driverless
                driverless = device["driverless"]
            ppds, index = drivers_and_index()
            requested_driver = automatic_driver(device, index, ppds)
            if requested_driver is None:
                raise ValueError("找不到已確認相容的驅動，請選擇相符型號的驅動程式。")
        driver, ppd_file = pick_driver(requested_driver, driverless)
        current = cups.admin_printers()["printers"]
        duplicate = next((item for item in current if item["uri"] == uri), None)
        if duplicate:
            return jsonify(error=f"這台打印機已經加入過了（{duplicate['info'] or duplicate['name']}）。"), 409
        name = payload.get("name") or queue_name(label, [item["name"] for item in current])
        if not isinstance(name, str) or not QUEUE_NAME.fullmatch(name):
            raise ValueError("打印機代號只可使用英文字母、數字、底線及連字號，並以英文字母開頭。")
        if is_companion_name(name):
            raise ValueError("這個打印機代號保留給相容模式使用，請換一個代號。")
        if any(item["name"].lower() == name.lower() for item in current):
            return jsonify(error="已經有同名的打印機，請換一個代號。"), 409
        add = cups.add_profiled_printer if hasattr(cups, "add_profiled_printer") else cups.add_printer
        profile_options = {"driverless": driverless, "ipp_uri": ipp_uri} if hasattr(cups, "add_profiled_printer") else {}
        result = add(name, uri, driver, info=clean_text(payload.get("info"), 127) or label[:127],
                     location=clean_text(payload.get("location"), 127), ppd_file=ppd_file,
                     make_default=payload.get("default") is True, **profile_options)
        return jsonify(result), 201

    @app.patch("/api/admin/printers/<name>")
    @guard()
    def admin_modify_printer(name):
        printer = find_printer(name)
        payload = body()
        info = clean_text(payload["info"], 127) if "info" in payload else None
        location = clean_text(payload["location"], 127) if "location" in payload else None
        driver = ppd_file = None
        if payload.get("driver"):
            # CUPS checks the printer itself when switching to the driverless mode.
            driver, ppd_file = pick_driver(payload["driver"], True)
        if hasattr(cups, "modify_profiled_printer"):
            uri = printer.get("uri", "")
            ipp_uri = uri if uri.startswith(("ipp://", "ipps://")) else None
            compatible = "driverless" in printer.get("print_modes", [])
            if driver and not ipp_uri:
                found = next((device for device in discovery.snapshot()["devices"] if device.get("uri") == uri), None)
                if found is not None:
                    try:
                        device = discovery.get(found["id"])
                    except ValueError:
                        device = None
                    if device is not None:
                        ipp_uri = device.get("ipp_uri")
                        compatible = bool(device.get("driverless"))
            return jsonify(cups.modify_profiled_printer(printer["name"], info, location, None if ppd_file else driver,
                                                        ppd_file, driverless=compatible, ipp_uri=ipp_uri))
        return jsonify(cups.modify_printer(printer["name"], info, location, None if ppd_file else driver, ppd_file))

    @app.post("/api/admin/printers/<name>/<action>")
    @guard()
    def admin_printer_action(name, action):
        if action not in PRINTER_ACTIONS:
            return jsonify(error="不支援這個操作。"), 404
        return jsonify(cups.printer_action(find_printer(name)["name"], action))

    @app.delete("/api/admin/printers/<name>")
    @guard()
    def admin_delete_printer(name):
        return jsonify(cups.printer_action(find_printer(name)["name"], "delete"))

    @app.get("/api/admin/jobs")
    @guard()
    def admin_jobs():
        result = cups.all_jobs()
        for job in result["jobs"]:
            job["title"] = re.sub(r"^FP-[A-Za-z0-9_-]{16,100} ", "", job.get("title") or "打印任務")
        return jsonify(result)

    @app.post("/api/admin/jobs/<int:job_id>/cancel")
    @guard()
    def admin_cancel_job(job_id):
        cups.cancel_any_job(job_id)
        return jsonify(ok=True)
