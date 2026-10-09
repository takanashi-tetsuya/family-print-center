from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from print_center.cups_client import CupsClient, CupsError


def adapter(native):
    client = CupsClient.__new__(CupsClient)
    client.user = "familyprint"
    client.host = "127.0.0.1"
    client.port = 631
    client.cups = SimpleNamespace(Connection=Mock(return_value=native), setUser=Mock(), setPasswordCB=Mock())
    return client


def test_missing_pycups_reports_clear_service_status():
    client = CupsClient.__new__(CupsClient)
    client.cups = None
    assert client.status()["available"] is False
    assert "pycups" in client.status()["error"]


def test_connection_errors_do_not_disclose_internal_details():
    client = adapter(Mock())
    client.cups.Connection.side_effect = RuntimeError("password=secret /private/path")
    result = client.status()
    assert result["available"] is False
    assert "secret" not in result["error"]
    assert "/private" not in result["error"]


def test_each_operation_uses_a_fresh_connection():
    native = Mock()
    native.printFile.return_value = 42
    native.getJobs.return_value = {}
    client = adapter(native)
    assert client.print_file("brother", Path("/tmp/ready.pdf"), "Home work", {"copies": "1"}) == 42
    client.jobs()
    assert client.cups.Connection.call_count == 2
    native.printFile.assert_called_once_with("brother", "/tmp/ready.pdf", "Home work", {"copies": "1"})
    assert native.getJobs.call_args.kwargs["my_jobs"] is True
    requested = native.getJobs.call_args.kwargs["requested_attributes"]
    assert {"job-id", "job-name", "job-printer-uri", "job-state", "job-originating-user-name"} <= set(requested)


def test_cancelling_other_users_job_never_calls_cups_cancel():
    native = Mock()
    native.getJobAttributes.return_value = {"job-originating-user-name": "another-user", "job-state": 3}
    with pytest.raises(CupsError, match="只能取消"):
        adapter(native).cancel_job(7)
    native.cancelJob.assert_not_called()


@pytest.mark.parametrize("state", [7, 8, 9])
def test_finished_job_cannot_be_cancelled(state):
    native = Mock()
    native.getJobAttributes.return_value = {"job-originating-user-name": "familyprint", "job-state": state}
    with pytest.raises(CupsError, match="已結束"):
        adapter(native).cancel_job(7)
    native.cancelJob.assert_not_called()


def test_active_owned_job_can_be_cancelled():
    native = Mock()
    native.getJobAttributes.return_value = {"job-originating-user-name": "familyprint", "job-state": 5}
    adapter(native).cancel_job(7)
    native.cancelJob.assert_called_once_with(7)


@pytest.mark.parametrize("method, states, value", [("hold_job", {3, 5, 6}, "indefinite"), ("release_job", {4}, "no-hold")])
def test_pause_and_resume_only_touch_owned_jobs_in_the_right_state(method, states, value):
    for state in range(3, 10):
        native = Mock()
        native.getJobAttributes.return_value = {"job-originating-user-name": "familyprint", "job-state": state}
        if state in states:
            getattr(adapter(native), method)(7)
            native.setJobHoldUntil.assert_called_once_with(7, value)
        else:
            with pytest.raises(CupsError):
                getattr(adapter(native), method)(7)
            native.setJobHoldUntil.assert_not_called()
    native.getJobAttributes.return_value = {"job-originating-user-name": "another-user", "job-state": min(states)}
    with pytest.raises(CupsError, match="只能"):
        getattr(adapter(native), method)(7)
    native.setJobHoldUntil.assert_not_called()


def job_list(jobs, printers=None):
    native = Mock()
    native.getJobs.return_value = jobs
    native.getPrinters.return_value = printers or {}
    client = adapter(native)
    client.profiles = None
    return {job["id"]: job for job in client.jobs()["jobs"]}, native


