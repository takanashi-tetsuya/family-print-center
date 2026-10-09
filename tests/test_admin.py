import gzip
import io
import json
import re
import socket
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from print_center import admin
from print_center.admin import (GENERIC_PCL, GENERIC_POSTSCRIPT, automatic_driver, build_driver_index, build_uri, driver_candidates,
                                parse_device_id, parse_ipp, queue_name, resolve_printer_host, search_drivers,
                                validate_ppd)
from print_center.app import create_app
from print_center.cups_client import CupsClient
from print_center.discovery import Discovery

PPDS = {
    "everywhere-is-not-a-ppd": {"ppd-make": "Other", "ppd-make-and-model": "Other"},
    GENERIC_POSTSCRIPT: {"ppd-make": "Generic", "ppd-make-and-model": "Generic PostScript Printer",
                         "ppd-device-id": "MFG:Generic;CMD:PS;"},
    GENERIC_PCL: {"ppd-make": "Generic", "ppd-make-and-model": "Generic PCL Laser Printer",
                  "ppd-device-id": "MFG:Generic;CMD:PCL;"},
    "drv:///brlaser.drv/br2700.ppd": {"ppd-make": "Brother", "ppd-make-and-model": "Brother MFC-L2700DW series, using brlaser v6",
                                       "ppd-device-id": "MFG:Brother;CMD:PJL,HBP;MDL:MFC-L2700DW series;",
                                       "ppd-natural-language": "en"},
    "drv:///hpcups.drv/hp-laserjet_pro_m404-m405.ppd": {"ppd-make": "HP", "ppd-make-and-model": "HP LaserJet Pro M404-M405, hpcups 3.24.4",
                                                         "ppd-device-id": "MFG:HP;MDL:HP LaserJet Pro M404-M405;",
                                                         "ppd-natural-language": "en"},
    "foomatic-db-compressed-ppds:0/ppd/foomatic-ppd/Kyocera-ECOSYS_P2135dn-Postscript.ppd": {
        "ppd-make": "Kyocera", "ppd-make-and-model": "Kyocera ECOSYS P2135dn Foomatic/Postscript (recommended)",
        "ppd-device-id": "MFG:Kyocera;MDL:ECOSYS P2135dn;", "ppd-natural-language": "en"},
}
PPD_TEXT = b'*PPD-Adobe: "4.3"\n*NickName: "Example Laser PS"\n*ModelName: "Example Laser"\n*OpenUI *PageSize: PickOne\n'


class AdminCups:
    def __init__(self):
        self.added, self.modified, self.actions, self.cancelled = [], [], [], []
        self.printers = [{"name": "brother", "info": "Brother MFC", "location": "", "make_model": "Brother",
                          "uri": "usb://Brother/MFC-L2700DW", "connection": "USB 連接線", "state": 3,
                          "state_label": "待命", "message": "", "reasons": [], "accepting": True, "is_default": True}]

    def status(self):
        return {"available": True, "error": None}

    def printers_for_print(self):
        return {"printers": [], "default": None}

    def printers(self):
        return {"printers": [], "default": None}

    def jobs(self):
        return {"jobs": []}

    def ppds(self):
        return PPDS

    def admin_printers(self):
        return {"printers": [dict(item) for item in self.printers], "default": "brother"}

    def add_printer(self, name, uri, driver, **options):
        self.added.append((name, uri, driver, options))
        return {"name": name}

    def modify_printer(self, *arguments):
        self.modified.append(arguments)
        return {"name": arguments[0]}

    def printer_action(self, name, action):
        self.actions.append((name, action))
        return {"job_id": 7} if action == "test" else {"ok": True}

    def all_jobs(self):
        return {"jobs": [{"id": 5, "title": "FP-abcdefghijklmnop12 homework.pdf", "can_cancel": True}]}

    def cancel_any_job(self, job_id):
        self.cancelled.append(job_id)


class AdminDiscovery:
    def __init__(self):
        self.started = 0
        self.devices = {"usb_brother": {"id": "usb_brother", "label": "Brother MFC-L2700DW series", "kind": "usb",
                                        "uri": "usb://Brother/MFC-L2700DW%20series?serial=1", "driverless": False,
                                        "make_model": "Brother MFC-L2700DW series",
                                        "device_id": "MFG:Brother;CMD:PJL,HBP;MDL:MFC-L2700DW series;"},
                        "epson": {"id": "epson", "label": "Epson ET-2850", "kind": "ipp",
                                  "uri": "dnssd://Epson._ipp._tcp.local./", "driverless": True}}

    def snapshot(self):
        return {"devices": [dict(device) for device in self.devices.values()], "scanning": False, "error": None}

    def start(self):
        self.started += 1

    def get(self, device_id):
        if not isinstance(device_id, str) or device_id not in self.devices:
            raise ValueError("找不到此打印機，請重新掃描。")
        return dict(self.devices[device_id])


