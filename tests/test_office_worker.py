import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from deploy import office_worker


@pytest.fixture
def uno_environment(monkeypatch):
    document = Mock()
    document.getPrinter.return_value = [SimpleNamespace(Name="Name", Value="brother")]
    desktop = Mock()
    desktop.loadComponentFromURL.return_value = document
    remote = Mock()
    remote.ServiceManager.createInstanceWithContext.return_value = desktop
    resolver = Mock()
    resolver.resolve.return_value = remote
    context = Mock()
    context.ServiceManager.createInstanceWithContext.return_value = resolver
    uno = ModuleType("uno")
    uno.getComponentContext = lambda: context
    uno.getConstantByName = lambda name: name
    uno.Any = lambda type_name, value: (type_name, value)
    beans = ModuleType("com.sun.star.beans")
    beans.PropertyValue = SimpleNamespace
    monkeypatch.setitem(sys.modules, "uno", uno)
    monkeypatch.setitem(sys.modules, "com.sun.star.beans", beans)
    process = Mock()
    process.poll.return_value = None
    process.wait.return_value = 0
    monkeypatch.setattr(office_worker.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(office_worker.shutil, "which", lambda _: "/usr/bin/libreoffice")
    return document, desktop, process


@pytest.mark.parametrize("extension", ["pptx", "docx", "xlsx", "docm", "xlsm", "pptm", "ppsx", "csv", "tsv", "txt"])
def test_office_uses_safe_native_print_api_and_no_export(tmp_path, uno_environment, extension):
    document, desktop, process = uno_environment
    source = tmp_path / f"input.{extension}"
    source.write_text("native original")
    request = {"source": str(source), "profile": str(tmp_path / "profile"), "printer": "brother",
               "job_name": "FP-unique_request_123 Family document",
               "options": {"copies": "2", "page-ranges": "1-3,5"}}
    assert office_worker.run(request) == {"submitted": True}
    load_options = {prop.Name: prop.Value for prop in desktop.loadComponentFromURL.call_args.args[3]}
    assert load_options["Hidden"] is True and load_options["ReadOnly"] is True
    assert load_options["MacroExecutionMode"].endswith("NEVER_EXECUTE")
    assert load_options["UpdateDocMode"].endswith("NO_UPDATE")
    descriptor = {prop.Name: prop.Value for prop in document.setPrinter.call_args.args[0]}
    assert descriptor["Name"] == "brother"
    print_options = {prop.Name: prop.Value for prop in document.print.call_args.args[0]}
    assert print_options["JobName"] == request["job_name"]
    assert print_options["CopyCount"] == ("short", 2)
    assert print_options["Pages"] == "1-3;5"
    assert print_options["Wait"] is True and print_options["SinglePrintJobs"] is False
    document.storeToURL.assert_not_called()
    document.storeAsURL.assert_not_called()
    document.close.assert_called_once_with(True)
    desktop.terminate.assert_called_once()


def test_unopenable_office_document_cannot_be_printed(tmp_path, uno_environment):
    document, desktop, process = uno_environment
    desktop.loadComponentFromURL.return_value = None
    with pytest.raises(RuntimeError, match="cannot be opened"):
        office_worker.run({"source": str(tmp_path / "bad.docx"), "profile": str(tmp_path / "profile"),
                           "printer": "brother", "options": {"copies": "1"}})
    document.print.assert_not_called()
    desktop.terminate.assert_called_once()


def test_fallback_to_different_printer_is_refused(tmp_path, uno_environment):
    document, desktop, process = uno_environment
    document.getPrinter.return_value = [SimpleNamespace(Name="Name", Value="another-printer")]
    with pytest.raises(RuntimeError):
        office_worker.run({"source": str(tmp_path / "document.docx"), "profile": str(tmp_path / "profile"),
                           "printer": "brother", "options": {"copies": "1"}})
    document.print.assert_not_called()


@pytest.mark.parametrize("is_valid", [None, True, False])
def test_nonstandard_printer_validity_is_optional_but_explicit_false_refuses_submission(tmp_path, uno_environment, is_valid):
    document, _, _ = uno_environment
    descriptors = [SimpleNamespace(Name="Name", Value="brother")]
    if is_valid is not None:
        descriptors.append(SimpleNamespace(Name="IsValid", Value=is_valid))
    document.getPrinter.return_value = descriptors
    request = {"source": str(tmp_path / "input.docx"), "profile": str(tmp_path / "profile"),
               "printer": "brother", "job_name": "FP-request original", "options": {"copies": "1"}}
    if is_valid is False:
        with pytest.raises(RuntimeError, match="invalid"):
            office_worker.run(request)
        document.print.assert_not_called()
    else:
        assert office_worker.run(request) == {"submitted": True}
        document.print.assert_called_once()


@pytest.mark.parametrize("mode", ["preview", "print"])
def test_direct_worker_uses_svp_without_display_even_if_parent_requests_gen(tmp_path, uno_environment, monkeypatch, mode):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("SAL_USE_VCLPLUGIN", "gen")
    monkeypatch.setenv("LD_PRELOAD", "/stale-job/bridge.so")
    monkeypatch.setenv("ADMIN_PASSWORD", "not-for-office")
    request = {"mode": mode, "source": str(tmp_path / "input.docx"), "profile": str(tmp_path / "profile"),
               "target": str(tmp_path / "preview.pdf"), "printer": "brother", "job_name": "FP-request original",
               "options": {"copies": "1"}}
    office_worker.run(request)
    environment = office_worker.subprocess.Popen.call_args.kwargs["env"]
    command = office_worker.subprocess.Popen.call_args.args[0]
    assert "--headless" in command and environment["SAL_USE_VCLPLUGIN"] == "svp"
    assert "DISPLAY" not in environment and "LD_PRELOAD" not in environment
    assert "ADMIN_PASSWORD" not in environment


@pytest.mark.parametrize("service,filter_name", [
    ("com.sun.star.sheet.SpreadsheetDocument", "calc_pdf_Export"),
    ("com.sun.star.presentation.PresentationDocument", "impress_pdf_Export"),
    ("com.sun.star.text.TextDocument", "writer_pdf_Export"),
])
def test_preview_exports_pdf_with_safe_load_options_and_does_not_print(tmp_path, uno_environment, service, filter_name):
    document, desktop, process = uno_environment
    document.supportsService.side_effect = lambda name: name == service
    target = tmp_path / "preview.pdf"
    result = office_worker.run({"mode": "preview", "source": str(tmp_path / "input.docx"),
                               "profile": str(tmp_path / "profile"), "target": str(target)})
    assert result == {"preview": True}
    url, descriptor = document.storeToURL.call_args.args
    assert url == target.resolve().as_uri()
    export_options = {prop.Name: prop.Value for prop in descriptor}
    assert export_options["FilterName"] == filter_name and export_options["Overwrite"] is True
    load_options = {prop.Name: prop.Value for prop in desktop.loadComponentFromURL.call_args.args[3]}
    assert load_options["MacroExecutionMode"].endswith("NEVER_EXECUTE")
    assert load_options["UpdateDocMode"].endswith("NO_UPDATE")
    document.print.assert_not_called()
    document.setPrinter.assert_not_called()


def test_native_bridge_is_loaded_only_by_libreoffice_and_requires_a_submission_receipt(tmp_path, uno_environment, monkeypatch):
    document, _, _ = uno_environment
    bridge = tmp_path / "bridge.so"
    bridge.write_bytes(b"mock")
    receipt = tmp_path / "receipt.txt"
    monkeypatch.setenv("FPC_CUPS_SHIM", str(bridge))
    document.print.side_effect = lambda _: receipt.write_text("37\n")
    result = office_worker.run({"source": str(tmp_path / "input.docx"), "profile": str(tmp_path / "profile"),
                               "printer": "brother", "job_name": "FP-request document", "cups_receipt": str(receipt),
                               "options": {"copies": "2", "printer-resolution": "600dpi", "media": "iso_a5_148x210mm"}})
    assert result == {"submitted": True, "job_id": 37}
    environment = office_worker.subprocess.Popen.call_args.kwargs["env"]
    assert environment["LD_PRELOAD"] == str(bridge)
    assert "FPC_CUPS_SHIM" not in environment
    # Device/filter choices go to the CUPS bridge once, preserving original page layout.
    descriptor = {prop.Name: prop.Value for prop in document.setPrinter.call_args.args[0]}
    assert descriptor == {"Name": "brother"}
    print_options = {prop.Name: prop.Value for prop in document.print.call_args.args[0]}
    assert "printer-resolution" not in print_options and "media" not in print_options
    document.storeToURL.assert_not_called()


@pytest.mark.parametrize("receipt_content", [None, "0", "not-a-job"])
def test_missing_or_failed_cups_bridge_receipt_is_not_reported_as_success(tmp_path, uno_environment, monkeypatch, receipt_content):
    bridge = tmp_path / "bridge.so"
    bridge.write_bytes(b"mock")
    receipt = tmp_path / "receipt.txt"
    if receipt_content is not None:
        receipt.write_text(receipt_content)
    monkeypatch.setenv("FPC_CUPS_SHIM", str(bridge))
    with pytest.raises(RuntimeError, match="not submitted"):
        office_worker.run({"source": str(tmp_path / "input.docx"), "profile": str(tmp_path / "profile"),
                           "printer": "brother", "job_name": "FP-request document", "cups_receipt": str(receipt),
                           "options": {"copies": "1"}})


def test_unavailable_native_bridge_refuses_to_start_libreoffice(tmp_path, uno_environment, monkeypatch):
    monkeypatch.delenv("FPC_CUPS_SHIM", raising=False)
    with pytest.raises(RuntimeError, match="bridge is unavailable"):
        office_worker.run({"source": str(tmp_path / "input.docx"), "profile": str(tmp_path / "profile"),
                           "printer": "brother", "job_name": "FP-request document", "cups_receipt": str(tmp_path / "receipt"),
                           "options": {"copies": "1"}})
    office_worker.subprocess.Popen.assert_not_called()


def test_document_cleanup_failure_does_not_leave_libreoffice_running_or_lose_a_submitted_job(tmp_path, uno_environment):
    document, desktop, process = uno_environment
    document.close.side_effect = RuntimeError("cannot close document")
    document.dispose.side_effect = RuntimeError("cannot dispose document")
    assert office_worker.run({"source": str(tmp_path / "input.docx"), "profile": str(tmp_path / "profile"),
                              "printer": "brother", "job_name": "FP-request original", "options": {"copies": "1"}}) == {"submitted": True}
    desktop.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)


