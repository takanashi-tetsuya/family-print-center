import copy
import io
import json
from contextlib import contextmanager
from types import MethodType
from unittest.mock import Mock

import pytest

from print_center.cups_client import CupsClient, CupsError
from print_center.printer_profiles import PrinterProfiles, companion_name
from print_center.printer_profiles_cli import HOUSEHOLD_PRINTERS, apply_profiles, plan_profiles
from print_center.admin import GENERIC_PCL, GENERIC_POSTSCRIPT
from tests.helpers import office_bytes, pdf_bytes
from tests.test_api import app, client, headers, uploaded, print_payload
from tests.test_cups_client import adapter


def test_profiles_persist_privately_and_reject_cross_printer_queues(tmp_path):
    store = PrinterProfiles(tmp_path)
    store.register("brother", {"driver": "brother_driver", "driverless": "brother"}, "driver")
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert PrinterProfiles(tmp_path).snapshot() == store.snapshot()
    with pytest.raises(ValueError):
        store.register("epson", {"driver": "brother_driver", "driverless": "epson"}, "driver")
    assert list(store.snapshot()) == ["brother"]


@pytest.mark.parametrize("record", [[], {"brother": {"queues": {"driver": "../other"}, "default_mode": "driver"}},
                                     {"brother": {"queues": {"driver": "brother", "driverless": "BROTHER"}, "default_mode": "driver"}}])