def fake_probe(host):
    resolve_printer_host(host)
    return {"host": host, "make_model": "HP LaserJet Pro M404dn", "device_id": "MFG:HP;MDL:HP LaserJet Pro M404dn;CMD:PCL,POSTSCRIPT;",
            "pdl": "", "driverless": False,
            "connections": [{"protocol": "socket", "port": 9100, "path": "", "label": "直接傳送", "uri": f"socket://{host}:9100"}]}


@pytest.fixture
def app(tmp_path):
    value = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "CONVERSION_SYNC": True, "MAX_UPLOAD_MB": 1,
                        "SECRET_KEY": "test-session-secret", "PRINTER_PROBE": fake_probe},
                       cups_client=AdminCups(), discovery=AdminDiscovery())
    yield value
    value.extensions["uploads"].executor.shutdown(wait=True)


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client):
    return {"X-CSRF-Token": client.get("/api/admin/session").json["csrf_token"], "Origin": "http://localhost"}


def initial_password(app):
    return (Path(app.config["DATA_DIR"]) / "admin-initial-password").read_text().strip()


def login(client, password, address="192.168.1.20"):
    return client.post("/api/admin/login", json={"password": password}, headers=csrf(client),
                       environ_base={"REMOTE_ADDR": address})


@pytest.fixture
def signed_in(app, client):
    """An administrator who already replaced the initial password."""
    assert login(client, initial_password(app)).status_code == 200
    result = client.post("/api/admin/password", json={"current": initial_password(app), "new": "family-printer-9"},
                         headers=csrf(client))
    assert result.status_code == 200
    return csrf(client)


ADMIN_ENDPOINTS = [("get", "/api/admin/overview"), ("get", "/api/admin/printers"), ("get", "/api/admin/scan"),
                   ("post", "/api/admin/scan"), ("get", "/api/admin/drivers?q=brother"), ("get", "/api/admin/ppds"),
                   ("post", "/api/admin/ppds"), ("delete", "/api/admin/ppds/" + "a" * 24), ("post", "/api/admin/probe"),
                   ("post", "/api/admin/printers"), ("patch", "/api/admin/printers/brother"),
                   ("post", "/api/admin/printers/brother/test"), ("delete", "/api/admin/printers/brother"),
                   ("get", "/api/admin/jobs"), ("post", "/api/admin/jobs/5/cancel"), ("post", "/api/admin/password"),
                   ("get", "/api/admin/fonts"), ("post", "/api/admin/fonts"), ("delete", "/api/admin/fonts/" + "a" * 24)]


# --- Authentication ---------------------------------------------------------

def test_first_start_creates_private_hashed_credentials(app):
    data = Path(app.config["DATA_DIR"])
    password = initial_password(app)
    assert len(password) >= 12
    for name in ("admin.json", "admin-initial-password"):
        assert (data / name).stat().st_mode & 0o777 == 0o600
    record = json.loads((data / "admin.json").read_text())
    assert password not in json.dumps(record)
    assert record["hash"].startswith(("scrypt:", "pbkdf2:"))
    assert record["must_change"] is True


def test_configured_password_is_used_once_and_not_written_to_disk(tmp_path):
    value = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "SECRET_KEY": "x", "ADMIN_PASSWORD": "chosen-by-owner"},
                       cups_client=AdminCups(), discovery=AdminDiscovery())
    assert not (tmp_path / "admin-initial-password").exists()
    assert "chosen-by-owner" not in (tmp_path / "admin.json").read_text()
    client = value.test_client()
    result = login(client, "chosen-by-owner")
    assert result.status_code == 200 and result.json["must_change"] is False
    value.extensions["uploads"].executor.shutdown(wait=True)


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_every_admin_endpoint_requires_login(client, method, path):
    result = getattr(client, method)(path, json={}, headers=csrf(client))
    assert result.status_code == 401
    assert result.json["login"] is True


def test_admin_page_is_served_without_inline_code_and_is_not_cached(client):
    result = client.get("/admin")
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    assert "script-src 'self'" in result.headers["Content-Security-Policy"]
    html = result.get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert " style=" not in html and "<style" not in html
    assert not re.search(r"\son[a-z]+=", html)


def test_wrong_password_is_rejected(client):
    result = login(client, "not-the-password")
    assert result.status_code == 401
    assert client.get("/api/admin/overview").status_code == 401


@pytest.mark.parametrize("password", [None, 42, ["list"], "", "x" * 500])
def test_malformed_passwords_are_rejected(client, password):
    assert login(client, password).status_code == 401


def test_initial_password_must_be_changed_before_managing_printers(app, client):
    assert login(client, initial_password(app)).json["must_change"] is True
    result = client.get("/api/admin/printers")
    assert result.status_code == 403 and result.json["must_change"] is True
    assert client.post("/api/admin/password", json={"current": initial_password(app), "new": "family-printer-9"},
                       headers=csrf(client)).status_code == 200
    assert not (Path(app.config["DATA_DIR"]) / "admin-initial-password").exists()
    assert client.get("/api/admin/printers").status_code == 200


@pytest.mark.parametrize("new,message", [("short", "8"), ("aaaaaaaaaa", "簡單"), (None, "8")])
def test_weak_new_passwords_are_refused(app, client, new, message):
    login(client, initial_password(app))
    result = client.post("/api/admin/password", json={"current": initial_password(app), "new": new}, headers=csrf(client))
    assert result.status_code == 400 and message in result.json["error"]


