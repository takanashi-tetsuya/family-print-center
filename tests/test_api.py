import copy
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import fitz
from PIL import Image

from print_center.app import create_app
from print_center.cups_client import CupsError
from tests.helpers import office_bytes, pdf_bytes


class FakeCups:
    def __init__(self):
        self.prints = []
        self.cancelled = []
        self.added = []
        self.error = None
        self.printer = {
            "name": "brother", "label": "Brother MFC", "state": 3,
            "accepting": True, "is_default": True,
            "capabilities": {
                "sides": ["one-sided", "two-sided-long-edge", "two-sided-short-edge"],
                "color_modes": ["monochrome"],
                "media": ["iso_a4_210x297mm", "na_letter_8.5x11in"],
                "print_options": [
                    {"key": "printer-resolution", "choices": [{"value": "300dpi"}, {"value": "600dpi"}]},
                    {"key": "print-quality", "choices": [{"value": "3"}, {"value": "4"}, {"value": "5"}]},
                    {"key": "number-up", "choices": [{"value": "1"}, {"value": "2"}, {"value": "4"}]},
                    {"key": "print-scaling", "choices": [{"value": "fit"}, {"value": "none"}, {"value": "fill"}]},
                    {"key": "BrMediaType", "choices": [{"value": "PLAIN"}, {"value": "THICK"}]},
                ],
            },
        }

    def status(self):
        return {"available": self.error is None, "error": self.error}

    def printers(self):
        if self.error:
            raise CupsError(self.error)
        return {"printers": [copy.deepcopy(self.printer)], "default": "brother"}

    def print_file(self, printer, path, title, options):
        if self.error:
            raise CupsError(self.error)
        assert path.is_file()
        self.prints.append((printer, path, title, options))
        return 41 + len(self.prints)

    def jobs(self):
        return {"jobs": [{"id": 42, "can_cancel": True, "can_hold": True, "can_release": False},
                         {"id": 43, "can_cancel": False, "can_hold": False, "can_release": False},
                         {"id": 44, "can_cancel": True, "can_hold": False, "can_release": True}]}

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)

    def hold_job(self, job_id):
        self.cancelled.append(("hold", job_id))

    def release_job(self, job_id):
        self.cancelled.append(("release", job_id))

    def ppds(self):
        return {"drv:///sample.drv/generic.ppd": {"ppd-make": "Generic", "ppd-make-and-model": "Generic PostScript Printer"}}

    def admin_printers(self):
        return {"printers": [{"name": "brother", "info": "Brother MFC", "uri": "usb://Brother/MFC", "is_default": True}],
                "default": "brother"}

    def all_jobs(self):
        return {"jobs": []}

    def add_printer(self, name, uri, driver, **options):
        self.added.append((name, uri, driver, options))
        return {"name": name}


class FakeDiscovery:
    def snapshot(self):
        return {"devices": [], "scanning": False, "error": None}

    def start(self):
        pass

    def get(self, device_id):
        if device_id != "discovered_device":
            raise ValueError("找不到此打印機，請重新掃描。")
        return {"id": device_id, "label": "Epson", "driverless": True, "uri": "dnssd://Epson._ipp._tcp.local."}


@pytest.fixture
def app(tmp_path):
    value = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "CONVERSION_SYNC": True,
                        "MAX_UPLOAD_MB": 1, "SECRET_KEY": "test-session-secret"},
                       cups_client=FakeCups(), discovery=FakeDiscovery())
    yield value
    value.extensions["uploads"].executor.shutdown(wait=True)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def headers(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json["csrf_token"], "Origin": "http://localhost"}


@pytest.fixture
def uploaded(client, headers):
    response = client.post("/api/uploads", data={"file": (io.BytesIO(pdf_bytes()), "homework.pdf")}, headers=headers)
    assert response.status_code == 202
    return response.json["id"]