def test_jobs_show_the_printers_own_words_and_translate_only_known_keywords():
    jobs, _ = job_list({
        10: {"job-state": 5, "job-printer-uri": "ipp://localhost/printers/brother", "job-state-reasons": "job-printing"},
        11: {"job-state": 3, "job-printer-uri": "ipp://localhost/printers/brother", "job-state-reasons": ["job-queued"]},
        12: {"job-state": 8, "job-printer-uri": "ipp://localhost/printers/brother", "job-state-reasons": "aborted-by-system",
             "job-printer-state-message": "Filter\x1b[0m failed\n", "job-printer-state-reasons": ["none"]},
        13: {"job-state": 9, "job-state-reasons": "job-completed-successfully", "job-printer-state-message": "Ready"},
        14: {"job-state": 7, "job-state-reasons": "job-canceled-by-user", "job-printer-state-message": "Printing page 2"},
    }, {"brother": {"printer-state": 4, "printer-state-message": "Media jam in tray 2 (ipp://admin:secret@10.0.0.9/)",
                    "printer-state-reasons": ["media-jam-error", "com.brother-wait-cooling-report",
                                              "cups-waiting-for-job-completed"]}})
    assert jobs[10]["reasons"] == ["卡紙", "com.brother-wait-cooling-report"]
    assert jobs[10]["message"] == "Media jam in tray 2 (ipp://10.0.0.9/)"
    assert jobs[10]["attention"] is True and jobs[11]["attention"] is False
    assert not any(job["printer_stopped"] for job in jobs.values())
    assert (jobs[10]["can_hold"], jobs[10]["can_release"], jobs[10]["can_cancel"]) == (True, False, True)
    # Waiting behind a working printer: the printer's words belong to the job that is printing.
    assert jobs[11]["reasons"] == [] and jobs[11]["message"] == ""
    assert jobs[12]["reasons"] == ["打印服務中止了這份任務"] and jobs[12]["message"] == "Filter [0m failed"
    assert jobs[12]["can_cancel"] is False
    assert jobs[13]["message"] == jobs[14]["message"] == "" and jobs[13]["reasons"] == jobs[14]["reasons"] == []
    assert jobs[12]["attention"] is True and jobs[13]["attention"] is jobs[14]["attention"] is False


@pytest.mark.parametrize("reasons, attention", [
    (["none"], False), (["media-low-report", "toner-low-warning"], False), (["connecting-to-device"], False),
    (["cups-waiting-for-job-completed"], False), (["media-empty-error"], True), (["door-open"], True),
])
def test_only_printer_errors_ask_for_attention(reasons, attention):
    jobs, _ = job_list({1: {"job-state": 5, "job-printer-uri": "ipp://localhost/printers/brother", "time-at-processing": 9}},
                       {"brother": {"printer-state": 4, "printer-state-reasons": reasons}})
    assert jobs[1]["attention"] is attention and jobs[1]["started"] is True


@pytest.mark.parametrize("reason,label,attention", [
    ("other-warning", "打印機有警告", False), ("other-report", "打印機狀態訊息", False),
    ("other", "打印機回報錯誤", True), ("other-error", "打印機回報錯誤", True),
])
def test_other_reason_keeps_its_severity_in_printer_and_job_text(reason, label, attention):
    reasons = ["cups-waiting-for-job-completed", reason]
    jobs, _ = job_list({1: {"job-state": 5, "job-printer-uri": "ipp://localhost/printers/test"}},
                       {"test": {"printer-state": 4, "printer-state-reasons": reasons}})
    assert jobs[1]["reasons"] == [label]
    assert jobs[1]["attention"] is attention
    client, _ = printer_adapter({"printer-state-reasons": reasons})
    assert client.printers()["printers"][0]["reasons"] == [label]