def test_password_change_requires_current_password(app, client):
    login(client, initial_password(app))
    result = client.post("/api/admin/password", json={"current": "guess", "new": "family-printer-9"}, headers=csrf(client))
    assert result.status_code == 400
    assert login(client, initial_password(app)).status_code == 200


def test_password_change_signs_out_other_browsers(app, client, signed_in):
    other = app.test_client()
    assert login(other, "family-printer-9").status_code == 200
    assert other.get("/api/admin/printers").status_code == 200
    assert client.post("/api/admin/password", json={"current": "family-printer-9", "new": "another-secret-7"},
                       headers=csrf(client)).status_code == 200
    assert client.get("/api/admin/printers").status_code == 200
    assert other.get("/api/admin/printers").status_code == 401


def test_login_rotates_form_token(app, client):
    before = csrf(client)
    login(client, initial_password(app))
    assert client.post("/api/admin/logout", headers=before).status_code == 403


def test_logout_ends_session(client, signed_in):
    assert client.post("/api/admin/logout", headers=signed_in).status_code == 200
    assert client.get("/api/admin/printers").status_code == 401


def test_repeated_failures_lock_that_address_even_for_the_right_password(app, client):
    for _ in range(5):
        assert login(client, "guess", "192.168.1.66").status_code == 401
    blocked = login(client, initial_password(app), "192.168.1.66")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert login(app.test_client(), initial_password(app), "192.168.1.67").status_code == 200


def test_many_addresses_cannot_bypass_the_global_limit(app, client):
    for number in range(admin.FAILURES_TOTAL):
        login(client, "guess", f"10.0.{number // 200}.{number % 200 + 1}")
    assert login(app.test_client(), initial_password(app), "192.168.1.99").status_code == 429


def test_failures_expire_after_the_window(app, client, monkeypatch):
    for _ in range(5):
        login(client, "guess")
    later = time.time() + admin.FAILURE_WINDOW + 1
    monkeypatch.setattr(admin.time, "time", lambda: later)
    assert login(client, initial_password(app)).status_code == 200


def test_session_expires_after_idle_time(app, client, signed_in, monkeypatch):
    later = time.time() + admin.SESSION_IDLE + 5
    monkeypatch.setattr(admin.time, "time", lambda: later)
    assert client.get("/api/admin/printers").status_code == 401


def test_session_cannot_outlive_absolute_lifetime_even_when_active(app, client, signed_in, monkeypatch):
    start = time.time()
    for step in range(1, 10):
        moment = start + step * (admin.SESSION_IDLE - 60)
        monkeypatch.setattr(admin.time, "time", lambda moment=moment: moment)
        status = client.get("/api/admin/printers").status_code
        if moment - start > admin.SESSION_LIFETIME:
            assert status == 401
            break
        assert status == 200
    else:
        pytest.fail("session never expired")


def test_corrupt_credentials_disable_login_instead_of_resetting(tmp_path):
    (tmp_path / "admin.json").write_text("{broken")
    value = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "SECRET_KEY": "x"},
                       cups_client=AdminCups(), discovery=AdminDiscovery())
    client = value.test_client()
    assert client.get("/api/admin/session").json["configured"] is False
    assert login(client, "anything").status_code == 503
    assert (tmp_path / "admin.json").read_text() == "{broken"
    value.extensions["uploads"].executor.shutdown(wait=True)


def test_admin_requests_from_public_hostnames_are_rejected(client):
    assert client.get("/admin", base_url="http://attacker.example.com").status_code == 403
    assert client.get("/api/admin/session", base_url="http://8.8.8.8").status_code == 403


# --- Printer setup ----------------------------------------------------------

def test_scan_results_recommend_drivers_and_mark_added_printers(app, client, signed_in):
    assert client.post("/api/admin/scan", headers=signed_in).status_code == 202
    devices = {device["id"]: device for device in client.get("/api/admin/scan").json["devices"]}
    assert devices["usb_brother"]["drivers"][0]["id"] == "drv:///brlaser.drv/br2700.ppd"
    assert devices["usb_brother"]["drivers"][0]["match"] == "exact"
    assert devices["epson"]["drivers"][0]["id"] == "everywhere"
    assert devices["usb_brother"]["added_as"] is None
    app.extensions["cups_client"].printers[0]["uri"] = "usb://Brother/MFC-L2700DW%20series?serial=1"
    devices = {device["id"]: device for device in client.get("/api/admin/scan").json["devices"]}
    assert devices["usb_brother"]["added_as"] == "brother"


def test_discovered_printer_is_added_with_matching_driver(app, client, signed_in):
    payload = {"source": "discovered", "device_id": "epson", "driver": "everywhere", "info": "客廳 Epson",
               "location": "客廳", "default": True}
    result = client.post("/api/admin/printers", json=payload, headers=signed_in)
    assert result.status_code == 201
    name, uri, driver, options = app.extensions["cups_client"].added[0]
    assert name == "Epson_ET_2850" and uri.startswith("dnssd://") and driver == "everywhere"
    assert options == {"info": "客廳 Epson", "location": "客廳", "ppd_file": None, "make_default": True}


