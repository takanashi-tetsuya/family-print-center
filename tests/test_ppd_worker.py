"""Native PPD regression checks without a CUPS server or any print jobs."""
import gc
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from print_center import cups_client
from print_center.cups_client import CupsClient, CupsError


PPD = '''*PPD-Adobe: "4.3"
*FormatVersion: "4.3"
*FileVersion: "1.0"
*LanguageVersion: English
*LanguageEncoding: ISOLatin1
*PCFileName: "FPC.PPD"
*Manufacturer: "Generic"
*Product: "(Regression Printer)"
*ModelName: "Regression Printer"
*NickName: "Regression Printer"
*PSVersion: "(3010.000) 0"
*LanguageLevel: "3"
*ColorDevice: False
*DefaultColorSpace: Gray
*FileSystem: False
*Throughput: "1"
*OpenGroup: General/General
*OpenUI *PageSize/Page Size: PickOne
*OrderDependency: 10 AnySetup *PageSize
*DefaultPageSize: A4
*PageSize A4/A4: "<</PageSize[595 842]>>setpagedevice"
*PageSize Letter/Letter: "<</PageSize[612 792]>>setpagedevice"
*CloseUI: *PageSize
*DefaultImageableArea: A4
*ImageableArea A4: "0 0 595 842"
*ImageableArea Letter: "0 0 612 792"
*DefaultPaperDimension: A4
*PaperDimension A4: "595 842"
*PaperDimension Letter: "612 792"
*OpenUI *Duplex/Double Sided: PickOne
*OrderDependency: 20 AnySetup *Duplex
*DefaultDuplex: None
*Duplex None/Off: "<</Duplex false>>setpagedevice"
*Duplex DuplexNoTumble/Long Edge: "<</Duplex true /Tumble false>>setpagedevice"
*Duplex DuplexTumble/Short Edge: "<</Duplex true /Tumble true>>setpagedevice"
*CloseUI: *Duplex
*OpenUI *MediaType/Media Type: PickOne
*OrderDependency: 30 AnySetup *MediaType
*DefaultMediaType: Plain
*MediaType Plain/Plain Paper: "<</MediaType(Plain)>>setpagedevice"
*MediaType Transparency/Transparency: "<</MediaType(Transparency)>>setpagedevice"
*CloseUI: *MediaType
*OpenSubGroup: Quality/Quality
*OpenUI *Resolution/Resolution: PickOne
*OrderDependency: 40 AnySetup *Resolution
*DefaultResolution: 600dpi
*Resolution 300dpi/300 DPI: "<</HWResolution[300 300]>>setpagedevice"
*Resolution 600dpi/600 DPI: "<</HWResolution[600 600]>>setpagedevice"
*Resolution HQ1200/High Quality: "<</HWResolution[2400 600]>>setpagedevice"
*CloseUI: *Resolution
*CloseSubGroup: Quality
*CloseGroup: General
*UIConstraints: *MediaType Transparency *Duplex DuplexTumble
'''


@pytest.fixture
def native_adapter(tmp_path):
    cups = pytest.importorskip("cups")
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("Native descriptor regression uses Linux /proc")
    client = CupsClient.__new__(CupsClient)
    client.cups = cups
    paths = []

    def get_ppd(_name):
        path = tmp_path / f"download-{len(paths)}.ppd"
        path.write_text(PPD)
        paths.append(path)
        return str(path)

    connection = SimpleNamespace(getPPD=get_ppd)
    client._connection = lambda: connection
    return client, connection, paths


def descriptor_count():
    return len(list(Path("/proc/self/fd").iterdir()))


def test_repeated_real_ppd_queries_and_validation_do_not_leak_parent_descriptors(native_adapter):
    client, connection, paths = native_adapter
    attrs = {"media-supported": ["iso_a4_210x297mm"],
             "sides-supported": ["one-sided", "two-sided-long-edge", "two-sided-short-edge"]}
    ipp_options = [{"key": "printer-resolution", "label": "DPI", "category": "quality",
                    "choices": [{"value": "600dpi", "label": "600 DPI"}], "default": "600dpi"}]
    gc.collect()
    initial = descriptor_count()
    for _ in range(60):
        controls, aliases = client._ppd_options(connection, "fixture", attrs, ipp_options)
        by_key = {item["key"]: item for item in controls}
        assert {"printer-resolution", "Resolution", "MediaType"} <= by_key.keys()
        assert "PageSize" not in by_key and "Duplex" not in by_key
        assert aliases["printer-resolution"]["600dpi"] == {"Resolution": "600dpi"}
        assert {choice["value"] for choice in by_key["Resolution"]["choices"]} == {"300dpi", "HQ1200"}
        client.validate_job_options("fixture", {"media": "iso_a4_210x297mm", "MediaType": "Plain",
                                                "sides": "two-sided-long-edge", "Resolution": "600dpi"})
        assert not paths[-1].exists() and not paths[-2].exists()
    gc.collect()
    assert descriptor_count() <= initial + 1
    assert len(paths) == 120