def print_payload(upload_id, **extra):
    return {"upload_id": upload_id, "printer": "brother", "request_id": "request_unique_123456", **extra}


def test_basic_reads_and_private_session_cookie(client):
    result = client.get("/api/status")
    assert result.json["cups"]["available"] is True
    assert "csrf_token" in result.json
    cookie = result.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert client.get("/api/printers").status_code == 200
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/healthz").json["ok"] is True


@pytest.mark.parametrize("hostname", ["localhost", "192.168.8.1", "OpenWrt", "print.local", "print.lan", "printer.home.arpa"])
def test_lan_addresses_and_names_are_accepted(client, hostname):
    assert client.get("/api/status", base_url=f"http://{hostname}").status_code == 200


@pytest.mark.parametrize("hostname", ["attacker.example.com", "8.8.8.8"])
def test_public_hostnames_and_addresses_cannot_obtain_browser_token(client, hostname):
    result = client.get("/api/status", base_url=f"http://{hostname}")
    assert result.status_code == 403
    assert "csrf_token" not in result.json


def test_explicitly_configured_hostname_can_be_used(app, client):
    app.config["ALLOWED_HOSTS"] = ["printing.example.com"]
    assert client.get("/api/status", base_url="http://printing.example.com").status_code == 200


@pytest.mark.parametrize("method,path", [("post", "/api/uploads"), ("post", "/api/print"),
                                         ("post", "/api/jobs/42/cancel"), ("post", "/api/jobs/42/hold"),
                                         ("post", "/api/jobs/44/release"), ("post", "/api/admin/login"),
                                         ("post", "/api/admin/scan"), ("post", "/api/admin/printers"),
                                         ("post", "/api/admin/password"), ("post", "/api/admin/logout"),
                                         ("delete", "/api/uploads/" + "a" * 32)])
def test_every_mutation_requires_browser_token(client, method, path):
    assert getattr(client, method)(path).status_code == 403