@pytest.mark.parametrize("change,status", [
    ({"device_id": "unknown"}, 400), ({"device_id": ["epson"]}, 400), ({"driver": "/tmp/evil.ppd"}, 400),
    ({"driver": {"id": "everywhere"}}, 400), ({"driver": "upload:../../etc/passwd"}, 400),
    ({"driver": "upload:" + "a" * 24}, 400), ({"name": "../../path"}, 400), ({"name": "名稱"}, 400),
    ({"name": "x" * 65}, 400), ({"name": "BROTHER"}, 409), ({"info": "x" * 200}, 400), ({"location": 7}, 400),
    ({"device_id": "usb_brother", "driver": "everywhere"}, 400), ({"source": "uri"}, 400),
    ({"name": "printer_fpc_drv_12345678"}, 400),
])
def test_invalid_printer_additions_never_reach_cups(app, client, signed_in, change, status):
    payload = {"source": "discovered", "device_id": "epson", "driver": "everywhere", **change}
    assert client.post("/api/admin/printers", json=payload, headers=signed_in).status_code == status
    assert app.extensions["cups_client"].added == []


def test_same_device_cannot_be_added_twice(app, client, signed_in):
    app.extensions["cups_client"].printers[0]["uri"] = "dnssd://Epson._ipp._tcp.local./"
    result = client.post("/api/admin/printers", json={"source": "discovered", "device_id": "epson", "driver": "everywhere"},
                         headers=signed_in)
    assert result.status_code == 409


def test_manual_network_printer_uses_validated_uri(app, client, signed_in):
    payload = {"source": "manual", "protocol": "socket", "host": "192.168.1.50", "driver": GENERIC_PCL}
    assert client.post("/api/admin/printers", json=payload, headers=signed_in).status_code == 201
    assert app.extensions["cups_client"].added[0][1] == "socket://192.168.1.50:9100"


@pytest.mark.parametrize("host", ["8.8.8.8", "127.0.0.1", "::1", "[::ffff:127.0.0.1]", "0.0.0.0", "224.0.0.1",
                                  "printer.example.com", "192.168.1.5/admin", "a b", "192.168.1.5:631@evil", ""])
def test_manual_printers_must_be_on_the_home_network(app, client, signed_in, host):
    payload = {"source": "manual", "protocol": "ipp", "host": host, "driver": "everywhere"}
    assert client.post("/api/admin/printers", json=payload, headers=signed_in).status_code == 400
    assert app.extensions["cups_client"].added == []


def test_manual_everywhere_requires_ipp(app, client, signed_in):
    payload = {"source": "manual", "protocol": "socket", "host": "192.168.1.50", "driver": "everywhere"}
    assert client.post("/api/admin/printers", json=payload, headers=signed_in).status_code == 400


def test_probe_returns_connections_and_driver_suggestions(client, signed_in):
    result = client.post("/api/admin/probe", json={"host": "192.168.1.50"}, headers=signed_in)
    assert result.status_code == 200
    assert result.json["connections"][0]["uri"] == "socket://192.168.1.50:9100"
    assert result.json["drivers"][0]["id"] == "drv:///hpcups.drv/hp-laserjet_pro_m404-m405.ppd"
    assert result.json["drivers"][0]["match"] == "similar"
    assert client.post("/api/admin/probe", json={"host": "127.0.0.1"}, headers=signed_in).status_code == 400


def test_printer_actions_are_limited_and_target_existing_printers(app, client, signed_in):
    assert client.post("/api/admin/printers/brother/test", headers=signed_in).json["job_id"] == 7
    assert client.post("/api/admin/printers/brother/rename", headers=signed_in).status_code == 404
    assert client.post("/api/admin/printers/missing/test", headers=signed_in).status_code == 404
    assert client.delete("/api/admin/printers/bad%20name", headers=signed_in).status_code == 400
    assert client.delete("/api/admin/printers/BROTHER", headers=signed_in).status_code == 200
    assert app.extensions["cups_client"].actions == [("brother", "test"), ("brother", "delete")]


def test_printer_details_and_driver_can_be_changed(app, client, signed_in):
    result = client.patch("/api/admin/printers/brother", json={"info": "書房", "driver": GENERIC_POSTSCRIPT}, headers=signed_in)
    assert result.status_code == 200
    assert app.extensions["cups_client"].modified == [("brother", "書房", None, GENERIC_POSTSCRIPT, None)]
    assert client.patch("/api/admin/printers/brother", json={"driver": "../x"}, headers=signed_in).status_code == 400


def test_admin_can_list_and_cancel_any_job(app, client, signed_in):
    assert client.get("/api/admin/jobs").json["jobs"][0]["title"] == "homework.pdf"
    assert client.post("/api/admin/jobs/5/cancel", headers=signed_in).status_code == 200
    assert app.extensions["cups_client"].cancelled == [5]