def test_real_ppd_conflict_rejected_and_default_can_be_overridden(native_adapter):
    client, _, paths = native_adapter
    with pytest.raises(ValueError, match="不能同時使用"):
        client.validate_job_options("fixture", {"MediaType": "Transparency", "sides": "two-sided-short-edge"})
    client.validate_job_options("fixture", {"MediaType": "Transparency", "sides": "one-sided"})
    assert all(not path.exists() for path in paths)


def test_downloaded_malformed_ppd_refuses_validation_but_retains_ipp_controls(native_adapter):
    client, connection, paths = native_adapter
    original = connection.getPPD

    def malformed(name):
        path = Path(original(name))
        path.write_text("Not a PPD")
        return str(path)

    connection.getPPD = malformed
    with pytest.raises(CupsError, match="核對打印機"):
        client.validate_job_options("fixture", {"sides": "one-sided"})
    ipp_controls = [{"key": "number-up", "choices": [{"value": "1", "label": "1"}]}]
    assert client._ppd_options(connection, "fixture", {}, ipp_controls) == (ipp_controls, {})
    assert all(not path.exists() for path in paths)


def test_missing_ppd_preserves_ipp_capability_validation_fallback(native_adapter):
    client, connection, paths = native_adapter

    def absent(_name):
        raise RuntimeError("IPP-only queue has no PPD")

    connection.getPPD = absent
    client.validate_job_options("fixture", {"sides": "one-sided"})
    assert paths == []


@pytest.mark.parametrize("failure", ["timeout", "crash", "invalid_json", "invalid_reply", "unavailable"])
def test_worker_failure_falls_back_for_controls_but_refuses_validation_and_cleans_files(native_adapter, monkeypatch, failure):
    client, connection, paths = native_adapter

    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        if failure == "crash":
            return SimpleNamespace(returncode=1, stdout="")
        if failure == "invalid_json":
            return SimpleNamespace(returncode=0, stdout="not JSON")
        if failure == "unavailable":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"operation": "validate", "available": False, "conflict": False}))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"operation": "validate", "available": "yes", "conflict": False}))

    monkeypatch.setattr(cups_client.subprocess, "run", fail)
    controls = [{"key": "printer-resolution", "choices": []}]
    assert client._ppd_options(connection, "fixture", {}, controls) == (controls, {})
    with pytest.raises(CupsError, match="核對打印機"):
        client.validate_job_options("fixture", {"sides": "one-sided"})
    assert all(not path.exists() for path in paths)


def test_worker_receives_only_ppd_inputs_without_credentials(native_adapter, monkeypatch):
    client, connection, paths = native_adapter
    monkeypatch.setenv("CUPS_PASSWORD_FILE", "/private/credential")
    monkeypatch.setenv("ADMIN_PASSWORD", "do-not-forward")
    monkeypatch.setenv("CUPS_SERVER", "private-server")
    seen = {}

    def respond(command, **kwargs):
        seen.update(command=command, **kwargs)
        payload = json.loads(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"operation": payload["operation"], "options": [], "aliases": {}}))

    monkeypatch.setattr(cups_client.subprocess, "run", respond)
    controls, aliases = client._ppd_options(connection, "fixture", {"media-supported": ["iso_a4_210x297mm"],
                                                                  "printer-uuid": b"unrelated binary", "password": "secret"}, [])
    assert (controls, aliases) == ([], {})
    payload = json.loads(seen["input"])
    assert payload["attrs"] == {"media-supported": ["iso_a4_210x297mm"]}
    assert {"CUPS_PASSWORD_FILE", "ADMIN_PASSWORD", "CUPS_SERVER"}.isdisjoint(seen["env"])
    assert "secret" not in seen["input"] and "do-not-forward" not in seen["input"]
    assert seen["timeout"] == 10
    assert not paths[-1].exists()