def test_jobs_waiting_on_a_stopped_printer_say_so_and_paused_jobs_can_resume():
    jobs, native = job_list({
        20: {"job-state": 3, "job-printer-uri": "ipp://localhost/printers/brother", "job-state-reasons": "printer-stopped"},
        21: {"job-state": 4, "job-printer-uri": "ipp://localhost/printers/brother", "job-state-reasons": "job-hold-until-specified"},
    }, {"brother": {"printer-state": 5, "printer-state-message": "/usr/lib/cups/filter/x failed",
                    "printer-state-reasons": ["paused", "other"]}})
    assert jobs[20]["reasons"] == ["打印機已暫停", "打印機回報錯誤"]
    assert jobs[20]["message"] == "/usr/lib/cups/filter/x failed"
    assert jobs[20]["attention"] is True and jobs[20]["started"] is False
    assert jobs[20]["printer_stopped"] is jobs[21]["printer_stopped"] is True
    assert jobs[21]["state_label"] == "已暫停"
    assert (jobs[21]["can_hold"], jobs[21]["can_release"]) == (False, True)
    native.getPrinters.assert_called_once()


def test_unfinished_jobs_stay_listed_and_finished_lists_skip_printer_lookups():
    finished = {job_id: {"job-state": 9} for job_id in range(100, 160)}
    jobs, native = job_list({**finished, 1: {"job-state": 4}})
    assert 1 in jobs and len(jobs) == 51
    jobs, native = job_list(finished)
    native.getPrinters.assert_not_called()


def test_rejecting_printer_status_comes_from_full_ipp_attributes():
    native = Mock()
    native.getDefault.return_value = "brother"
    native.getPrinters.return_value = {"brother": {"printer-info": "Brother", "printer-state": 3}}
    native.getPrinterAttributes.return_value = {
        "printer-is-accepting-jobs": False,
        "sides-supported": ["one-sided"],
        "print-color-mode-supported": ["monochrome"],
        "media-supported": ["iso_a4_210x297mm"],
    }
    assert adapter(native).printers()["printers"][0]["accepting"] is False


def printer_adapter(attrs, ppd=None, path=None):
    native = Mock()
    native.getDefault.return_value = "test"
    native.getPrinters.return_value = {"test": {"printer-info": "Test printer", "printer-state": 3}}
    native.getPrinterAttributes.return_value = attrs
    client = adapter(native)
    if ppd is not None:
        native.getPPD.return_value = str(path)
        client.cups.PPD = Mock(return_value=ppd)
    return client, native


def test_ipp_job_controls_report_supported_choices_without_forcing_defaults():
    client, _ = printer_adapter({
        "printer-resolution-supported": [(300, 300, 3), (600, 1200, 3), (118, 118, 4)],
        "printer-resolution-default": (300, 300, 3), "print-quality-supported": [4, 5],
        "media-type-supported": ["stationery", "photographic-glossy"],
        "media-source-supported": ["auto", "main"], "output-bin-supported": ["face-down"],
        "finishings-supported": [3], "number-up-supported": [1, 2, 4],
    })
    controls = {option["key"]: option for option in client.printers()["printers"][0]["capabilities"]["print_options"]}
    assert [choice["value"] for choice in controls["printer-resolution"]["choices"]] == ["300dpi", "600x1200dpi", "118dpcm"]
    assert controls["printer-resolution"]["default"] == "300dpi"
    assert [choice["value"] for choice in controls["print-quality"]["choices"]] == ["4", "5"]
    assert controls["print-quality"]["choices"][1]["label"] == "最佳（較慢）"
    assert [choice["value"] for choice in controls["number-up"]["choices"]] == ["1", "2", "4"]
    assert controls["media-type"]["group"] == "paper"
    assert controls["number-up"]["default"] == ""
    assert {"page-set", "outputorder", "multiple-document-handling", "number-up-layout", "print-scaling"} <= set(controls)