def test_driver_search_matches_words_and_model_numbers(client, signed_in):
    labels = [item["label"] for item in client.get("/api/admin/drivers?q=brother l2700").json["drivers"]]
    assert labels == ["Brother MFC-L2700DW series, using brlaser v6"]
    assert client.get("/api/admin/drivers?q=").json["drivers"] == []


# --- Uploaded drivers -------------------------------------------------------

def upload(client, headers, data, name="printer.ppd"):
    return client.post("/api/admin/ppds", data={"file": (io.BytesIO(data), name)}, headers=headers)


def test_fonts_page_lists_missing_fonts_and_manages_uploaded_ones(app, client, signed_in, monkeypatch):
    from tests.test_font_substitution import TTF, Tools
    fonts = app.extensions["fonts"]
    monkeypatch.setattr("print_center.fonts.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(fonts, "run", Tools())
    assert client.get("/api/admin/fonts").json == {"fonts": [], "missing": [], "limit_mb": 64}
    fonts.note([["華康少女文字W5", ["Noto Sans CJK TC/Regular"]]])
    assert client.get("/api/admin/fonts").json["missing"][0]["name"] == "華康少女文字W5"
    assert client.post("/api/admin/fonts", data={}, headers=signed_in).status_code == 400
    refused = client.post("/api/admin/fonts", data={"file": (io.BytesIO(b"MZ" * 50), "font.ttf")}, headers=signed_in)
    assert refused.status_code == 400 and "字體檔" in refused.json["error"]
    added = client.post("/api/admin/fonts", data={"file": (io.BytesIO(TTF), "kai.ttf")}, headers=signed_in)
    assert added.status_code == 201 and added.json["families"] == ["Family Kai", "家庭楷書"]
    assert [font["id"] for font in client.get("/api/admin/fonts").json["fonts"]] == [added.json["id"]]
    assert client.delete(f"/api/admin/fonts/{added.json['id']}", headers=signed_in).status_code == 200
    assert client.delete(f"/api/admin/fonts/{added.json['id']}", headers=signed_in).status_code == 400
    assert client.get("/api/admin/fonts").json["fonts"] == []


def test_valid_ppd_upload_is_stored_privately_and_usable(app, client, signed_in):
    result = upload(client, signed_in, PPD_TEXT)
    assert result.status_code == 201 and result.json["label"] == "Example Laser PS"
    path = Path(app.config["DATA_DIR"]) / "ppds" / (result.json["id"].split(":")[1] + ".ppd")
    assert path.stat().st_mode & 0o777 == 0o600
    payload = {"source": "manual", "protocol": "socket", "host": "192.168.1.50", "driver": result.json["id"]}
    assert client.post("/api/admin/printers", json=payload, headers=signed_in).status_code == 201
    assert app.extensions["cups_client"].added[0][3]["ppd_file"] == path
    assert client.delete(f"/api/admin/ppds/{path.stem}", headers=signed_in).status_code == 200
    assert not path.exists()


@pytest.mark.parametrize("data,message", [
    (PPD_TEXT + b'*cupsFilter: "application/vnd.cups-raster 0 /tmp/run-me"\n', "過濾程式"),
    (PPD_TEXT + b'*cupsFilter2: "application/pdf application/vnd.cups-pdf 0 -"\n', "過濾程式"),
    (PPD_TEXT + b'*FoomaticRIPCommandLine: "touch /tmp/pwned"\n', "過濾程式"),
    (PPD_TEXT + b'*Include: "/etc/shadow"\n', "過濾程式"),
    (PPD_TEXT + b'*cupsPreFilter: "x 0 y"\n', "過濾程式"),
    (b"#!/bin/sh\nrm -rf /\n", "PPD"),
    (b'*PPD-Adobe: "4.3"\n*OpenUI *PageSize: PickOne\n', "NickName"),
    (PPD_TEXT + b"\x00", "內容"),
])
def test_dangerous_or_invalid_ppds_are_refused(client, signed_in, data, message):
    result = upload(client, signed_in, data)
    assert result.status_code == 400 and message in result.json["error"]


def test_compressed_ppd_is_accepted_but_decompression_bombs_are_not(client, signed_in):
    assert upload(client, signed_in, gzip.compress(PPD_TEXT), "printer.ppd.gz").status_code == 201
    bomb = gzip.compress(PPD_TEXT + b"*% " + b"A" * (3 * 1024 * 1024))
    result = upload(client, signed_in, bomb, "bomb.ppd.gz")
    assert result.status_code == 400 and "2 MB" in result.json["error"]


def test_oversized_ppd_upload_is_refused(client, signed_in):
    assert upload(client, signed_in, PPD_TEXT + b"*% " + b"A" * (2 * 1024 * 1024)).status_code in {400, 413}


@pytest.mark.parametrize("identifier", ["..%2F..%2Fadmin", "a" * 23, "g" * 24, "A" * 24])
def test_uploaded_driver_paths_cannot_escape_storage(app, client, signed_in, identifier):
    result = client.delete(f"/api/admin/ppds/{identifier}", headers=signed_in)
    assert result.status_code in {400, 404}
    assert (Path(app.config["DATA_DIR"]) / "admin.json").exists()


# --- Units --------------------------------------------------------------------

def test_device_id_parsing_accepts_long_and_short_keys():
    assert parse_device_id("MANUFACTURER:HP;MODEL:LaserJet;COMMAND SET:PCL,PS;") == {"MFG": "HP", "MDL": "LaserJet", "CMD": "PCL,PS"}


def test_driver_matching_handles_brand_aliases_and_generic_fallbacks():
    index = build_driver_index(PPDS)
    kyocera = driver_candidates({"device_id": "MFG:Kyocera Mita;MDL:ECOSYS P2135dn;CMD:PJL,KPDL;"}, index, PPDS)
    assert kyocera[0]["label"].startswith("Kyocera ECOSYS P2135dn") and kyocera[0]["match"] == "exact"
    assert GENERIC_POSTSCRIPT in [item["id"] for item in kyocera]
    unknown = driver_candidates({"device_id": "MFG:Acme;MDL:Writer 1;CMD:PCLXL;"}, index, PPDS)
    assert [item["id"] for item in unknown] == [GENERIC_PCL]
    hp = driver_candidates({"make_model": "Hewlett-Packard HP LaserJet Pro M404dn"}, index, PPDS)
    assert hp[0]["id"] == "drv:///hpcups.drv/hp-laserjet_pro_m404-m405.ppd"


def test_matching_driver_precedes_driverless_compatibility():
    index = build_driver_index(PPDS)
    result = driver_candidates({"driverless": True, "device_id": "MFG:Brother;MDL:MFC-L2700DW;"}, index, PPDS)
    assert result[0]["match"] == "exact" and result[1]["id"] == "everywhere"
    assert automatic_driver({"driverless": True, "device_id": "MFG:Brother;MDL:MFC-L2700DW;"}, index, PPDS) == result[0]["id"]


def test_generated_ipp_ppds_are_not_matching_vendor_drivers():
    ppds = {**PPDS, "driverless:ipp://epson.local/ipp/print": {
        "ppd-make": "Epson", "ppd-make-and-model": "Epson L6270 Series, driverless, cups-filters 1.28.17"}}
    device = {"driverless": True, "make_model": "Epson L6270 Series", "pdl": "image/pwg-raster,image/urf,image/jpeg"}
    result = driver_candidates(device, build_driver_index(ppds), ppds)
    assert result[0]["id"] == "everywhere"
    assert not any(item["id"].startswith("driverless:") for item in result)
    assert automatic_driver(device, build_driver_index(ppds), ppds) == "everywhere"


def test_announced_pcl_is_preferred_but_unknown_and_similar_drivers_are_not_automatic():
    device = {"driverless": True, "device_id": "MFG:Brother;MDL:MFC-8515DN;CMD:PJL,PCL,PCLXL,URF;"}
    index = build_driver_index(PPDS)
    assert driver_candidates(device, index, PPDS)[0]["id"] == GENERIC_PCL
    assert automatic_driver(device, index, PPDS) == GENERIC_PCL
    assert GENERIC_POSTSCRIPT not in {item["id"] for item in driver_candidates(device, index, PPDS)}
    assert automatic_driver({"driverless": True, "make_model": "Brother MFC-8515DN"}, index, PPDS) == "everywhere"
    assert automatic_driver({"make_model": "HP LaserJet Pro M404dn"}, index, PPDS) is None


def test_discovered_auto_driver_uses_exact_driver(app, client, signed_in):
    result = client.post("/api/admin/printers", json={"source": "discovered", "device_id": "usb_brother", "driver": "auto"}, headers=signed_in)
    assert result.status_code == 201
    assert app.extensions["cups_client"].added[0][2] == "drv:///brlaser.drv/br2700.ppd"


def test_profiled_add_uses_discovery_ipp_address_and_fixed_device_uri(app, client, signed_in):
    device = app.extensions["discovery"].devices["epson"]
    device["ipp_uri"] = "ipp://epson.local:631/ipp/print"
    add = Mock(return_value={"name": "epson"})
    app.extensions["cups_client"].add_profiled_printer = add
    result = client.post("/api/admin/printers", json={"source": "discovered", "device_id": "epson", "driver": "auto",
                                                    "ipp_uri": "ipp://attacker.invalid/ignored"}, headers=signed_in)
    assert result.status_code == 201
    assert add.call_args.args[1] == device["uri"]
    assert add.call_args.kwargs["ipp_uri"] == device["ipp_uri"]
    assert add.call_args.kwargs["driverless"] is True


def test_manual_explicit_ipp_path_is_validated_by_profile_generation(app, client, signed_in, monkeypatch):
    add = Mock(return_value={"name": "epson"})
    app.extensions["cups_client"].add_profiled_printer = add
    result = client.post("/api/admin/printers", json={"source": "manual", "protocol": "ipp", "host": "192.168.1.50",
                                                    "path": "ipp/port1", "driver": "everywhere"}, headers=signed_in)
    assert result.status_code == 201
    assert add.call_args.args[1] == "ipp://192.168.1.50:631/ipp/port1"
    assert add.call_args.kwargs["ipp_uri"] == add.call_args.args[1]


def test_queue_names_are_ascii_unique_and_case_insensitive():
    assert queue_name("客廳 Epson ET-2850", ["epson_et_2850"]) == "Epson_ET_2850_2"
    assert queue_name("客廳", []) == "printer"
    assert queue_name("3D Printer", []) == "printer_3D_Printer"


@pytest.mark.parametrize("arguments,expected", [
    (("ipp", "192.168.1.5"), "ipp://192.168.1.5:631/ipp/print"),
    (("ipps", "[fe80::1]", 443, "ipp"), "ipps://[fe80::1]:443/ipp"),
    (("socket", "printer.local", None, "ignored"), "socket://printer.local:9100"),
    (("lpd", "192.168.1.5", 515, "BINARY_P1"), "lpd://192.168.1.5:515/BINARY_P1"),
])
def test_manual_uris_are_built_from_parts(arguments, expected):
    assert build_uri(*arguments) == expected


@pytest.mark.parametrize("arguments", [("http", "192.168.1.5"), ("ipp", "192.168.1.5", 0), ("ipp", "192.168.1.5", "631"),
                                       ("ipp", "192.168.1.5", 631, "../admin"), ("ipp", "192.168.1.5", 631, "a?b=1"),
                                       ("ipp", "192.168.1.5", 631, "a#b"), ("lpd", "192.168.1.5", 515, "")])
def test_manual_uri_parts_are_validated(arguments):
    with pytest.raises(ValueError):
        build_uri(*arguments)


def test_local_names_must_resolve_only_to_home_addresses():
    private = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 0))]
    mixed = lambda *args, **kwargs: private() + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    assert resolve_printer_host("Printer.local", private) == ("printer.local", "192.168.1.9")
    assert resolve_printer_host("brother", private) == ("brother", "192.168.1.9")
    with pytest.raises(ValueError):
        resolve_printer_host("printer.local", mixed)


