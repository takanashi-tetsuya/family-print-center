from pathlib import Path

import pytest

from print_center.cups_client import CupsError
from print_center.printer_profiles import companion_name
from print_center.printer_profiles_cli import HOUSEHOLD_PRINTERS, apply_profiles, plan_profiles
from tests.test_printer_profiles import migration_sources


GENERIC_PPD = b'''*PPD-Adobe: "4.3"
*ModelName: "Generic PCL Laser Printer"
*NickName: "Generic PCL Laser Printer"
*1284DeviceID: "CMD:PCL;"
*cupsFilter: "application/vnd.cups-raster 50 rastertohp"
*UIConstraints: *Duplex *Option1 False
*UIConstraints: *Option1 False *Duplex
*OpenGroup: InstallableOptions/Installable Options
*OpenUI *Option1/Duplexer: Boolean
*DefaultOption1: False
*Option1 False/Not Installed: ""
*Option1 True/Installed: ""
*CloseUI: *Option1
*CloseGroup: InstallableOptions
*DefaultOption2: 2Trays
*DefaultDuplex: None
*Duplex None/Off (1-Sided): "<</Duplex false>>setpagedevice"
*Duplex DuplexNoTumble/Long-Edge (Portrait): "<</Duplex true/Tumble false>>setpagedevice"
*Duplex DuplexTumble/Short-Edge (Landscape): "<</Duplex true/Tumble true>>setpagedevice"
'''


@pytest.fixture
def sources(tmp_path):
    cups, native, discovery, devices = migration_sources()
    name = "brother_mfc8515dn"
    driver = companion_name(name, "driver")
    native.getPrinters.return_value[driver] = {
        "device-uri": devices[name]["uri"], "printer-make-and-model": "Generic PCL Laser Printer"}
    cups._profile_records.return_value = {
        name: {"queues": {"driver": driver, "driverless": name}, "default_mode": "driver"},
        "epson_l6276": {"queues": {"driverless": "epson_l6276"}, "default_mode": "driverless"},
    }
    native.getPrinterAttributes.return_value = {"sides-supported": ["one-sided", "two-sided-long-edge", "two-sided-short-edge"]}
    ppds = {driver: GENERIC_PPD}
    downloads = []

    def get_ppd(queue):
        path = tmp_path / f"download-{len(downloads)}.ppd"
        path.write_bytes(ppds[queue])
        downloads.append(path)
        return str(path)
    native.getPPD.side_effect = get_ppd
    cups.modify_printer.side_effect = lambda queue, **options: ppds.update({queue: Path(options["ppd_file"]).read_bytes()})
    return cups, native, discovery, devices, ppds, driver, downloads


def test_dry_run_reports_missing_verified_duplexer_without_modifying_ppd(sources):
    cups, native, discovery, _, ppds, driver, downloads = sources
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    assert plans[0]["operation"] == "keep"
    assert plans[0]["duplexer"] == {"needs_configuration": True, "changed": False, "status": "enable_duplexer"}
    assert plans[1]["duplexer"]["status"] == "not_applicable"
    assert ppds[driver] == GENERIC_PPD
    cups.modify_printer.assert_not_called()
    assert all(not path.exists() for path in downloads)


def test_apply_changes_only_default_option1_and_is_idempotent(sources):
    cups, native, discovery, _, ppds, driver, downloads = sources
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    apply_profiles(cups, plans)
    assert ppds[driver] == GENERIC_PPD.replace(b"*DefaultOption1: False", b"*DefaultOption1: True")
    assert cups.modify_printer.call_count == 1
    assert cups.modify_printer.call_args.args == (driver,)
    assert plans[0]["duplexer"] == {"needs_configuration": False, "changed": True, "status": "enabled"}
    again = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    assert again[0]["duplexer"]["status"] == "enabled"
    apply_profiles(cups, again)
    assert cups.modify_printer.call_count == 1
    assert all(not path.exists() for path in downloads)
    native.printFile.assert_not_called()
    cups.print_file.assert_not_called()


@pytest.mark.parametrize("sides", [["one-sided"], [], None, [None, {}]])
def test_ipp_duplex_must_be_confirmed_before_ppd_inspection_or_change(sources, sides):
    cups, native, discovery, _, ppds, driver, _ = sources
    native.getPrinterAttributes.return_value = {"sides-supported": sides}
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    assert plans[0]["duplexer"]["status"] == "ipp_duplex_not_confirmed"
    apply_profiles(cups, plans)
    assert ppds[driver] == GENERIC_PPD
    cups.modify_printer.assert_not_called()
    native.getPPD.assert_not_called()


@pytest.mark.parametrize("original,replacement", [
    (b'"Generic PCL Laser Printer"', b'"Brother Other Model"'),
    (b"Option1 True/Installed", b"Option1 True/Not Installed"),
    (b"rastertohp", b"otherfilter"),
    (b"*DefaultOption1: False", b"*DefaultOption1: False\n*DefaultOption1: False"),
])
def test_other_or_ambiguous_ppds_are_untouched(sources, original, replacement):
    cups, native, discovery, _, ppds, driver, _ = sources
    ppds[driver] = GENERIC_PPD.replace(original, replacement)
    before = ppds[driver]
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    assert plans[0]["duplexer"]["status"] == "unrecognized_ppd"
    apply_profiles(cups, plans)
    assert ppds[driver] == before
    cups.modify_printer.assert_not_called()


def test_apply_rechecks_same_device_uri_before_configuring_hardware(sources):
    cups, native, discovery, _, ppds, driver, _ = sources
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    native.getPrinters.return_value[driver]["device-uri"] = "dnssd://Other._ipp._tcp.local./"
    with pytest.raises(ValueError, match="連線已變更"):
        apply_profiles(cups, plans)
    assert ppds[driver] == GENERIC_PPD
    cups.modify_printer.assert_not_called()


def test_verification_failure_restores_the_original_ppd(sources):
    cups, native, discovery, _, ppds, driver, _ = sources
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    updates = []
    def incomplete_update(queue, **options):
        value = Path(options["ppd_file"]).read_bytes()
        updates.append(value)
        ppds[queue] = GENERIC_PPD if len(updates) == 1 else value
    cups.modify_printer.side_effect = incomplete_update
    with pytest.raises(CupsError, match="已回復原有 PPD"):
        apply_profiles(cups, plans)
    assert len(updates) == 2 and ppds[driver] == GENERIC_PPD
    native.printFile.assert_not_called()


def test_fresh_profile_checks_duplexer_after_driver_queue_is_created(sources):
    cups, native, discovery, _, ppds, driver, _ = sources
    native.getPrinters.return_value.pop(driver)
    cups._profile_records.return_value.pop("brother_mfc8515dn")
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    assert plans[0]["duplexer"]["status"] == "check_after_driver_creation"
    apply_profiles(cups, plans)
    assert ppds[driver].count(b"*DefaultOption1: True") == 1
    assert plans[0]["duplexer"]["status"] == "enabled"
