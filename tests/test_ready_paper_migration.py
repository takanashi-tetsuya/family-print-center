from pathlib import Path
from unittest.mock import Mock

import pytest

from print_center.cups_client import CupsError
from print_center.printer_profiles_cli import HOUSEHOLD_PRINTERS, apply_profiles, plan_profiles
from tests.test_printer_profiles import migration_sources


EPSON_PPD = b'''*PPD-Adobe: "4.3"
*ModelName: "EPSON L6270 Series"
*NickName: "EPSON L6270 Series, driverless, cups-filters"
*DefaultPageSize: Letter
*PageSize Letter/US Letter: "<</PageSize[612 792]>>setpagedevice"
*PageSize A4/A4: "<</PageSize[595.275590551181 841.889763779528]>>setpagedevice"
*DefaultPageRegion: Letter
*PageRegion Letter/US Letter: "<</PageSize[612 792]>>setpagedevice"
*PageRegion A4/A4: "<</PageSize[595.275590551181 841.889763779528]>>setpagedevice"
*DefaultMediaType: Stationery
*DefaultDuplex: None
*DefaultcupsPrintQuality: Normal
'''


@pytest.fixture
def sources(tmp_path):
    cups, native, discovery, devices = migration_sources()
    name = "epson_l6276"
    ppds = {name: EPSON_PPD}
    downloads = []

    def get_ppd(queue):
        path = tmp_path / f"download-{len(downloads)}.ppd"
        path.write_bytes(ppds[queue])
        downloads.append(path)
        return str(path)

    native.getPPD.side_effect = get_ppd
    native.getJobs.return_value = {}
    cups.modify_printer.side_effect = lambda queue, **options: ppds.update({queue: Path(options["ppd_file"]).read_bytes()})
    direct = Mock()
    direct.getPrinterAttributes.return_value = {
        "printer-uuid": "urn:uuid:" + devices[name]["identity"],
        "media-ready": "iso_a4_210x297mm", "media-default": "na_letter_8.5x11in",
    }
    cups.cups.Connection.return_value = direct
    return cups, native, discovery, devices, ppds, direct, downloads


def paper_plan(sources, **options):
    cups, _, discovery, *_ = sources
    return plan_profiles(cups, discovery, ["epson_l6276"], **options)


def test_default_migration_keeps_paper_settings_and_does_not_contact_printer(sources):
    cups, native, _, _, ppds, direct, _ = sources
    plans = paper_plan(sources)
    apply_profiles(cups, plans)
    assert plans[0]["paper_defaults"]["status"] == "not_requested"
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()
    cups.cups.Connection.assert_not_called()
    native.getPPD.assert_not_called()


def test_opt_in_dry_run_reads_physical_ipp_without_submitting_or_changing(sources):
    cups, native, _, devices, ppds, direct, downloads = sources
    plans = paper_plan(sources, align_ready_paper=True)
    assert plans[0]["paper_defaults"] == {"needs_configuration": True, "changed": False, "status": "align_a4"}
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()
    cups.cups.Connection.assert_called_once_with(
        host="epson_l6276.local", port=631, encryption=cups.cups.HTTP_ENCRYPT_NEVER)
    direct.getPrinterAttributes.assert_called_once_with(
        uri=devices["epson_l6276"]["ipp_uri"], requested_attributes=["printer-uuid", "media-ready", "media-default"])
    assert cups.cups.setPasswordCB.call_args.args[0]("Password:") == ""
    assert all(not path.exists() for path in downloads)
    native.printFile.assert_not_called()
    cups.print_file.assert_not_called()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_apply_changes_only_two_defaults_and_is_idempotent(sources, newline):
    cups, native, _, _, ppds, _, downloads = sources
    original = EPSON_PPD.replace(b"\n", newline)
    ppds["epson_l6276"] = original
    plans = paper_plan(sources, align_ready_paper=True)
    apply_profiles(cups, plans)
    expected = original.replace(b"*DefaultPageSize: Letter", b"*DefaultPageSize: A4").replace(
        b"*DefaultPageRegion: Letter", b"*DefaultPageRegion: A4")
    assert ppds["epson_l6276"] == expected
    assert cups.modify_printer.call_count == 1
    assert plans[0]["paper_defaults"] == {"needs_configuration": False, "changed": True, "status": "aligned"}
    again = paper_plan(sources, align_ready_paper=True)
    apply_profiles(cups, again)
    assert again[0]["paper_defaults"]["status"] == "aligned"
    assert cups.modify_printer.call_count == 1
    assert all(not path.exists() for path in downloads)
    native.printFile.assert_not_called()