def ipp_response(status, attributes):
    body = struct.pack(">BBHI", 2, 0, status, 1) + b"\x04"
    for name, value in attributes:
        body += bytes([0x41]) + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(value)) + value.encode()
    return body + b"\x03"


def test_ipp_response_parser_reads_model_and_rejects_truncation():
    status, values = parse_ipp(ipp_response(0, [("printer-make-and-model", "Canon G3010"), ("printer-info", "x")]))
    assert status == 0 and values["printer-make-and-model"] == ["Canon G3010"]
    with pytest.raises(ValueError):
        parse_ipp(ipp_response(0, [("printer-make-and-model", "Canon")])[:-8])


def test_probe_combines_open_ports_and_ipp_answer(monkeypatch):
    monkeypatch.setattr(admin, "_port_open", lambda address, port, timeout: port in {631, 9100})
    monkeypatch.setattr(admin, "_ipp_query", lambda address, host, port, path, secure, timeout:
                        {"printer-make-and-model": ["Canon G3010"], "document-format-supported": ["image/urf"],
                         "urf-supported": ["W8"]} if (path, secure) == ("ipp/print", False) else None)
    result = admin.probe_printer("192.168.1.30")
    assert result["make_model"] == "Canon G3010" and result["driverless"] is True
    assert [item["uri"] for item in result["connections"]] == ["ipp://192.168.1.30:631/ipp/print", "socket://192.168.1.30:9100"]