def test_process_cleanup_tolerates_exit_between_timeout_and_termination(tmp_path, uno_environment):
    import subprocess
    _, desktop, process = uno_environment
    process.wait.side_effect = [subprocess.TimeoutExpired("office", 5), 0]
    process.terminate.side_effect = ProcessLookupError
    assert office_worker.run({"source": str(tmp_path / "input.docx"), "profile": str(tmp_path / "profile"),
                              "printer": "brother", "job_name": "FP-request original", "options": {"copies": "1"}}) == {"submitted": True}
    desktop.terminate.assert_called_once()
    assert process.wait.call_count == 2


def preview_with_toolkit(tmp_path, uno_environment, toolkit):
    document, desktop, process = uno_environment
    services = {"com.sun.star.frame.Desktop": desktop, "com.sun.star.awt.Toolkit": toolkit}
    context = sys.modules["uno"].getComponentContext()
    resolver = context.ServiceManager.createInstanceWithContext.return_value
    resolver.resolve.return_value.ServiceManager.createInstanceWithContext.side_effect = lambda name, _: services[name]
    document.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextDocument"
    source = tmp_path / "input.docx"
    source.write_text("original")
    request = {"mode": "preview", "source": str(source), "profile": str(tmp_path / "profile"),
               "target": str(tmp_path / "preview.pdf"), "fonts": str(tmp_path / "fonts.json")}
    return office_worker.run(request), tmp_path / "fonts.json"