def test_corrupt_profiles_do_not_silently_change_print_destination(tmp_path, record):
    (tmp_path / "printer-profiles.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="設定檔損壞"):
        PrinterProfiles(tmp_path)


def native_profile_adapter(tmp_path, monkeypatch):
    native = Mock()
    printers = {"brother": {"device-uri": "dnssd://Brother._ipp._tcp.local./?uuid=1234", "printer-info": "Brother",
                            "printer-state": 3, "printer-make-and-model": "Brother MFC-8515DN, driverless, cups-filters"}}
    native.getPrinters.side_effect = lambda: copy.deepcopy(printers)
    native.getDefault.return_value = "brother"
    native.getPrinterAttributes.side_effect = lambda name, **kwargs: {
        "printer-make-and-model": printers[name]["printer-make-and-model"],
        "printer-resolution-supported": [(300, 300, 3)] if "driverless" in printers[name]["printer-make-and-model"] else [(600, 600, 3)],
        "printer-is-accepting-jobs": True,
    }

    def add(name, **kwargs):
        original = printers.get(name, {})
        printers[name] = {**original, "device-uri": kwargs.get("device", original.get("device-uri")),
                          "printer-info": kwargs.get("info", original.get("printer-info", "Brother")), "printer-state": 3,
                          "printer-make-and-model": "Brother, driverless" if "filename" in kwargs else "Generic PCL Laser Printer"}
    native.addPrinter.side_effect = add
    native.deletePrinter.side_effect = lambda name: printers.pop(name, None)
    native.setPrinterInfo.side_effect = lambda name, value: printers[name].update({"printer-info": value})
    native.setPrinterLocation.side_effect = lambda name, value: printers[name].update({"printer-location": value})
    cups = adapter(native)
    cups.configure_profiles(tmp_path)
    ppd = tmp_path / "ipp.ppd"
    ppd.write_text('*PPD-Adobe: "4.3"')

    @contextmanager
    def compatible(_uri):
        yield ppd
    monkeypatch.setattr(cups, "_driverless_ppd", compatible)
    return cups, native, printers, ppd


def test_existing_driverless_queue_gains_driver_without_replacing_ipp_ppd(tmp_path, monkeypatch):
    cups, native, printers, ppd = native_profile_adapter(tmp_path, monkeypatch)
    cups.register_profiles("brother", {"driverless": "brother"}, "driverless")
    cups.modify_profiled_printer("brother", driver="drv:///sample.drv/generpcl.ppd")
    driver_queue = companion_name("brother", "driver")
    assert set(printers) == {"brother", driver_queue}
    assert "driverless" in printers["brother"]["printer-make-and-model"]
    assert native.addPrinter.call_args_list[0].args == (driver_queue,)
    assert native.addPrinter.call_args.kwargs["device"] == printers["brother"]["device-uri"]
    public = cups.printers()
    assert public["default"] == "brother" and len(public["printers"]) == 1
    printer = public["printers"][0]
    assert printer["name"] == "brother" and printer["default_mode"] == "driver"
    assert [mode["id"] for mode in printer["print_modes"]] == ["driver", "driverless"]
    assert printer["print_modes"][0]["label"] == "通用 PCL 驅動"
    assert cups.resolve_print_mode(printer)["name"] == driver_queue
    assert cups.resolve_print_mode(printer, "driverless")["name"] == "brother"
    assert cups.resolve_print_mode(printer)["capabilities"]["print_options"][0]["choices"][0]["value"] == "600dpi"
    assert cups.resolve_print_mode(printer, "driverless")["capabilities"]["print_options"][0]["choices"][0]["value"] == "300dpi"
    assert {item["name"] for item in cups.admin_printers()["printers"]} == {"brother"}
    assert "_driver_name" not in printer and "_mode" not in printer
    # Selecting a profile only resolves a destination, with no shared queue mutation.
    assert native.addPrinter.call_count == 1
    native.printFile.assert_not_called()


def test_supply_levels_come_from_whichever_mode_queue_printed_last(tmp_path, monkeypatch):
    cups, native, printers, _ = native_profile_adapter(tmp_path, monkeypatch)
    cups.register_profiles("brother", {"driverless": "brother"}, "driverless")
    cups.modify_profiled_printer("brother", driver="drv:///sample.drv/generpcl.ppd")
    driver_queue = companion_name("brother", "driver")
    reported = {"brother": (1760000000, 80), driver_queue: (1760009999, 12)}
    plain = native.getPrinterAttributes.side_effect

    def attributes(name, **kwargs):
        when, level = reported[name]
        return {**plain(name, **kwargs), "marker-names": "Black Toner", "marker-colors": "#000000",
                "marker-types": "toner", "marker-levels": level, "marker-change-time": when}
    native.getPrinterAttributes.side_effect = attributes
    assert [supply["level"] for supply in cups.printers()["printers"][0]["supplies"]] == [12]
    admin = cups.admin_printers()["printers"][0]
    assert [supply["level"] for supply in admin["supplies"]] == [12] and admin["supplies_time"] == 1760009999
    reported[driver_queue] = (1700000000, 12)
    assert [supply["level"] for supply in cups.printers()["printers"][0]["supplies"]] == [80]
    assert "_supplies_time" not in cups.printers()["printers"][0]


def test_profiles_require_same_device_and_existing_queues(tmp_path, monkeypatch):
    cups, native, printers, _ = native_profile_adapter(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="尚未建立"):
        cups.register_profiles("brother", {"driver": "brother", "driverless": "missing"})
    printers["epson"] = {"device-uri": "dnssd://Epson._ipp._tcp.local./", "printer-make-and-model": "Epson"}
    with pytest.raises(ValueError, match="同一台"):
        cups.register_profiles("brother", {"driver": "brother", "driverless": "epson"})
    assert cups.profiles.snapshot() == {}
    native.addPrinter.assert_not_called()


def test_new_driver_and_compatibility_profiles_preserve_dnssd_and_default(tmp_path, monkeypatch):
    cups, native, printers, ppd = native_profile_adapter(tmp_path, monkeypatch)
    uri = "dnssd://New._ipp._tcp.local./?uuid=new"
    cups.add_profiled_printer("new", uri, "drv:///sample.drv/generpcl.ppd", driverless=True,
                              ipp_uri="ipp://new.local/ipp/print", make_default=True)
    calls = native.addPrinter.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["ppdname"] == "drv:///sample.drv/generpcl.ppd"
    assert calls[1].kwargs["filename"] == str(ppd)
    assert all(call.kwargs["device"] == uri for call in calls)
    assert native.setDefault.call_args.args == ("new",)
    assert cups.profiles.snapshot()["new"]["default_mode"] == "driver"


def test_profile_creation_failure_rolls_back_both_new_queues(tmp_path, monkeypatch):
    cups, native, printers, _ = native_profile_adapter(tmp_path, monkeypatch)
    add = native.addPrinter.side_effect
    def fail_second(name, **kwargs):
        if "filename" in kwargs:
            raise RuntimeError("failed")
        add(name, **kwargs)
    native.addPrinter.side_effect = fail_second
    with pytest.raises(CupsError):
        cups.add_profiled_printer("new", "dnssd://New._ipp._tcp.local./", "driver", driverless=True,
                                  ipp_uri="ipp://new.local/ipp/print")
    assert set(printers) == {"brother"}
    assert cups.profiles.snapshot() == {}
    native.setDefault.assert_called_with("brother")


def test_administrator_edits_and_delete_cover_both_profiles(tmp_path, monkeypatch):
    cups, native, printers, _ = native_profile_adapter(tmp_path, monkeypatch)
    cups.modify_profiled_printer("brother", driver="driver")
    driver_queue = companion_name("brother", "driver")
    cups.modify_profiled_printer("brother", info="書房", location="家裡", driver="everywhere")
    assert all(item["printer-info"] == "書房" and item["printer-location"] == "家裡" for item in printers.values())
    assert cups.profiles.snapshot()["brother"]["default_mode"] == "driver"
    assert native.addPrinter.call_count == 1
    cups.printer_action("brother", "pause")
    assert {call.args[0] for call in native.disablePrinter.call_args_list} == {"brother", driver_queue}
    cups.printer_action("brother", "delete")
    assert native.deletePrinter.call_args_list[-1].args == ("brother",)
    assert printers == {} and cups.profiles.snapshot() == {}


def test_unregistered_auxiliary_queue_is_never_public_even_before_app_reload(tmp_path, monkeypatch):
    cups, native, printers, _ = native_profile_adapter(tmp_path, monkeypatch)
    auxiliary = companion_name("brother", "driver")
    printers[auxiliary] = {**printers["brother"], "printer-make-and-model": "Generic PCL Laser Printer"}
    # Simulate a CLI that created the queue while the running process has its old map.
    assert cups.profiles.snapshot() == {}
    assert [printer["name"] for printer in cups.printers()["printers"]] == ["brother"]
    assert [printer["name"] for printer in cups.admin_printers()["printers"]] == ["brother"]


def test_failed_compatibility_setup_restores_existing_driver_ppd(tmp_path, monkeypatch):
    cups, native, printers, _ = native_profile_adapter(tmp_path, monkeypatch)
    printers["brother"]["printer-make-and-model"] = "Generic PCL Laser Printer"
    cups.register_profiles("brother", {"driver": "brother"}, "driver")
    old_ppd = tmp_path / "original.ppd"
    old_ppd.write_text('*PPD-Adobe: "4.3"\n*NickName: "Original Driver"\n')
    native.getPPD.return_value = str(old_ppd)

    @contextmanager
    def fail_compatibility(_uri):
        raise CupsError("IPP connection failed")
        yield
    monkeypatch.setattr(cups, "_driverless_ppd", fail_compatibility)
    with pytest.raises(CupsError, match="IPP connection failed"):
        cups.modify_profiled_printer("brother", driver="new-driver", info="New label", driverless=True,
                                     ipp_uri="ipp://brother.local/ipp/print")
    assert native.addPrinter.call_args_list[0].kwargs == {"ppdname": "new-driver"}
    assert native.addPrinter.call_args_list[1].kwargs == {"filename": str(old_ppd)}
    assert printers["brother"]["printer-info"] == "Brother"
    assert cups.profiles.snapshot()["brother"] == {"queues": {"driver": "brother"}, "default_mode": "driver"}
    assert not old_ppd.exists()


def test_failed_profile_save_restores_existing_labels(tmp_path, monkeypatch):
    cups, native, printers, _ = native_profile_adapter(tmp_path, monkeypatch)
    cups.register_profiles("brother", {"driverless": "brother"}, "driverless")
    monkeypatch.setattr(cups.profiles, "_save", Mock(side_effect=OSError("full disk")))
    with pytest.raises(OSError, match="full disk"):
        cups.modify_profiled_printer("brother", info="New label", location="New place")
    assert printers["brother"]["printer-info"] == "Brother"
    assert printers["brother"].get("printer-location") == ""
    assert cups.profiles.snapshot()["brother"]["default_mode"] == "driverless"


@pytest.fixture
def profiled_cups(app):
    cups = app.extensions["cups_client"]
    driver_caps = copy.deepcopy(cups.printer["capabilities"])
    ipp_caps = copy.deepcopy(driver_caps)
    ipp_caps["print_options"] = [{"key": "printer-resolution", "choices": [{"value": "300dpi"}]}]
    cups.printer.update(default_mode="driver", print_modes=[
        {"id": "driver", "label": "驅動", "capabilities": driver_caps, "state": 3, "accepting": True},
        {"id": "driverless", "label": "IPP", "capabilities": ipp_caps, "state": 3, "accepting": True},
    ])
    cups._profile_records = lambda: {"brother": {"queues": {"driver": "brother_driver", "driverless": "brother"}, "default_mode": "driver"}}
    cups.resolve_print_mode = MethodType(CupsClient.resolve_print_mode, cups)
    cups.validate_job_options = Mock()
    return cups


@pytest.mark.parametrize("mode,queue", [("auto", "brother_driver"), ("driver", "brother_driver"), ("driverless", "brother")])
def test_print_api_validates_and_submits_to_selected_fixed_queue(app, client, headers, uploaded, profiled_cups, mode, queue):
    result = client.post("/api/print", json=print_payload(uploaded, print_mode=mode), headers=headers)
    assert result.status_code == 200
    assert profiled_cups.prints[0][0] == queue
    assert profiled_cups.validate_job_options.call_args.args[0] == queue


@pytest.mark.parametrize("change", [{"printer": "brother_driver"}, {"print_mode": "other"}, {"print_mode": None},
                                    {"print_mode": {}}, {"print_mode": "driverless", "advanced_options": {"BrMediaType": "THICK"}},
                                    {"print_mode": "driverless", "advanced_options": {"printer-resolution": "600dpi"}}])
def test_hidden_queues_and_options_from_another_profile_cannot_be_submitted(app, client, headers, uploaded, profiled_cups, change):
    result = client.post("/api/print", json=print_payload(uploaded, **change), headers=headers)
    assert result.status_code == 400
    assert profiled_cups.prints == []
    profiled_cups.validate_job_options.assert_not_called()


def test_native_office_keeps_original_input_with_ipp_compatibility(app, client, headers, profiled_cups, monkeypatch):
    store = app.extensions["uploads"]
    monkeypatch.setattr(store, "capabilities", lambda: {"office": True})
    monkeypatch.setattr(store, "_office_preview", lambda source, target, directory: target.write_bytes(pdf_bytes()))
    source_id = client.post("/api/uploads", data={"file": (io.BytesIO(office_bytes("docx")), "document.docx")}, headers=headers).json["id"]
    native_print = Mock(return_value=80)
    monkeypatch.setattr(app.extensions["native_printer"], "print_file", native_print)
    result = client.post("/api/print", json=print_payload(source_id, print_mode="driverless"), headers=headers)
    assert result.status_code == 200
    queue, path = native_print.call_args.args[:2]
    assert queue == "brother" and path.name == "input.docx"
    assert profiled_cups.prints == []


def migration_sources():
    devices = {}
    for index, name in enumerate(HOUSEHOLD_PRINTERS, start=1):
        identity = f"00000000-0000-4000-8000-{index:012d}"
        brother = name.startswith("brother")
        devices[name] = {"id": name, "identity": identity, "kind": "ipp", "driverless": True,
                         "uri": f"dnssd://{name}._ipp._tcp.local./?uuid={identity}",
                         "ipp_uri": f"ipp://{name}.local/ipp/print",
                         "make_model": "Brother MFC-8515DN" if brother else "Epson L6270 Series",
                         "device_id": "MFG:Brother;MDL:MFC-8515DN;CMD:PJL,PCL,PCLXL,URF;" if brother else "MFG:Epson;MDL:L6270 Series;CMD:ESC/P-R;"}
    native = Mock()
    native.getPrinters.return_value = {
        name: {"device-uri": device["uri"], "printer-make-and-model": device["make_model"] + ", driverless"}
        for name, device in devices.items()}
    native.getPrinterAttributes.return_value = {"sides-supported": ["one-sided"]}
    cups = Mock()
    cups._call.side_effect = lambda callback: callback(native)
    cups._profile_records.return_value = {}
    cups.ppds.return_value = {
        GENERIC_PCL: {"ppd-make": "Generic", "ppd-make-and-model": "Generic PCL Laser Printer"},
        GENERIC_POSTSCRIPT: {"ppd-make": "Generic", "ppd-make-and-model": "Generic PostScript Printer"},
        "driverless:ipp://epson.local/ipp/print": {"ppd-make": "Epson", "ppd-make-and-model": "Epson L6270 Series, driverless"},
    }
    def create_driver(name, **_options):
        native.getPrinters.return_value[companion_name(name, "driver")] = {
            "device-uri": devices[name]["uri"], "printer-make-and-model": "Generic PCL Laser Printer"}
    cups.modify_profiled_printer.side_effect = create_driver
    discovery = Mock()
    discovery.snapshot.return_value = {"scanning": False, "devices": list(devices.values())}
    discovery.get.side_effect = lambda name: dict(devices[name])
    return cups, native, discovery, devices


def test_household_migration_dry_run_and_explicit_apply_keep_existing_ipp_queues():
    cups, native, discovery, _ = migration_sources()
    plans = plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    assert [plan["recommended_driver"] for plan in plans] == [GENERIC_PCL, "everywhere"]
    assert [plan["default_mode"] for plan in plans] == ["driver", "driverless"]
    assert plans[0]["queues"]["driverless"] == "brother_mfc8515dn"
    assert plans[1]["queues"] == {"driverless": "epson_l6276"}
    cups.modify_profiled_printer.assert_not_called()
    cups.register_profiles.assert_not_called()
    apply_profiles(cups, plans)
    assert cups.modify_profiled_printer.call_count == 1
    assert cups.modify_profiled_printer.call_args.args == ("brother_mfc8515dn",)
    assert cups.modify_profiled_printer.call_args.kwargs["driver"] == GENERIC_PCL
    cups.register_profiles.assert_called_once_with("epson_l6276", {"driverless": "epson_l6276"}, "driverless")
    cups.print_file.assert_not_called()
    native.printFile.assert_not_called()


def test_household_migration_requires_both_confirmed_devices_before_any_changes():
    cups, native, discovery, devices = migration_sources()
    devices["epson_l6276"]["identity"] = "some-other-printer"
    with pytest.raises(ValueError, match="唯一且已確認"):
        plan_profiles(cups, discovery, list(HOUSEHOLD_PRINTERS))
    cups.modify_profiled_printer.assert_not_called()
    cups.register_profiles.assert_not_called()