def test_ppd_validator_reports_nickname():
    assert validate_ppd(PPD_TEXT)[1] == "Example Laser PS"


# --- CUPS adapter -------------------------------------------------------------

def adapter(native):
    client = CupsClient.__new__(CupsClient)
    client.user, client.host, client.port = "familyprint", "127.0.0.1", 631
    client.cups = SimpleNamespace(Connection=Mock(return_value=native), setUser=Mock(), setPasswordCB=Mock())
    return client


def test_no_password_file_means_libcups_never_prompts(monkeypatch):
    monkeypatch.delenv("CUPS_PASSWORD_FILE", raising=False)
    client = adapter(Mock())
    client._connection()
    callback = client.cups.setPasswordCB.call_args.args[0]
    assert callback("Password for familyprint?") == ""


def test_adding_uses_named_driver_or_uploaded_file():
    native = Mock()
    native.getDefault.return_value = None
    adapter(native).add_printer("epson", "dnssd://Epson/", "everywhere", info="客廳", location="", make_default=False)
    native.addPrinter.assert_called_once_with("epson", device="dnssd://Epson/", info="客廳", location="", ppdname="everywhere")
    native.setDefault.assert_called_once_with("epson")
    native = Mock()
    adapter(native).add_printer("laser", "socket://192.168.1.5:9100", "upload:x", ppd_file=Path("/data/ppds/a.ppd"))
    assert native.addPrinter.call_args.kwargs["filename"] == "/data/ppds/a.ppd"
    assert "ppdname" not in native.addPrinter.call_args.kwargs
    native.setDefault.assert_not_called()