def test_preview_reports_requested_and_drawn_font_names(tmp_path, uno_environment):
    document, desktop, process = uno_environment
    toolkit = Mock()
    order = []
    toolkit.startTrackingFontMappingUse.side_effect = lambda: order.append("track")
    desktop.loadComponentFromURL.side_effect = lambda *args: order.append("load") or document
    toolkit.finishTrackingFontMappingUse.return_value = [
        SimpleNamespace(originalFont="標楷體", usedFonts=("TW-Kai/Regular",), count=3)]
    result, report = preview_with_toolkit(tmp_path, uno_environment, toolkit)
    assert result == {"preview": True} and order == ["track", "load"]
    assert report.read_text(encoding="utf-8") == '[["標楷體", ["TW-Kai/Regular"]]]'


def test_font_tracking_failure_leaves_the_preview_intact(tmp_path, uno_environment):
    toolkit = Mock()
    toolkit.finishTrackingFontMappingUse.side_effect = RuntimeError("older LibreOffice")
    result, report = preview_with_toolkit(tmp_path, uno_environment, toolkit)
    assert result == {"preview": True} and not report.exists()
    toolkit.startTrackingFontMappingUse.side_effect = RuntimeError("no toolkit")
    toolkit.finishTrackingFontMappingUse.reset_mock()
    result, report = preview_with_toolkit(tmp_path, uno_environment, toolkit)
    assert result == {"preview": True} and not report.exists()
    toolkit.finishTrackingFontMappingUse.assert_not_called()


def test_printing_never_tracks_fonts(tmp_path, uno_environment):
    source = tmp_path / "input.docx"
    source.write_text("original")
    office_worker.run({"source": str(source), "profile": str(tmp_path / "profile"), "printer": "brother",
                       "job_name": "FP-unique_request_123 doc", "fonts": str(tmp_path / "fonts.json"), "options": {"copies": "1"}})
    resolver = sys.modules["uno"].getComponentContext().ServiceManager.createInstanceWithContext.return_value
    created = [call.args[0] for call in resolver.resolve.return_value.ServiceManager.createInstanceWithContext.call_args_list]
    assert created == ["com.sun.star.frame.Desktop"] and not (tmp_path / "fonts.json").exists()