@pytest.mark.parametrize("defaults", [(b"Legal", b"Legal"), (b"A4", b"Letter"), (b"A4.Borderless", b"A4.Borderless")])
def test_nonfactory_and_mixed_user_defaults_are_preserved(sources, defaults):
    cups, _, _, _, ppds, direct, _ = sources
    ppds["epson_l6276"] = EPSON_PPD.replace(b"DefaultPageSize: Letter", b"DefaultPageSize: " + defaults[0]).replace(
        b"DefaultPageRegion: Letter", b"DefaultPageRegion: " + defaults[1])
    before = ppds["epson_l6276"]
    plans = paper_plan(sources, align_ready_paper=True)
    apply_profiles(cups, plans)
    assert plans[0]["paper_defaults"]["status"] == "custom_default_preserved"
    assert ppds["epson_l6276"] == before
    cups.modify_printer.assert_not_called()
    direct.getPrinterAttributes.assert_not_called()


@pytest.mark.parametrize("ready", [None, [], "na_letter_8.5x11in", ["iso_a4_210x297mm", "na_letter_8.5x11in"], [None]])
def test_missing_or_ambiguous_physical_ready_paper_does_not_change_ppd(sources, ready):
    cups, _, _, _, ppds, direct, _ = sources
    direct.getPrinterAttributes.return_value["media-ready"] = ready
    plans = paper_plan(sources, align_ready_paper=True)
    apply_profiles(cups, plans)
    assert plans[0]["paper_defaults"]["status"] == "ready_a4_not_confirmed"
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()


def test_another_ipp_printer_uuid_is_rejected(sources):
    cups, _, _, devices, ppds, direct, _ = sources
    direct.getPrinterAttributes.return_value["printer-uuid"] = "urn:uuid:" + devices["brother_mfc8515dn"]["identity"]
    with pytest.raises(ValueError, match="IPP 打印機身份"):
        paper_plan(sources, align_ready_paper=True)
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()


def test_apply_rechecks_uri_and_loaded_paper(sources):
    cups, native, _, _, ppds, direct, _ = sources
    plans = paper_plan(sources, align_ready_paper=True)
    direct.getPrinterAttributes.return_value["media-ready"] = "na_letter_8.5x11in"
    apply_profiles(cups, plans)
    assert plans[0]["paper_defaults"]["status"] == "ready_a4_not_confirmed"
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()
    native.getPrinters.return_value["epson_l6276"]["device-uri"] = "dnssd://Other._ipp._tcp.local./"
    with pytest.raises(ValueError, match="連線已變更"):
        apply_profiles(cups, plans)


@pytest.mark.parametrize("jobs,status", [
    ({1: {"job-printer-uri": "ipp://localhost/printers/epson_l6276"}}, "pending_jobs"),
    ({1: {}}, "pending_jobs_unknown"),
])
def test_pending_or_unknown_jobs_keep_original_defaults(sources, jobs, status):
    cups, native, _, _, ppds, _, _ = sources
    native.getJobs.return_value = jobs
    plans = paper_plan(sources, align_ready_paper=True)
    apply_profiles(cups, plans)
    assert plans[0]["paper_defaults"]["status"] == status
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()


def test_verification_failure_rolls_back_all_previous_ppd_defaults(sources):
    cups, _, _, _, ppds, _, _ = sources
    plans = paper_plan(sources, align_ready_paper=True)
    updates = []

    def incomplete_update(queue, **options):
        value = Path(options["ppd_file"]).read_bytes()
        updates.append(value)
        ppds[queue] = value.replace(b"DefaultDuplex: None", b"DefaultDuplex: DuplexNoTumble") if len(updates) == 1 else value

    cups.modify_printer.side_effect = incomplete_update
    with pytest.raises(CupsError, match="已回復原有 PPD"):
        apply_profiles(cups, plans)
    assert len(updates) == 2 and ppds["epson_l6276"] == EPSON_PPD


@pytest.mark.parametrize("original,replacement,status", [
    (b"EPSON L6270 Series", b"Other Printer", "unrecognized_ppd"),
    (b"*DefaultPageRegion: Letter", b"*DefaultPageRegion: Letter\n*DefaultPageRegion: Letter", "unrecognized_ppd"),
    (b"*PageRegion A4/A4", b"*PageRegion Legal/Legal", "a4_not_supported"),
])
def test_unknown_ppd_or_unsupported_a4_is_never_modified(sources, original, replacement, status):
    cups, _, _, _, ppds, direct, _ = sources
    ppds["epson_l6276"] = EPSON_PPD.replace(original, replacement)
    before = ppds["epson_l6276"]
    plans = paper_plan(sources, align_ready_paper=True)
    apply_profiles(cups, plans)
    assert plans[0]["paper_defaults"]["status"] == status
    assert ppds["epson_l6276"] == before
    direct.getPrinterAttributes.assert_not_called()
    cups.modify_printer.assert_not_called()