def test_add_failure_gives_plain_message_without_internal_details():
    native = Mock()
    native.addPrinter.side_effect = RuntimeError("client-error-not-possible /etc/cups/ppd/x.ppd")
    with pytest.raises(Exception) as caught:
        adapter(native).add_printer("x", "ipp://192.168.1.5:631/ipp/print", "everywhere")
    assert "/etc/cups" not in str(caught.value) and "打印機" in str(caught.value)


def test_detected_devices_skip_placeholders_and_public_hosts():
    native = Mock()
    native.getDevices.return_value = {
        "usb://Brother/MFC-L2700DW%20series?serial=1": {"device-make-and-model": "Brother MFC-L2700DW series",
                                                         "device-info": "Brother MFC-L2700DW series",
                                                         "device-id": "MFG:Brother;MDL:MFC-L2700DW series;"},
        "socket://192.168.1.40": {"device-make-and-model": "HP LaserJet", "device-info": "HP LaserJet 192.168.1.40",
                                  "device-id": "MFG:HP;MDL:LaserJet;"},
        "socket://8.8.4.4": {"device-make-and-model": "Remote", "device-info": "Remote"},
        "socket": {"device-make-and-model": "Unknown", "device-info": "AppSocket/HP JetDirect"},
        "file:/etc/passwd": {"device-make-and-model": "Unknown", "device-info": "File"},
    }
    devices = adapter(native).devices()
    assert [device["uri"] for device in devices] == ["usb://Brother/MFC-L2700DW%20series?serial=1", "socket://192.168.1.40"]
    assert devices[0]["kind"] == "usb" and devices[1]["kind"] == "network"
    assert "dnssd" in native.getDevices.call_args.kwargs["exclude_schemes"]


def test_admin_printer_list_hides_uri_credentials_and_translates_reasons():
    native = Mock()
    native.getDefault.return_value = "office"
    native.getPrinters.return_value = {"office": {"device-uri": "smb://user:secret@nas/printer", "printer-state": 5,
                                                  "printer-state-reasons": ["media-empty-error", "toner-low-warning"],
                                                  "printer-info": "Office"}}
    native.getPrinterAttributes.return_value = {"printer-is-accepting-jobs": False}
    printer = adapter(native).admin_printers()["printers"][0]
    assert "secret" not in printer["uri"]
    assert printer["reasons"] == ["缺紙", "碳粉快用完"]
    assert printer["accepting"] is False and printer["is_default"] is True


def test_job_lists_show_newest_jobs_even_with_long_history():
    native = Mock()
    native.getJobs.return_value = {number: {"job-state": 9} for number in range(1, 151)}
    client = adapter(native)
    assert [job["id"] for job in client.jobs()["jobs"]][:2] == [150, 149]
    assert len(client.jobs()["jobs"]) == 50
    assert "limit" not in native.getJobs.call_args.kwargs
    assert client.all_jobs()["jobs"][0]["id"] == 150


def test_discovery_merges_usb_devices_with_network_results(monkeypatch):
    monkeypatch.setattr("print_center.discovery.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    usb = {"label": "Brother", "uri": "usb://Brother/X", "kind": "usb", "driverless": False, "identity": "usb://Brother/X", "priority": 10}
    discovery = Discovery(sources=[lambda: [usb]])
    discovery.scanning = True
    discovery._scan()
    devices = discovery.snapshot()["devices"]
    assert len(devices) == 1 and devices[0]["uri"] == "usb://Brother/X" and len(devices[0]["id"]) == 24
    assert discovery.get(devices[0]["id"])["kind"] == "usb"


def test_similar_models_must_share_the_whole_model_number():
    ppds = {"a": {"ppd-make": "HP", "ppd-make-and-model": "HP LaserJet Pro M402-M403 Postscript (recommended)",
                  "ppd-device-id": "MFG:Hewlett-Packard;MDL:hp-laserjet_pro_m402-m403;"},
            "b": {"ppd-make": "HP", "ppd-make-and-model": "HP LaserJet Pro M404-M405 Postscript (recommended)",
                  "ppd-device-id": "MFG:Hewlett-Packard;MDL:hp-laserjet_pro_m404-m405;"},
            "c": {"ppd-make": "HP", "ppd-make-and-model": "HP LaserJet Foomatic/laserjet (recommended)"}}
    result = driver_candidates({"device_id": "MFG:HP;MDL:HP LaserJet Pro M404dn;"}, build_driver_index(ppds), ppds)
    assert [item["id"] for item in result] == ["b"]


def test_duplicate_driver_copies_are_offered_once():
    ppds = {"vendor/brother.ppd": {"ppd-make": "Brother", "ppd-make-and-model": "Brother MFC-8515DN for CUPS"},
            "lsb/usr/brother/brother.ppd": {"ppd-make": "Brother", "ppd-make-and-model": "Brother MFC-8515DN for CUPS"}}
    result = driver_candidates({"make_model": "Brother MFC-8515DN for CUPS"}, build_driver_index(ppds), ppds)
    assert len(result) == 1 and result[0]["match"] == "exact"
    assert [item["label"] for item in search_drivers(ppds, "brother 8515")] == ["Brother MFC-8515DN for CUPS"]