@pytest.mark.parametrize("changes", [{"X-CSRF-Token": "incorrect"}, {"Origin": "http://evil.test"},
                                   {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                                   {"Origin": "https://localhost"}, {"X-CSRF-Token": "é"}])
def test_invalid_or_cross_origin_tokens_are_rejected(client, headers, changes):
    result = client.post("/api/jobs/42/cancel", headers={**headers, **changes})
    assert result.status_code == 403


def test_token_is_bound_to_browser_session(app, client, headers):
    another_client = app.test_client()
    assert another_client.post("/api/jobs/42/cancel", headers=headers).status_code == 403


def test_document_ready_preview_works_and_api_is_not_cached(client, uploaded):
    result = client.get(f"/api/uploads/{uploaded}")
    assert result.json["status"] == "ready"
    assert result.json["pages"] == 3
    assert result.headers["Cache-Control"] == "no-store"
    preview = client.get(result.json["preview_url"])
    assert preview.status_code == 200
    assert preview.mimetype == "application/pdf"
    assert preview.data.startswith(b"%PDF")
    assert preview.headers["X-Content-Type-Options"] == "nosniff"


def test_page_preview_is_a_browser_displayable_png(client, uploaded):
    record = client.get(f"/api/uploads/{uploaded}").json
    assert f"/api/uploads/{uploaded}" in record["page_preview_url"]
    result = client.get(record["page_preview_url"])
    assert result.status_code == 200 and result.mimetype == "image/png"
    assert result.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert result.headers["Cache-Control"] == "no-store"
    assert result.headers["X-Content-Type-Options"] == "nosniff"
    with Image.open(io.BytesIO(result.data)) as image:
        image.load()
        assert image.width > 0 and image.height > 0
    assert client.get(f"/api/uploads/{uploaded}/pages/3/preview.png").status_code == 200


@pytest.mark.parametrize("page_number", [0, 4])
def test_page_preview_rejects_pages_outside_the_document(client, uploaded, page_number):
    result = client.get(f"/api/uploads/{uploaded}/pages/{page_number}/preview.png")
    assert result.status_code == 400 and result.json["error"]


def test_missing_or_unready_document_cannot_expose_page_preview(app, client, uploaded):
    assert client.get(f"/api/uploads/{'a' * 32}/pages/1/preview.png").status_code == 404
    assert client.get("/api/uploads/invalid-id/pages/1/preview.png").status_code == 404
    store = app.extensions["uploads"]
    record = store.get(uploaded)
    record["status"] = "converting"
    store.write(record)
    result = client.get(f"/api/uploads/{uploaded}/pages/1/preview.png")
    assert result.status_code == 409 and result.json["error"]


@pytest.mark.parametrize("width,height", [(595, 100000), (595, 2000)])
def test_page_preview_bounds_the_pixmap_before_rendering(client, headers, monkeypatch, width, height):
    with fitz.open() as document:
        document.new_page(width=width, height=height).insert_text((40, 40), "A tall printable page")
        original = document.tobytes()
    upload = client.post("/api/uploads", data={"file": (io.BytesIO(original), "tall.pdf")}, headers=headers)
    assert upload.status_code == 202
    assert client.get(f"/api/uploads/{upload.json['id']}").json["status"] == "ready"
    original_get_pixmap = fitz.Page.get_pixmap
    dimensions = []

    def bounded_pixmap(page, *args, **kwargs):
        rectangle = (page.rect * kwargs["matrix"]).irect
        assert 0 < rectangle.width <= 1200
        assert 0 < rectangle.height <= 4096
        assert rectangle.width * rectangle.height <= 4_000_000
        dimensions.append((rectangle.width, rectangle.height))
        return original_get_pixmap(page, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_pixmap", bounded_pixmap)
    result = client.get(f"/api/uploads/{upload.json['id']}/pages/1/preview.png")
    assert result.status_code == 200
    with Image.open(io.BytesIO(result.data)) as image:
        image.load()
        assert image.size == dimensions[0]


def test_valid_print_sends_only_validated_string_options(app, client, headers, uploaded):
    payload = print_payload(uploaded, copies=2, page_ranges="1-2, 3", sides="two-sided-long-edge",
                            media="iso_a4_210x297mm", color_mode="monochrome", orientation="landscape",
                            fit_to_page=False, advanced_options={"number-up": "1"}, arbitrary_cups_option="malicious")
    result = client.post("/api/print", json=payload, headers=headers)
    assert result.json["job_id"] == 42
    printer, path, title, options = app.extensions["cups_client"].prints[0]
    assert printer == "brother" and title == "homework.pdf"
    assert path.name == "document.pdf"
    assert options["copies"] == "2" and options["page-ranges"] == "1-2,3"
    assert options["number-up"] == "1" and options["orientation-requested"] == "4"
    assert options["print-scaling"] == "none"
    assert "arbitrary_cups_option" not in options
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in options.items())


def test_retry_does_not_submit_duplicate_print(app, client, headers, uploaded):
    payload = print_payload(uploaded)
    first = client.post("/api/print", json=payload, headers=headers)
    second = client.post("/api/print", json=payload, headers=headers)
    assert first.json["job_id"] == second.json["job_id"] == 42
    assert second.json["reused"] is True
    assert len(app.extensions["cups_client"].prints) == 1


def test_auto_settings_leave_printer_and_driver_defaults_in_control(app, client, headers, uploaded):
    result = client.post("/api/print", json=print_payload(
        uploaded, sides="auto", color_mode="", media="auto", orientation="",
        advanced_options={"printer-resolution": "auto", "print-quality": "", "number-up": "", "print-scaling": "auto"}),
        headers=headers)
    assert result.status_code == 200
    assert app.extensions["cups_client"].prints[0][3] == {"copies": "1", "job-sheets": "none,none"}


def test_explicit_dpi_quality_driver_and_layout_options_reach_cups(app, client, headers, uploaded):
    selected = {"printer-resolution": "600dpi", "print-quality": "5", "BrMediaType": "THICK", "number-up": "4", "print-scaling": "fill"}
    result = client.post("/api/print", json=print_payload(uploaded, advanced_options=selected), headers=headers)
    assert result.status_code == 200
    assert all(app.extensions["cups_client"].prints[0][3][key] == value for key, value in selected.items())


@pytest.mark.parametrize("main,driver", [("sides", "Duplex"), ("color_mode", "ColorModel"),
                                         ("media", "PageSize"), ("media", "PageRegion")])
def test_main_and_unique_driver_controls_cannot_both_be_explicit(app, client, headers, uploaded, main, driver):
    caps = app.extensions["cups_client"].printer["capabilities"]
    caps["print_options"].append({"key": driver, "choices": [{"value": "VendorSpecial"}]})
    selected = {"sides": "one-sided", "color_mode": "monochrome", "media": "iso_a4_210x297mm"}[main]
    payload = print_payload(uploaded, **{main: selected}, advanced_options={driver: "VendorSpecial"})
    result = client.post("/api/print", json=payload, headers=headers)
    assert result.status_code == 400 and "自動" in result.json["error"]
    assert app.extensions["cups_client"].prints == []
    assert app.extensions["uploads"].get(uploaded)["requests"] == {}


@pytest.mark.parametrize("main,driver", [("sides", "Duplex"), ("color_mode", "ColorModel"),
                                         ("media", "PageSize"), ("media", "PageRegion")])
@pytest.mark.parametrize("auto", ["", "auto"])
def test_unique_driver_mode_is_allowed_when_main_control_is_auto(app, client, headers, uploaded, main, driver, auto):
    caps = app.extensions["cups_client"].printer["capabilities"]
    caps["print_options"].append({"key": driver, "choices": [{"value": "VendorSpecial"}]})
    result = client.post("/api/print", json=print_payload(uploaded, **{main: auto}, advanced_options={driver: "VendorSpecial"}), headers=headers)
    assert result.status_code == 200
    options = app.extensions["cups_client"].prints[0][3]
    assert options[driver] == "VendorSpecial"
    assert {"sides", "print-color-mode", "media"}.isdisjoint(options)


def test_driver_auto_does_not_conflict_with_explicit_main_setting(app, client, headers, uploaded):
    caps = app.extensions["cups_client"].printer["capabilities"]
    caps["print_options"].append({"key": "Duplex", "choices": [{"value": "Booklet"}]})
    result = client.post("/api/print", json=print_payload(uploaded, sides="one-sided", advanced_options={"Duplex": "auto"}), headers=headers)
    assert result.status_code == 200
    options = app.extensions["cups_client"].prints[0][3]
    assert options["sides"] == "one-sided" and "Duplex" not in options


def test_custom_paper_and_driver_page_size_are_mutually_exclusive(app, client, headers, uploaded):
    caps = app.extensions["cups_client"].printer["capabilities"]
    caps["custom_media"] = {"min_width": 89, "max_width": 215.9, "min_height": 127, "max_height": 1200, "unit": "mm"}
    caps["print_options"].append({"key": "PageSize", "choices": [{"value": "VendorSpecial"}]})
    payload = print_payload(uploaded, media="custom", custom_media={"width_mm": 100, "height_mm": 150},
                            advanced_options={"PageSize": "VendorSpecial"})
    result = client.post("/api/print", json=payload, headers=headers)
    assert result.status_code == 400 and "自動" in result.json["error"]
    assert app.extensions["cups_client"].prints == []


@pytest.mark.parametrize("orientation,expected", [("portrait", "3"), ("landscape", "4"), ("reverse-portrait", "6"), ("reverse-landscape", "5")])
def test_all_four_paper_directions(app, client, headers, uploaded, orientation, expected):
    result = client.post("/api/print", json=print_payload(uploaded, orientation=orientation), headers=headers)
    assert result.status_code == 200
    assert app.extensions["cups_client"].prints[0][3]["orientation-requested"] == expected


@pytest.mark.parametrize("selected", [
    {"printer-resolution": "1200dpi"}, {"printer-resolution": 600}, {"printer-resolution": None},
    {"printer-resolution": "600dpi job-hold-until=indefinite"}, {"printer-resolution": "600dpi\nDefault other"},
    {"unsupported": "auto"}, {"device-uri": "ipp://other"}, {"printer-is-accepting-jobs": "false"},
    {"print-quality": "best"}, {"BrMediaType": "color"}, ["printer-resolution"], "600dpi", None,
])
def test_invalid_advanced_settings_never_submit(app, client, headers, uploaded, selected):
    result = client.post("/api/print", json=print_payload(uploaded, advanced_options=selected), headers=headers)
    assert result.status_code == 400
    assert app.extensions["cups_client"].prints == []


def test_advanced_settings_are_validated_against_current_printer(app, client, headers, uploaded):
    app.extensions["cups_client"].printer["capabilities"]["print_options"][0]["choices"] = [{"value": "300dpi"}]
    result = client.post("/api/print", json=print_payload(uploaded, advanced_options={"printer-resolution": "600dpi"}), headers=headers)
    assert result.status_code == 400
    assert app.extensions["cups_client"].prints == []


def test_advertised_dpi_alias_adds_matching_driver_choice(app, client, headers, uploaded):
    app.extensions["cups_client"].printer["capabilities"]["option_aliases"] = {
        "printer-resolution": {"600dpi": {"Resolution": "600dpi"}}}
    result = client.post("/api/print", json=print_payload(uploaded, advanced_options={"printer-resolution": "600dpi"}), headers=headers)
    assert result.status_code == 200
    options = app.extensions["cups_client"].prints[0][3]
    assert options["printer-resolution"] == options["Resolution"] == "600dpi"


def test_unsafe_advertised_alias_is_rejected(app, client, headers, uploaded):
    app.extensions["cups_client"].printer["capabilities"]["option_aliases"] = {
        "printer-resolution": {"600dpi": {"device-uri": "ipp://other"}}}
    result = client.post("/api/print", json=print_payload(uploaded, advanced_options={"printer-resolution": "600dpi"}), headers=headers)
    assert result.status_code == 400
    assert app.extensions["cups_client"].prints == []


def test_even_advertised_queue_admin_options_cannot_be_injected(app, client, headers, uploaded):
    caps = app.extensions["cups_client"].printer["capabilities"]
    caps["print_options"].append({"key": "printer-is-accepting-jobs", "choices": [{"value": "false"}]})
    result = client.post("/api/print", json=print_payload(uploaded, advanced_options={"printer-is-accepting-jobs": "false"}), headers=headers)
    assert result.status_code == 400
    assert app.extensions["cups_client"].prints == []


def test_custom_paper_uses_advertised_mm_bounds(app, client, headers, uploaded):
    app.extensions["cups_client"].printer["capabilities"]["custom_media"] = {
        "min_width": 89, "max_width": 215.9, "min_height": 127, "max_height": 1200, "unit": "mm"}
    result = client.post("/api/print", json=print_payload(uploaded, media="custom", custom_media={"width_mm": 100, "height_mm": 148.5}), headers=headers)
    assert result.status_code == 200
    assert app.extensions["cups_client"].prints[0][3]["media"] == "Custom.100x148.5mm"


@pytest.mark.parametrize("dimensions", [{"width_mm": 88, "height_mm": 150}, {"width_mm": 100, "height_mm": 1201},
                                      {"width_mm": "100", "height_mm": 150}, {"width_mm": True, "height_mm": 150},
                                      {"width_mm": 100}, {"width_mm": 10 ** 400, "height_mm": 150}, {}, None])
def test_invalid_custom_paper_never_submits(app, client, headers, uploaded, dimensions):
    app.extensions["cups_client"].printer["capabilities"]["custom_media"] = {
        "min_width": 89, "max_width": 215.9, "min_height": 127, "max_height": 1200, "unit": "mm"}
    result = client.post("/api/print", json=print_payload(uploaded, media="custom", custom_media=dimensions), headers=headers)
    assert result.status_code == 400
    assert app.extensions["cups_client"].prints == []


def test_custom_paper_requires_reported_support(app, client, headers, uploaded):
    result = client.post("/api/print", json=print_payload(uploaded, media="custom", custom_media={"width_mm": 100, "height_mm": 150}), headers=headers)
    assert result.status_code == 400


@pytest.mark.parametrize("native", [False, True])
def test_driver_conflict_rejects_both_paths_before_marking_request(app, client, headers, uploaded, monkeypatch, native):
    store = app.extensions["uploads"]
    if native:
        monkeypatch.setattr(store, "capabilities", lambda: {"office": True})
        monkeypatch.setattr(store, "_office_preview", lambda source, target, directory: target.write_bytes(pdf_bytes()))
        uploaded = client.post("/api/uploads", data={"file": (io.BytesIO(office_bytes("docx")), "document.docx")}, headers=headers).json["id"]
    checker = Mock(side_effect=ValueError("紙張與雙面設定衝突"))
    monkeypatch.setattr(app.extensions["cups_client"], "validate_job_options", checker, raising=False)
    native_print = Mock(return_value=80)
    monkeypatch.setattr(app.extensions["native_printer"], "print_file", native_print)
    payload = print_payload(uploaded)
    rejected = client.post("/api/print", json=payload, headers=headers)
    assert rejected.status_code == 400 and "衝突" in rejected.json["error"]
    assert store.get(uploaded)["requests"] == {}
    assert uploaded not in store.active_prints
    assert app.extensions["cups_client"].prints == []
    native_print.assert_not_called()
    # Correcting the setting can reuse the same request ID: it was never submitted.
    checker.side_effect = None
    accepted = client.post("/api/print", json=payload, headers=headers)
    assert accepted.status_code == 200
    assert store.get(uploaded)["requests"][payload["request_id"]] == (80 if native else 42)


@pytest.mark.parametrize("change", [{"copies": 0}, {"copies": 100}, {"copies": True}, {"copies": 1.5},
                                  {"copies": "2"}, {"page_ranges": "0"}, {"page_ranges": "2-1"},
                                  {"page_ranges": "4"}, {"page_ranges": "1,,2"}, {"page_ranges": [1]},
                                  {"sides": "invalid"}, {"color_mode": "color"}, {"media": "invalid"},
                                  {"orientation": "diagonal"}, {"orientation": ["portrait"]},
                                  {"fit_to_page": "true"}, {"request_id": "short"},
                                  {"printer": "unknown"}, {"sides": ["one-sided"]}])
def test_invalid_print_settings_never_reach_printer(app, client, headers, uploaded, change):
    result = client.post("/api/print", json=print_payload(uploaded, **change), headers=headers)
    assert result.status_code == 400
    assert result.json["error"]
    assert app.extensions["cups_client"].prints == []


def test_unready_document_cannot_preview_or_print(app, client, headers, uploaded):
    store = app.extensions["uploads"]
    record = store.get(uploaded)
    record["status"] = "converting"
    store.write(record)
    assert client.get(f"/api/uploads/{uploaded}/pdf").status_code == 409
    assert client.post("/api/print", json=print_payload(uploaded), headers=headers).status_code == 409
    assert client.delete(f"/api/uploads/{uploaded}", headers=headers).status_code == 400
    assert app.extensions["cups_client"].prints == []


def test_missing_and_malformed_uploads_have_clear_errors(client, headers):
    assert client.post("/api/uploads", headers=headers).status_code == 400
    assert client.post("/api/uploads", data={"file": (io.BytesIO(b""), "blank.pdf")}, headers=headers).status_code == 400
    assert client.post("/api/uploads", data={"file": (io.BytesIO(b"run"), "malware.exe")}, headers=headers).status_code == 400
    invalid = client.post("/api/uploads", data={"file": (io.BytesIO(b"fake PDF"), "bad.pdf")}, headers=headers)
    assert invalid.status_code == 202
    assert client.get("/api/uploads/" + invalid.json["id"]).json["status"] == "error"
    assert client.post("/api/print", json=print_payload(invalid.json["id"]), headers=headers).status_code == 409


def test_full_conversion_queue_asks_the_browser_to_retry_later(app, client, headers):
    assert client.get("/api/status").json["max_pending_uploads"] == 8
    store = app.extensions["uploads"]
    store.slots = threading.BoundedSemaphore(1)
    store.slots.acquire()
    result = client.post("/api/uploads", data={"file": (io.BytesIO(pdf_bytes()), "second.pdf")}, headers=headers)
    assert result.status_code == 429
    assert result.headers["Retry-After"] == "5"
    assert "稍後" in result.json["error"]
    assert list(store.root.iterdir()) == []
    store.slots.release()
    assert client.post("/api/uploads", data={"file": (io.BytesIO(pdf_bytes()), "second.pdf")}, headers=headers).status_code == 202


def test_oversized_upload_returns_json_without_retaining_files(app, client, headers):
    result = client.post("/api/uploads", data={"file": (io.BytesIO(b"x" * (1200 * 1024)), "big.pdf")}, headers=headers)
    assert result.status_code == 413
    assert "上限" in result.json["error"]
    assert list(app.extensions["uploads"].root.iterdir()) == []


def test_nonexistent_ids_and_removed_files_are_not_accessible(client, headers, uploaded):
    assert client.get("/api/uploads/" + "0" * 32).status_code == 404
    assert client.get("/api/uploads/not-a-valid-id").status_code == 404
    assert client.post("/api/print", json=print_payload("../session.key"), headers=headers).status_code == 404
    assert client.delete(f"/api/uploads/{uploaded}", headers=headers).json["ok"] is True
    assert client.get(f"/api/uploads/{uploaded}/pdf").status_code == 404


@pytest.mark.parametrize("state,accepting", [(5, True), (3, False)])
def test_paused_or_rejecting_printer_is_not_used(app, client, headers, uploaded, state, accepting):
    app.extensions["cups_client"].printer.update(state=state, accepting=accepting)
    assert client.post("/api/print", json=print_payload(uploaded), headers=headers).status_code == 400
    assert app.extensions["cups_client"].prints == []


def test_unavailable_cups_reports_actionable_error(app, client, headers, uploaded):
    app.extensions["cups_client"].error = "無法連接 CUPS"
    assert client.get("/api/status").json["cups"]["available"] is False
    result = client.post("/api/print", json=print_payload(uploaded), headers=headers)
    assert result.status_code == 503 and "CUPS" in result.json["error"]


def test_cancellation_only_uses_known_active_jobs(app, client, headers):
    assert client.post("/api/jobs/999/cancel", headers=headers).status_code == 404
    assert client.post("/api/jobs/43/cancel", headers=headers).status_code == 409
    assert client.post("/api/jobs/42/cancel", headers=headers).json["ok"] is True
    assert app.extensions["cups_client"].cancelled == [42]


def test_only_active_jobs_can_be_paused_and_only_paused_jobs_resumed(app, client, headers):
    assert client.post("/api/jobs/999/hold", headers=headers).status_code == 404
    assert client.post("/api/jobs/43/hold", headers=headers).status_code == 409
    assert client.post("/api/jobs/44/hold", headers=headers).status_code == 409
    assert client.post("/api/jobs/42/release", headers=headers).status_code == 409
    assert client.post("/api/jobs/42/hold", headers=headers).json["ok"] is True
    assert client.post("/api/jobs/44/release", headers=headers).json["ok"] is True
    assert app.extensions["cups_client"].cancelled == [("hold", 42), ("release", 44)]


def test_printers_cannot_be_added_without_the_admin_page(app, client, headers):
    for path in ("/api/printers", "/api/discovery/scan"):
        assert client.post(path, json={"device_id": "discovered_device", "driver": "everywhere"}, headers=headers).status_code in {404, 405}
    assert client.get("/api/discovery").status_code == 404
    assert client.get("/api/drivers").status_code == 404
    assert client.post("/api/admin/printers", json={"source": "discovered", "device_id": "discovered_device",
                                                    "driver": "everywhere"}, headers=headers).status_code == 401
    assert app.extensions["cups_client"].added == []


@pytest.mark.parametrize("payload", [None, [], "text", 42])
def test_print_requires_json_object(client, headers, payload):
    assert client.post("/api/print", json=payload, headers=headers).status_code == 400


def test_office_preview_and_native_print_use_their_own_files(app, client, headers, monkeypatch):
    monkeypatch.setattr(app.extensions["uploads"], "capabilities", lambda: {"office": True})
    monkeypatch.setattr(app.extensions["uploads"], "_office_preview",
                        lambda source, target, directory: target.write_bytes(pdf_bytes()))
    calls = []

    def native_print(printer, source, title, options, request_id):
        assert source.suffix == ".pptx" and source.is_file()
        calls.append((printer, source, options, request_id))
        return 80

    monkeypatch.setattr(app.extensions["native_printer"], "print_file", native_print)
    response = client.post("/api/uploads", data={"file": (io.BytesIO(office_bytes("pptx")), "slides.pptx")}, headers=headers)
    upload_id = response.json["id"]
    assert client.get(f"/api/uploads/{upload_id}").json["native"] is True
    assert client.get(f"/api/uploads/{upload_id}/pdf").status_code == 200
    payload = print_payload(upload_id, page_ranges="1-2,3", copies=2)
    assert client.post("/api/print", json=payload, headers=headers).json["job_id"] == 80
    assert client.post("/api/print", json=payload, headers=headers).json["reused"] is True
    assert len(calls) == 1
    assert app.extensions["cups_client"].prints == []


def test_ambiguous_submission_is_never_automatically_retried(app, client, headers, uploaded, monkeypatch):
    calls = []

    def failing_print(*args):
        calls.append(args)
        raise CupsError("提交結果尚未確認")

    monkeypatch.setattr(app.extensions["cups_client"], "print_file", failing_print)
    payload = print_payload(uploaded)
    assert client.post("/api/print", json=payload, headers=headers).status_code == 503
    retry = client.post("/api/print", json=payload, headers=headers)
    assert retry.status_code == 409 and "重複打印" in retry.json["error"]
    assert len(calls) == 1


def test_inflight_print_remains_visible_but_cannot_be_deleted_or_submitted_again(app, client, headers, uploaded, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_print(*args):
        started.set()
        assert release.wait(timeout=5)
        return 42

    def first_request():
        browser = app.test_client()
        token = browser.get("/api/status").json["csrf_token"]
        return browser.post("/api/print", json=print_payload(uploaded), headers={"X-CSRF-Token": token})

    monkeypatch.setattr(app.extensions["cups_client"], "print_file", blocked_print)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_request)
        try:
            assert started.wait(timeout=2)
            assert client.get(f"/api/uploads/{uploaded}").status_code == 200
            assert client.get("/api/jobs").status_code == 200
            assert client.delete(f"/api/uploads/{uploaded}", headers=headers).status_code == 400
            another = print_payload(uploaded, request_id="another_request_123456")
            assert client.post("/api/print", json=another, headers=headers).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=5).json["job_id"] == 42
    assert client.delete(f"/api/uploads/{uploaded}", headers=headers).status_code == 200