def test_ppd_changed_while_reading_ready_paper_is_not_overwritten(sources):
    cups, _, _, _, ppds, direct, _ = sources
    plans = paper_plan(sources, align_ready_paper=True)
    attributes = direct.getPrinterAttributes.return_value

    def changing_printer(**_options):
        ppds["epson_l6276"] = EPSON_PPD.replace(b"DefaultDuplex: None", b"DefaultDuplex: DuplexNoTumble")
        return attributes

    direct.getPrinterAttributes.side_effect = changing_printer
    with pytest.raises(ValueError, match="設定已被更改"):
        apply_profiles(cups, plans)
    assert b"*DefaultDuplex: DuplexNoTumble" in ppds["epson_l6276"]
    assert b"*DefaultPageSize: Letter" in ppds["epson_l6276"]
    cups.modify_printer.assert_not_called()


def test_failed_rollback_is_reported_without_claiming_settings_are_restored(sources):
    cups, _, _, _, ppds, _, _ = sources
    plans = paper_plan(sources, align_ready_paper=True)
    cups.modify_printer.side_effect = RuntimeError("CUPS unavailable")
    with pytest.raises(CupsError, match="未能回復原有 PPD"):
        apply_profiles(cups, plans)
    assert cups.modify_printer.call_count == 2


def test_cups_ppd_validation_failure_does_not_touch_queue(sources):
    cups, _, _, _, ppds, _, downloads = sources
    plans = paper_plan(sources, align_ready_paper=True)
    cups.check_ppd.side_effect = ValueError("PPD invalid")
    with pytest.raises(ValueError, match="PPD invalid"):
        apply_profiles(cups, plans)
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()
    assert all(not path.exists() for path in downloads)


def test_direct_ipp_read_failure_cannot_change_queue(sources):
    cups, _, _, _, ppds, direct, _ = sources
    direct.getPrinterAttributes.side_effect = RuntimeError("IPP unavailable")
    with pytest.raises(CupsError, match="無法讀取打印機實際裝紙"):
        paper_plan(sources, align_ready_paper=True)
    assert ppds["epson_l6276"] == EPSON_PPD
    cups.modify_printer.assert_not_called()


def test_ipps_read_requires_encryption(sources):
    cups, _, _, devices, _, _, _ = sources
    devices["epson_l6276"]["ipp_uri"] = "ipps://epson.local:443/ipp/print"
    paper_plan(sources, align_ready_paper=True)
    cups.cups.Connection.assert_called_once_with(host="epson.local", port=443, encryption=cups.cups.HTTP_ENCRYPT_ALWAYS)


@pytest.mark.parametrize("uri", ["ipp://attacker.invalid/ipp/print", "socket://epson.local:9100",
                                "ipp://user:password@epson.local:631/ipp/print"])
def test_untrusted_or_credentialed_endpoints_are_not_contacted(sources, uri):
    cups, _, _, devices, ppds, _, _ = sources
    devices["epson_l6276"]["ipp_uri"] = uri
    with pytest.raises(ValueError, match="區域網路 IPP 位址"):
        paper_plan(sources, align_ready_paper=True)
    cups.cups.Connection.assert_not_called()
    cups.modify_printer.assert_not_called()


def test_existing_queue_uuid_is_the_authority_without_source_household_identifiers():
    cups, native, discovery, devices = migration_sources()
    alternate = "11111111-2222-4333-8444-555555555555"
    device = devices["epson_l6276"]
    device["identity"] = alternate
    device["uri"] = "dnssd://Epson._ipp._tcp.local./?uuid=" + alternate
    native.getPrinters.return_value["epson_l6276"]["device-uri"] = device["uri"]
    plan = plan_profiles(cups, discovery, ["epson_l6276"])[0]
    assert plan["device"]["identity"] == alternate
    assert isinstance(HOUSEHOLD_PRINTERS, tuple)


def test_an_arbitrary_existing_dnssd_queue_can_be_registered():
    cups, native, discovery, devices = migration_sources()
    original = devices["epson_l6276"]
    native.getPrinters.return_value["study"] = native.getPrinters.return_value.pop("epson_l6276")
    plan = plan_profiles(cups, discovery, ["study"])[0]
    assert plan["name"] == "study" and plan["device"]["identity"] == original["identity"]
    assert plan["paper_defaults"]["status"] == "not_requested"
    apply_profiles(cups, [plan])
    cups.register_profiles.assert_called_once_with("study", {"driverless": "study"}, "driverless")


@pytest.mark.parametrize("uri", ["dnssd://Epson._ipp._tcp.local./", "ipp://printer.local/ipp/print",
                                "dnssd://Epson._ipp._tcp.local./?uuid=invalid",
                                "dnssd://Epson._ipp._tcp.local./?uuid=11111111-2222-4333-8444-555555555555&uuid=11111111-2222-4333-8444-555555555555"])
def test_missing_or_ambiguous_queue_identity_is_not_migrated(uri):
    cups, native, discovery, _ = migration_sources()
    native.getPrinters.return_value["epson_l6276"]["device-uri"] = uri
    with pytest.raises(ValueError, match="唯一且已確認"):
        plan_profiles(cups, discovery, ["epson_l6276"])
    cups.modify_printer.assert_not_called()