def test_resolution_single_triple_and_duplicate_equal_axes_are_normalized():
    client, _ = printer_adapter({"printer-resolution-supported": (600, 600, 3)})
    control = next(option for option in client.printers()["printers"][0]["capabilities"]["print_options"] if option["key"] == "printer-resolution")
    assert control["choices"] == [{"value": "600dpi", "label": "600 DPI"}]
    client, _ = printer_adapter({"printer-resolution-supported": ["300x300dpi", "300dpi", (0, 0, 3), (True, 300, 3), (600, 600, 8)]})
    control = next(option for option in client.printers()["printers"][0]["capabilities"]["print_options"] if option["key"] == "printer-resolution")
    assert [choice["value"] for choice in control["choices"]] == ["300dpi"]


def ppd_option(key, values, default=None):
    return SimpleNamespace(keyword=key, text=key, defchoice=default or values[0],
                           choices=[{"choice": value, "text": f"Choice {value}"} for value in values])


def ppd_group(name, options=(), subgroups=()):
    return SimpleNamespace(name=name, text=name, options=list(options), subgroups=list(subgroups))


def test_all_nested_driver_job_choices_are_exposed_and_ppd_is_removed(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("PPD fixture")
    ppd = SimpleNamespace(optionGroups=[ppd_group("General", [
        ppd_option("PageSize", ["A4", "Letter"]), ppd_option("Duplex", ["None", "DuplexNoTumble"]),
        ppd_option("ColorModel", ["Gray"]), ppd_option("Resolution", ["300dpi", "600dpi", "HQ1200"]),
    ], [ppd_group("Vendor", [ppd_option("BrMediaType", ["PLAIN", "THICK"]), ppd_option("TonerSaveMode", ["OFF", "ON"])])])], constraints=[])
    client, native = printer_adapter({"media-supported": ["iso_a4_210x297mm"], "sides-supported": ["one-sided"],
                                     "print-color-mode-supported": ["monochrome"], "printer-resolution-supported": [(300, 300, 3), (600, 600, 3)]}, ppd, path)
    caps = client.printers()["printers"][0]["capabilities"]
    controls = {option["key"]: option for option in caps["print_options"]}
    assert {"Resolution", "BrMediaType", "TonerSaveMode", "printer-resolution"} <= set(controls)
    assert not {"PageSize", "Duplex", "ColorModel"} & set(controls)
    assert [choice["value"] for choice in controls["Resolution"]["choices"]] == ["HQ1200"]
    assert controls["BrMediaType"]["default"] == "PLAIN"
    assert controls["TonerSaveMode"]["group"] == "quality"
    assert caps["option_aliases"]["printer-resolution"] == {
        "300dpi": {"Resolution": "300dpi"}, "600dpi": {"Resolution": "600dpi"}}
    assert not path.exists()
    native.getPPD.assert_called_once_with("test")
    native.addPrinter.assert_not_called()


def test_driver_controls_identical_to_ipp_are_not_duplicated(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("fixture")
    ppd = SimpleNamespace(optionGroups=[ppd_group("General", [ppd_option("Resolution", ["300dpi"])])], constraints=[])
    client, _ = printer_adapter({"printer-resolution-supported": [(300, 300, 3)]}, ppd, path)
    keys = [option["key"] for option in client.printers()["printers"][0]["capabilities"]["print_options"]]
    assert "printer-resolution" in keys and "Resolution" not in keys


def test_installable_admin_and_unsafe_driver_options_are_excluded(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("fixture")
    ppd = SimpleNamespace(optionGroups=[ppd_group("InstallableOptions", [ppd_option("OptionTrays", ["1Trays", "2Trays"])]),
            ppd_group("General", [ppd_option("Sleep", ["30", "60"]), ppd_option("printer-is-accepting-jobs", ["false"]),
                ppd_option("Bad Key", ["normal"]), ppd_option("VendorMode", ["normal", "bad value", "bad\nvalue", "other=setting", "quote'", "\\backslash"]),
                ppd_option("TonerSaveMode", ["OFF", "ON"])])], constraints=[])
    client, _ = printer_adapter({}, ppd, path)
    controls = {option["key"]: option for option in client.printers()["printers"][0]["capabilities"]["print_options"]}
    assert not {"OptionTrays", "Sleep", "printer-is-accepting-jobs", "Bad Key"} & set(controls)
    assert controls["VendorMode"]["choices"] == [{"value": "normal", "label": "Choice normal"}]
    assert "TonerSaveMode" in controls


def test_uninstalled_tray_is_excluded_from_driver_and_ipp_choices(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("fixture")
    constraint = SimpleNamespace(option1="OptionTrays", choice1="1Trays", option2="InputSlot", choice2="Tray2")
    ppd = SimpleNamespace(optionGroups=[ppd_group("InstallableOptions", [ppd_option("OptionTrays", ["1Trays", "2Trays"], "1Trays")]),
                                        ppd_group("General", [ppd_option("InputSlot", ["TRAY1", "TRAY2", "MPTRAY"])])], constraints=[constraint])
    client, _ = printer_adapter({"media-source-supported": ["auto", "tray-1", "tray-2", "by-pass-tray"]}, ppd, path)
    controls = {option["key"]: option for option in client.printers()["printers"][0]["capabilities"]["print_options"]}
    assert "InputSlot" not in controls
    assert "tray-2" not in {choice["value"] for choice in controls["media-source"]["choices"]}


def test_ppd_failures_keep_ipp_options_and_clean_downloaded_file(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("bad PPD")
    client, native = printer_adapter({"printer-resolution-supported": [(300, 300, 3)]}, SimpleNamespace(), path)
    client.cups.PPD.side_effect = RuntimeError("cannot parse")
    keys = [option["key"] for option in client.printers()["printers"][0]["capabilities"]["print_options"]]
    assert "printer-resolution" in keys and not path.exists()
    native.getPPD.side_effect = RuntimeError("driverless has no PPD")
    keys = [option["key"] for option in client.printers()["printers"][0]["capabilities"]["print_options"]]
    assert "printer-resolution" in keys


def test_custom_media_pseudo_sizes_become_bounds_and_not_regular_choices():
    client, _ = printer_adapter({"media-supported": ["iso_a4_210x297mm", "custom_min_89x127mm", "custom_max_215.9x1200mm"],
                                "media-size-supported": "(unknown IPP value tag 0x34)"})
    caps = client.printers()["printers"][0]["capabilities"]
    assert caps["media"] == ["iso_a4_210x297mm"]
    assert caps["custom_media"] == {"min_width": 89, "max_width": 215.9, "min_height": 127, "max_height": 1200, "unit": "mm"}


def test_ipp_custom_media_range_uses_hundredths_of_a_millimetre():
    client, _ = printer_adapter({"media-size-supported": [{"x-dimension": 21000, "y-dimension": 29700},
                                                        {"x-dimension": (9000, 21600), "y-dimension": (13000, 120000)}]})
    caps = client.printers()["printers"][0]["capabilities"]
    assert caps["custom_media"] == {"min_width": 90, "max_width": 216, "min_height": 130, "max_height": 1200, "unit": "mm"}
    client, _ = printer_adapter({"media-col-database": [{"media-size": {"x-dimension": 21000, "y-dimension": 29700}}]})
    assert client.printers()["printers"][0]["capabilities"]["custom_media"] is None


@pytest.mark.parametrize("key", ["Resolution", "SetResolution", "JCLResolution"])
def test_vendor_resolution_alias_uses_actual_postscript_dpi(tmp_path, key):
    path = tmp_path / "temporary.ppd"
    path.write_text(f'*{key} HQ1200/High quality: "<</HWResolution[2400 600]>>setpagedevice"\n')
    ppd = SimpleNamespace(optionGroups=[ppd_group("General", [ppd_option(key, ["600dpi", "HQ1200"])])], constraints=[])
    client, _ = printer_adapter({"printer-resolution-supported": [(600, 600, 3), (2400, 600, 3)]}, ppd, path)
    caps = client.printers()["printers"][0]["capabilities"]
    assert caps["option_aliases"]["printer-resolution"]["2400x600dpi"] == {key: "HQ1200"}
    assert key not in {option["key"] for option in caps["print_options"]}


def test_known_driver_aliases_merge_equivalent_controls_but_keep_unique_modes(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("fixture")
    ppd = SimpleNamespace(optionGroups=[ppd_group("General", [
        ppd_option("MediaType", ["Stationery", "PhotographicGlossy", "VendorSpecial"]),
        ppd_option("cupsPrintQuality", ["Normal", "High"]),
        ppd_option("OutputBin", ["FaceUp"]), ppd_option("InputSlot", ["TRAY1", "MPTRAY"]),
    ])], constraints=[])
    client, _ = printer_adapter({"media-type-supported": ["stationery", "photographic-glossy"],
                                "print-quality-supported": [4, 5], "output-bin-supported": ["face-up"],
                                "media-source-supported": ["tray-1", "by-pass-tray"]}, ppd, path)
    caps = client.printers()["printers"][0]["capabilities"]
    controls = {option["key"]: option for option in caps["print_options"]}
    assert not {"cupsPrintQuality", "OutputBin", "InputSlot"} & set(controls)
    assert controls["MediaType"]["choices"] == [{"value": "VendorSpecial", "label": "Choice VendorSpecial"}]
    assert caps["option_aliases"]["print-quality"]["5"] == {"cupsPrintQuality": "High"}
    assert caps["option_aliases"]["media-type"]["stationery"] == {"MediaType": "Stationery"}
    assert caps["option_aliases"]["media-source"]["by-pass-tray"] == {"InputSlot": "MPTRAY"}


def test_primary_ipp_controls_do_not_hide_unique_driver_modes(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("fixture")
    ppd = SimpleNamespace(optionGroups=[ppd_group("General", [
        ppd_option("Duplex", ["None", "DuplexNoTumble", "Booklet"]),
        ppd_option("ColorModel", ["Gray", "RGB", "CMYK"], "RGB"),
    ])], constraints=[])
    client, _ = printer_adapter({"sides-supported": ["one-sided", "two-sided-long-edge"],
                                "print-color-mode-supported": ["monochrome", "color"]}, ppd, path)
    controls = {option["key"]: option for option in client.printers()["printers"][0]["capabilities"]["print_options"]}
    assert [choice["value"] for choice in controls["Duplex"]["choices"]] == ["Booklet"]
    assert [choice["value"] for choice in controls["ColorModel"]["choices"]] == ["CMYK"]


def conflict_ppd():
    selected = {}
    ppd = Mock()
    ppd.optionGroups = [ppd_group("General", [ppd_option("MediaType", ["Plain", "Transparency"]),
                     ppd_option("Duplex", ["None", "DuplexTumble"]), ppd_option("PageSize", ["A4", "Letter"])])]
    ppd.markDefaults.side_effect = lambda: selected.update(MediaType="Plain", Duplex="None", PageSize="A4")
    ppd.markOption.side_effect = lambda key, value: selected.update({key: value})
    ppd.conflicts.side_effect = lambda: int(selected["MediaType"] == "Transparency" and selected["Duplex"] == "DuplexTumble")
    return ppd


def test_whole_driver_selection_is_checked_before_submission(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("fixture")
    ppd = conflict_ppd()
    client, native = printer_adapter({}, ppd, path)
    with pytest.raises(ValueError, match="不能同時使用"):
        client.validate_job_options("test", {"MediaType": "Transparency", "sides": "two-sided-short-edge", "media": "iso_a4_210x297mm"})
    assert not path.exists()
    ppd.markOption.assert_any_call("PageSize", "A4")
    ppd.markOption.assert_any_call("Duplex", "DuplexTumble")
    native.printFile.assert_not_called()


def test_pair_of_driver_controls_is_not_filtered_just_because_it_conflicts_with_defaults(tmp_path):
    path = tmp_path / "temporary.ppd"
    path.write_text("fixture")
    ppd = conflict_ppd()
    client, native = printer_adapter({}, ppd, path)
    client.validate_job_options("test", {"MediaType": "Transparency", "sides": "one-sided"})
    assert not path.exists()
    ppd.markOption.assert_any_call("MediaType", "Transparency")
    ppd.markOption.assert_any_call("Duplex", "None")
    native.printFile.assert_not_called()


def test_driverless_validation_does_not_modify_or_submit_anything():
    client, native = printer_adapter({})
    client.cups.PPD = Mock()
    native.getPPD.side_effect = RuntimeError("no PPD")
    client.validate_job_options("test", {"printer-resolution": "300dpi"})
    native.printFile.assert_not_called()
    native.addPrinter.assert_not_called()


def test_family_page_gets_plain_printer_warnings_and_print_modes():
    client, _ = printer_adapter({"printer-state-reasons": ["media-empty-error", "toner-low-warning", "com.vendor-x-report", "paused"],
                                "print-quality-supported": [3, 4, 5], "print-color-mode-supported": ["monochrome", "color"]})
    printer = client.printers()["printers"][0]
    assert printer["reasons"] == ["缺紙", "碳粉快用完"]
    assert [preset["id"] for preset in printer["capabilities"]["presets"]] == ["standard", "economy", "fine"]


MARKERS = {
    "marker-names": ["Black Toner", "Cyan Ink", "Tri-color", "Waste Ink Box", "Drum Unit", "Staples"],
    "marker-colors": ["#000000", "#00ffff", "#00FFFF#FF00FF#FFFF00", "#000000", "none", "none"],
    "marker-levels": [42, 9, -3, 80, 30, -1],
    "marker-low-levels": [5, 10, 10, 90, 35, 0],
    "marker-types": ["toner", "ink", "ink-cartridge", "waste-ink", "opc", "staples"],
    "marker-change-time": 1760000000,
}


def test_supply_levels_are_named_in_plain_words_and_unknown_levels_are_hidden():
    client, _ = printer_adapter({**MARKERS, "printer-state-reasons": ["marker-supply-low-warning", "media-low-report"]})
    printer = client.printers()["printers"][0]
    assert [(supply["label"], supply["level"], supply["low"]) for supply in printer["supplies"]] == [
        ("黑色碳粉", 42, False), ("青色墨水", 9, True), ("感光鼓", 30, True)]
    assert printer["supplies"][1]["color"] == "#00FFFF"
    # The named supply line replaces the generic warning; other reasons stay.
    assert printer["reasons"] == ["紙張快用完"]


@pytest.mark.parametrize("attrs", [{}, {"marker-levels": [-1, -2, -3], "marker-names": ["A", "B", "C"]},
                                   {"marker-levels": "unknown"}, {"marker-levels": 55, "marker-names": "Black", "marker-types": "toner"}])
def test_missing_or_unknown_supply_levels_never_read_as_empty(attrs):
    client, _ = printer_adapter({**attrs, "printer-state-reasons": ["toner-low-warning"]})
    printer = client.printers()["printers"][0]
    assert all(supply["level"] > 0 for supply in printer["supplies"])
    assert printer["reasons"] == ["碳粉快用完"]


def test_admin_list_shows_supplies_and_the_time_they_were_reported():
    native = Mock()
    native.getDefault.return_value = "office"
    native.getPrinters.return_value = {"office": {"device-uri": "usb://Brother/MFC", "printer-state": 3, "printer-info": "Office"}}
    native.getPrinterAttributes.return_value = {"printer-is-accepting-jobs": True, **MARKERS}
    printer = adapter(native).admin_printers()["printers"][0]
    assert [supply["label"] for supply in printer["supplies"]] == ["黑色碳粉", "青色墨水", "感光鼓"]
    assert printer["supplies_time"] == 1760000000
    assert "marker-levels" in native.getPrinterAttributes.call_args.kwargs["requested_attributes"]
