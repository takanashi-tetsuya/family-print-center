import json
import signal
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from print_center.cups_client import CupsError
from print_center.native_office import NativeOfficePrinter


@pytest.fixture
def printer(tmp_path, monkeypatch):
    cups = SimpleNamespace(host="127.0.0.1", port=631, jobs=Mock(return_value={"jobs": []}))
    printer = NativeOfficePrinter({"DATA_DIR": str(tmp_path), "CONVERSION_TIMEOUT": 10}, cups)
    bridge = tmp_path / "test-cups-options.so"
    bridge.write_bytes(b"mock bridge")
    monkeypatch.setattr(printer, "_prepare_shim", lambda: bridge)
    return printer


def test_native_print_has_private_settings_and_matches_exact_job_marker(printer, tmp_path, monkeypatch):
    source = tmp_path / "input.pptx"
    source.write_bytes(b"original office content")
    request_id = "request_unique_123456"
    options = {"copies": "2", "page-ranges": "1-2", "sides": "two-sided-long-edge", "media": "iso_a4_210x297mm"}
    requests = []

    def launch(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        requests.append(request)
        assert request["source"] == str(source.resolve())
        assert request["job_name"].startswith(f"FP-{request_id} ")
        settings_path = Path(kwargs["env"]["HOME"]) / ".cups" / "lpoptions"
        settings_text = settings_path.read_text()
        assert settings_text == "Default brother\n"
        content = Path(kwargs["env"]["FPC_CUPS_OPTIONS_FILE"]).read_bytes()
        assert content.startswith(b"FPCO1\n")
        assert b"two-sided-long-edge" in content and b"iso_a4_210x297mm" in content
        assert b"copies" not in content and b"page-ranges" not in content
        assert Path(kwargs["env"]["FPC_CUPS_OPTIONS_FILE"]).stat().st_mode & 0o777 == 0o600
        assert kwargs["env"]["FPC_CUPS_PRINTER"] == "brother"
        assert kwargs["env"]["FPC_CUPS_JOB_PREFIX"] == f"FP-{request_id} "
        assert "LD_PRELOAD" not in kwargs["env"]
        assert kwargs["env"]["CUPS_SERVER"] == "127.0.0.1:631"
        assert kwargs["env"]["SAL_USE_VCLPLUGIN"] == "svp"
        assert kwargs["start_new_session"] is True
        return SimpleNamespace(returncode=0, communicate=lambda timeout: (b'{"submitted":true}', b""))

    monkeypatch.setattr("print_center.native_office.subprocess.Popen", launch)
    printer.cups.jobs.return_value = {"jobs": [
        {"id": 100, "printer": "brother", "title": f"FP-{request_id}suffix other"},
        {"id": 99, "printer": "different", "title": f"FP-{request_id} document"},
        {"id": 42, "printer": "brother", "title": f"FP-{request_id} slides"},
    ]}
    assert printer.print_file("brother", source, "Slides", options, request_id) == 42
    assert len(requests) == 1
    assert source.read_bytes() == b"original office content"
    assert list(tmp_path.glob("office-print-*")) == []


def test_native_timeout_kills_entire_worker_group(printer, tmp_path, monkeypatch):
    process = Mock(pid=1234)
    process.communicate.side_effect = [subprocess.TimeoutExpired("office", 10), (b"", b"")]
    monkeypatch.setattr("print_center.native_office.subprocess.Popen", lambda *args, **kwargs: process)
    kill = Mock()
    monkeypatch.setattr("print_center.native_office.os.killpg", kill)
    with pytest.raises(CupsError, match="逾時"):
        printer.print_file("brother", tmp_path / "input.docx", "Doc", {"copies": "1"}, "request_unique_123456")
    kill.assert_called_once_with(1234, signal.SIGKILL)
    assert list(tmp_path.glob("office-print-*")) == []


def test_native_timeout_keeps_its_warning_when_the_worker_group_has_already_exited(printer, tmp_path, monkeypatch):
    process = Mock(pid=1234)
    process.communicate.side_effect = [subprocess.TimeoutExpired("office", 10), (b"", b"")]
    monkeypatch.setattr("print_center.native_office.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("print_center.native_office.os.killpg", Mock(side_effect=ProcessLookupError))
    with pytest.raises(CupsError, match="逾時"):
        printer.print_file("brother", tmp_path / "input.docx", "Doc", {"copies": "1"}, "request_unique_123456")


def test_unconfirmed_native_job_reports_ambiguous_submission(printer, tmp_path, monkeypatch):
    process = SimpleNamespace(returncode=0, communicate=lambda timeout: (b"{}", b""))
    monkeypatch.setattr("print_center.native_office.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("print_center.native_office.time.sleep", lambda _: None)
    with pytest.raises(CupsError, match="避免重複"):
        printer.print_file("brother", tmp_path / "input.docx", "Doc", {"copies": "1"}, "request_unique_123456")


def test_bridge_returns_the_actual_job_id_when_cups_job_titles_are_private(printer, tmp_path, monkeypatch):
    process = SimpleNamespace(returncode=0, communicate=lambda timeout: (b'{"submitted":true,"job_id":87}', b""))
    monkeypatch.setattr("print_center.native_office.subprocess.Popen", lambda *args, **kwargs: process)
    printer.cups.jobs.side_effect = CupsError("titles are private")
    assert printer.print_file("brother", tmp_path / "input.docx", "Doc", {"copies": "1"}, "request_unique_123456") == 87
    printer.cups.jobs.assert_not_called()


@pytest.mark.parametrize("name", ["bad printer", "../../printer", "printer\nnewsetting", "épson"])
def test_native_printer_name_cannot_inject_cups_settings(printer, tmp_path, name):
    with pytest.raises(CupsError):
        printer.print_file(name, tmp_path / "input.docx", "Doc", {"copies": "1"}, "request_unique_123456")
    assert list(tmp_path.glob("office-print-*")) == []


def test_private_binary_options_preserve_driver_values_and_omit_renderer_options(tmp_path):
    target = tmp_path / "options.bin"
    NativeOfficePrinter._write_options(target, {"copies": "2", "page-ranges": "1,3", "CustomDriver": "Choice With Spaces",
                                                "printer-resolution": "600x1200dpi"})
    data = target.read_bytes()
    records, cursor = {}, 6
    while cursor < len(data):
        key_length, value_length = struct.unpack(">HH", data[cursor:cursor + 4])
        cursor += 4
        key = data[cursor:cursor + key_length].decode()
        cursor += key_length
        records[key] = data[cursor:cursor + value_length].decode()
        cursor += value_length
    assert records == {"CustomDriver": "Choice With Spaces", "printer-resolution": "600x1200dpi"}


@pytest.mark.parametrize("options", [{"bad key": "on"}, {"DPI": "300\nmedia=A3"}, {"DPI": ""},
                                     {"DPI": True}, {"DPI": "A" * 2049}, {"DPI": "on\x00off"}])
def test_private_options_reject_malformed_driver_settings(tmp_path, options):
    with pytest.raises(CupsError):
        NativeOfficePrinter._write_options(tmp_path / "bad-options", options)


def test_private_options_allow_ui_fields_plus_driver_aliases_up_to_512_records(tmp_path):
    options = {f"DriverOption{index}": "Selected" for index in range(512)}
    NativeOfficePrinter._write_options(tmp_path / "options", options)
    options["DriverOption512"] = "Selected"
    with pytest.raises(CupsError, match="設定過多"):
        NativeOfficePrinter._write_options(tmp_path / "too-many-options", options)


def test_private_options_keep_the_total_byte_limit_after_increasing_record_count(tmp_path):
    options = {f"DriverOption{index}": "X" * 2048 for index in range(64)}
    with pytest.raises(CupsError, match="設定過多"):
        NativeOfficePrinter._write_options(tmp_path / "oversized-options", options)


def test_missing_compiler_is_reported_before_any_office_job_is_launched(tmp_path, monkeypatch):
    printer = NativeOfficePrinter({"DATA_DIR": str(tmp_path), "CONVERSION_TIMEOUT": 10}, SimpleNamespace())
    monkeypatch.setattr("print_center.native_office.shutil.which", lambda name: None)
    launch = Mock()
    monkeypatch.setattr("print_center.native_office.subprocess.Popen", launch)
    with pytest.raises(CupsError, match="直接列印功能還沒安裝好"):
        printer.print_file("brother", tmp_path / "source.docx", "document", {"copies": "1", "number-up": "2"}, "request")
    launch.assert_not_called()


def test_native_print_uses_the_same_uploaded_fonts_as_the_preview(printer, tmp_path, monkeypatch):
    seen = []
    printer.fonts = SimpleNamespace(environment=lambda: {"FONTCONFIG_FILE": "/data/fonts/fonts.conf"})

    def launch(command, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=lambda timeout: (b'{"submitted":true}', b""))

    monkeypatch.setattr("print_center.native_office.subprocess.Popen", launch)
    printer.cups.jobs.return_value = {"jobs": [{"id": 7, "printer": "brother", "title": "FP-request_unique_123456 Doc"}]}
    assert printer.print_file("brother", tmp_path / "input.docx", "Doc", {"copies": "1"}, "request_unique_123456") == 7
    assert seen[0]["FONTCONFIG_FILE"] == "/data/fonts/fonts.conf" and seen[0]["SAL_USE_VCLPLUGIN"] == "svp"
