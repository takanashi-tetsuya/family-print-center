"""Deployment-level security checks that span several modules."""
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from print_center.app import create_app
from print_center.conversion import PROFILE_XML, office_environment
from tests.test_api import FakeCups, FakeDiscovery

PUBLIC_ADMIN_ROUTES = {"/api/admin/session", "/api/admin/login", "/api/admin/logout"}


@pytest.fixture
def app(tmp_path):
    value = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "CONVERSION_SYNC": True, "MAX_UPLOAD_MB": 1},
                       cups_client=FakeCups(), discovery=FakeDiscovery())
    yield value
    value.extensions["uploads"].executor.shutdown(wait=True)


def admin_routes(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/admin") and rule.rule not in PUBLIC_ADMIN_ROUTES:
            path = re.sub(r"<(?:int:)?([a-z_]+)>", lambda match: "7" if "id" in match[1] else "brother", rule.rule)
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                yield method, path


def test_every_registered_admin_route_requires_login(app):
    # Built from the live URL map, so a new admin route cannot be added unprotected.
    client = app.test_client()
    token = client.get("/api/status").json["csrf_token"]
    routes = list(admin_routes(app))
    assert len(routes) >= 15
    for method, path in routes:
        result = client.open(path, method=method, json={}, headers={"X-CSRF-Token": token})
        assert result.status_code == 401, (method, path)


@pytest.mark.parametrize("path", ["/", "/admin", "/api/status", "/api/admin/session", "/missing", "/static/app.js"])
def test_security_headers_are_on_every_response(app, path):
    result = app.test_client().get(path)
    policy = result.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "object-src 'none'" in policy and "frame-ancestors 'self'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert result.headers["X-Content-Type-Options"] == "nosniff"
    assert result.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_public_host_header_is_refused_even_for_the_admin_login(app):
    client = app.test_client()
    result = client.post("/api/admin/login", json={"password": "x"}, base_url="http://evil.example.com")
    assert result.status_code == 403


def test_session_key_is_private_from_the_moment_it_is_created(tmp_path):
    value = create_app({"TESTING": True, "DATA_DIR": str(tmp_path)}, cups_client=FakeCups(), discovery=FakeDiscovery())
    value.extensions["uploads"].executor.shutdown(wait=True)
    mode = stat.S_IMODE((tmp_path / "session.key").stat().st_mode)
    assert mode == 0o600


def test_libreoffice_never_receives_admin_or_session_secrets(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "owner-secret")
    monkeypatch.setenv("SECRET_KEY", "signing-secret")
    monkeypatch.setenv("CUPS_PASSWORD_FILE", "/run/family-print-center/cups-password")
    environment = office_environment(HOME="/tmp/office")
    assert not {"ADMIN_PASSWORD", "SECRET_KEY", "CUPS_PASSWORD_FILE"} & set(environment)
    assert environment["HOME"] == "/tmp/office"


def test_office_profile_blocks_external_content_and_macros():
    for setting in ("DisableMacrosExecution", "DisableActiveContent", "BlockUntrustedRefererLinks"):
        assert re.search(rf'oor:name="{setting}"[^<]*><value>true</value>', PROFILE_XML), setting
    worker = (Path(__file__).resolve().parent.parent / "deploy" / "office_worker.py").read_text()
    assert "MacroExecMode.NEVER_EXECUTE" in worker and "UpdateDocMode.NO_UPDATE" in worker


OFFICE_AVAILABLE = bool(shutil.which("soffice") or shutil.which("libreoffice")) and subprocess.run(
    ["/usr/bin/python3", "-c", "import uno"], capture_output=True).returncode == 0 if os.path.exists("/usr/bin/python3") else False


def spreadsheet_with_links(path, secret, port):
    """An ODS whose formulas point at a local file and a local web address, with no cached values."""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?><office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" office:version="1.2"><office:body><office:spreadsheet>'
        '<table:table table:name="Sheet1">'
        f'<table:table-row><table:table-cell table:formula="of:=[\'{secret.as_uri()}\'#$Sheet1.A1]"/></table:table-row>'
        f'<table:table-row><table:table-cell table:formula="of:=COM.MICROSOFT.WEBSERVICE(&quot;http://127.0.0.1:{port}/x&quot;)"/></table:table-row>'
        '</table:table></office:spreadsheet></office:body></office:document-content>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("META-INF/manifest.xml", (
            '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
            'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>'))
        archive.writestr("content.xml", content)


@pytest.mark.skipif(not OFFICE_AVAILABLE, reason="LibreOffice with python3-uno is not installed")
def test_real_libreoffice_preview_cannot_read_local_files_or_reach_the_network(tmp_path):
    import http.server
    import threading
    import pymupdf
    from print_center.conversion import UploadStore

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"NETWORK-REACHED")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        secret = tmp_path / "secret.txt"
        secret.write_text("LOCAL-SECRET-VALUE\n")
        directory = tmp_path / "upload"
        directory.mkdir()
        source = directory / "input.ods"
        spreadsheet_with_links(source, secret, server.server_address[1])
        store = UploadStore.__new__(UploadStore)
        store.config, store.fonts = {"CONVERSION_TIMEOUT": 120}, None
        store._office_preview(source, directory / "document.pdf", directory)
        with pymupdf.open(directory / "document.pdf") as document:
            text = " ".join(page.get_text() for page in document)
    finally:
        server.shutdown()
    # The sheet itself rendered: the two cells show LibreOffice's "external content disabled" errors.
    assert "Sheet1" in text and "Err:" in text
    assert "LOCAL-SECRET-VALUE" not in text and "NETWORK-REACHED" not in text
    assert hits == []
