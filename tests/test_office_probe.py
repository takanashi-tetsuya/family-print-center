import json
import sys
import zipfile
from types import SimpleNamespace

import pymupdf as fitz
import pytest

from deploy import doctor, healthcheck
from print_center import office_probe


def render_probe(source, target, directory, timeout):
    with zipfile.ZipFile(source) as archive:
        assert archive.namelist()[0] == "mimetype"
        assert archive.testzip() is None
        assert office_probe.MARKER_TEXT in archive.read("content.xml").decode()
    assert directory == source.parent == target.parent
    assert timeout == 30
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), office_probe.MARKER_TEXT)
        document.save(target)


def test_probe_uses_real_preview_interface_with_private_generated_documents(monkeypatch):
    calls = []
    def render(*args, **kwargs):
        calls.append(args)
        render_probe(*args, **kwargs)
    monkeypatch.setattr(office_probe, "preview_office", render)
    results = office_probe.check_office()
    assert [item["component"] for item in results] == ["Writer", "Calc", "Impress"]
    assert all(item["ok"] and item["pages"] == 1 for item in results)
    assert {args[0].suffix for args in calls} == {".odt", ".ods", ".odp"}
    assert all(not args[0].parent.exists() for args in calls)


@pytest.mark.parametrize("mode", ["exception", "empty_pdf", "wrong_content"])
def test_installed_office_does_not_pass_without_valid_rendered_content(monkeypatch, mode):
    def render(source, target, directory, timeout):
        if mode == "exception":
            raise RuntimeError("private document contents and credentials")
        if mode == "empty_pdf":
            target.write_bytes(b"%PDF-1.4\n")
            return
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), "An unrelated blank result")
            document.save(target)
    monkeypatch.setattr(office_probe, "preview_office", render)
    results = office_probe.check_office()
    assert len(results) == 3
    assert all(item["ok"] is False for item in results)
    assert "private document" not in json.dumps(results)


def test_success_marker_is_private_and_failed_probe_removes_stale_marker(tmp_path, monkeypatch):
    marker = tmp_path / "ready.json"
    monkeypatch.setattr(office_probe, "preview_office", render_probe)
    assert office_probe.main(["--marker", str(marker)]) == 0
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_text())["print_jobs_submitted"] == 0
    def fail(*args, **kwargs):
        raise RuntimeError("Preview cannot start")
    monkeypatch.setattr(office_probe, "preview_office", fail)
    assert office_probe.main(["--marker", str(marker)]) == 1
    assert not marker.exists()


def ready_record():
    return {"ok": True, "print_jobs_submitted": 0,
            "components": [{"component": name, "ok": True, "pages": 1}
                           for name in ("Writer", "Calc", "Impress")]}


@pytest.mark.parametrize("data", [None, "bad JSON", {}, {"ok": True},
                                  {**ready_record(), "components": []},
                                  {**ready_record(), "print_jobs_submitted": 1},
                                  {**ready_record(), "components": [None, None, None]},
                                  {**ready_record(), "components": [dict(item, ok=False)
                                   for item in ready_record()["components"]]}])
def test_health_requires_successful_startup_preview_before_network_checks(tmp_path, monkeypatch, data):
    marker = tmp_path / "ready.json"
    if data is not None:
        marker.write_text(data if isinstance(data, str) else json.dumps(data))
    monkeypatch.setattr(healthcheck, "Path", lambda _path: marker)
    connection = []
    monkeypatch.setattr(healthcheck.socket, "create_connection", lambda *a, **k: connection.append(True))
    with pytest.raises(RuntimeError, match="Office startup preview"):
        healthcheck.main()
    assert not connection


def test_health_accepts_all_three_successful_preview_results(tmp_path, monkeypatch):
    marker = tmp_path / "ready.json"
    marker.write_text(json.dumps(ready_record()))
    monkeypatch.setattr(healthcheck, "Path", lambda _path: marker)
    healthcheck.office_ready()


@pytest.fixture
def doctor_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CUPS_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    connection = SimpleNamespace(getPrinters=lambda: {"printer": {"device-uri": "ipps://user:TESTTOKEN@printer/ipp"}})
    monkeypatch.setitem(sys.modules, "cups", SimpleNamespace(setUser=lambda user: None,
                        Connection=lambda **kwargs: connection))


def test_doctor_detects_runtime_failure_even_when_binaries_and_uno_exist(doctor_environment, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "office_smoke", lambda: [{"component": "Writer", "ok": False}])
    assert doctor.main([]) == 1
    output = capsys.readouterr().out
    assert "Writer preview: FAILED" in output
    assert "TESTTOKEN" not in output


def test_quick_doctor_explicitly_skips_real_preview(doctor_environment, monkeypatch, capsys):
    def forbidden():
        pytest.fail("Quick diagnostics should not start LibreOffice")
    monkeypatch.setattr(doctor, "office_smoke", forbidden)
    assert doctor.main(["--quick"]) == 0
    assert "SKIPPED (--quick)" in capsys.readouterr().out


def test_diagnostics_redact_credentials_from_error_messages():
    assert doctor.safe_message("Failed: ipps://user:TESTTOKEN@printer:631/ipp") == "Failed: ipps://printer:631/ipp"
