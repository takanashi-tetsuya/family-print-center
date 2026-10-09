"""Exercise the native bridge against a fake submission ABI, with no printer/network."""
import os
import shutil
import subprocess
from ctypes.util import find_library
from pathlib import Path

import pytest

from print_center.native_office import NativeOfficePrinter


@pytest.fixture(scope="module")
def shim_harness(tmp_path_factory):
    compiler, library = shutil.which("cc"), find_library("cups")
    if not compiler or not library:
        pytest.skip("CUPS bridge ABI test requires a C compiler and libcups runtime")
    root = tmp_path_factory.mktemp("cups-shim-abi")
    source = Path(__file__).resolve().parent.parent / "deploy" / "cups_options_shim.c"
    subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared", str(source),
                    "-o", str(root / "bridge.so"), f"-l:{library}", "-ldl"], check=True, capture_output=True)
    (root / "fake.c").write_text(r'''
#include <stdio.h>
typedef struct _http_s http_t;
typedef struct cups_option_s { char *name; char *value; } cups_option_t;
static int submit(const char *api, int count, cups_option_t *options) {
    printf("%s\n", api);
    for (int i = 0; i < count; i++) printf("%s=%s\n", options[i].name, options[i].value);
    return 27;
}
int cupsPrintFile(const char *n, const char *f, const char *t, int c, cups_option_t *o) { return submit("cupsPrintFile", c, o); }
int cupsPrintFile2(http_t *h, const char *n, const char *f, const char *t, int c, cups_option_t *o) { return submit("cupsPrintFile2", c, o); }
int cupsPrintFiles(const char *n, int nf, const char **f, const char *t, int c, cups_option_t *o) { return submit("cupsPrintFiles", c, o); }
int cupsPrintFiles2(http_t *h, const char *n, int nf, const char **f, const char *t, int c, cups_option_t *o) { return submit("cupsPrintFiles2", c, o); }
''')
    (root / "main.c").write_text(r'''
#include <stdio.h>
#include <string.h>
typedef struct _http_s http_t;
typedef struct cups_option_s { char *name; char *value; } cups_option_t;
extern int cupsPrintFile(const char *, const char *, const char *, int, cups_option_t *);
extern int cupsPrintFile2(http_t *, const char *, const char *, const char *, int, cups_option_t *);
extern int cupsPrintFiles(const char *, int, const char **, const char *, int, cups_option_t *);
extern int cupsPrintFiles2(http_t *, const char *, int, const char **, const char *, int, cups_option_t *);
int main(int argc, char **argv) {
    cups_option_t options[] = {{"copies", "2"}, {"PageSize", "A4"}, {"Resolution", "300dpi"}, {"job-sheets", "none"},
                             {"Duplex", "None"}, {"InputSlot", "Default"}, {"ColorModel", "RGB"},
                             {"MediaType", "Plain"}, {"BrMediaType", "Plain"}};
    const char *files[] = {"original-rendered-spool.ps"};
    int result;
    if (!strcmp(argv[1], "cupsPrintFile")) result = cupsPrintFile(argv[2], files[0], argv[3], 9, options);
    else if (!strcmp(argv[1], "cupsPrintFile2")) result = cupsPrintFile2(NULL, argv[2], files[0], argv[3], 9, options);
    else if (!strcmp(argv[1], "cupsPrintFiles")) result = cupsPrintFiles(argv[2], 1, files, argv[3], 9, options);
    else result = cupsPrintFiles2(NULL, argv[2], 1, files, argv[3], 9, options);
    return result == 27 ? 0 : 1;
}
''')
    subprocess.run([compiler, "-fPIC", "-shared", str(root / "fake.c"), "-o", str(root / "libfakecups.so")],
                   check=True, capture_output=True)
    subprocess.run([compiler, str(root / "main.c"), "-o", str(root / "submit"), "-L", str(root), "-lfakecups",
                    f"-Wl,-rpath,{root}"], check=True, capture_output=True)
    return root


def run_harness(root, tmp_path, options, *, api="cupsPrintFile", printer="brother", title="FP-request source.docx", corrupt=None):
    options_file = tmp_path / "options.bin"
    NativeOfficePrinter._write_options(options_file, options)
    if corrupt is not None:
        options_file.write_bytes(corrupt)
    receipt = tmp_path / "receipt"
    env = {**os.environ, "LD_PRELOAD": str(root / "bridge.so"), "FPC_CUPS_OPTIONS_FILE": str(options_file),
           "FPC_CUPS_RECEIPT": str(receipt), "FPC_CUPS_PRINTER": "brother", "FPC_CUPS_JOB_PREFIX": "FP-request "}
    result = subprocess.run([str(root / "submit"), api, printer, title], env=env, capture_output=True, text=True)
    return result, receipt


@pytest.mark.parametrize("api", ["cupsPrintFile", "cupsPrintFile2", "cupsPrintFiles", "cupsPrintFiles2"])
def test_device_options_override_submission_while_copy_count_remains_with_renderer(shim_harness, tmp_path, api):
    result, receipt = run_harness(shim_harness, tmp_path, {"copies": "7", "page-ranges": "2-3", "media": "iso_a5_148x210mm",
                                                        "Resolution": "600dpi", "printer-resolution": "600dpi", "number-up": "4",
                                                        "VendorSetting": "Choice With Spaces"}, api=api)
    assert result.returncode == 0, result.stderr
    settings = dict(line.split("=", 1) for line in result.stdout.splitlines()[1:])
    assert settings["copies"] == "2" and "page-ranges" not in settings
    assert settings["Resolution"] == "600dpi" and settings["printer-resolution"] == "600dpi"
    assert settings["number-up"] == "4" and settings["VendorSetting"] == "Choice With Spaces"
    assert settings["media"] == "iso_a5_148x210mm" and "PageSize" not in settings
    assert receipt.read_text() == "27\n"


def test_auto_preserves_the_original_page_size_and_resolution(shim_harness, tmp_path):
    result, receipt = run_harness(shim_harness, tmp_path, {})
    settings = dict(line.split("=", 1) for line in result.stdout.splitlines()[1:])
    assert result.returncode == 0
    assert settings["PageSize"] == "A4" and settings["Resolution"] == "300dpi"
    assert "media" not in settings and "printer-resolution" not in settings
    assert settings["Duplex"] == "None" and settings["ColorModel"] == "RGB"
    assert settings["MediaType"] == "Plain" and settings["BrMediaType"] == "Plain"


def test_manual_ipp_choices_remove_conflicting_renderer_defaults_and_preserve_verified_aliases(shim_harness, tmp_path):
    result, _ = run_harness(shim_harness, tmp_path, {"sides": "two-sided-long-edge", "print-color-mode": "monochrome",
                                                  "media-source": "manual", "InputSlot": "Manual"})
    settings = dict(line.split("=", 1) for line in result.stdout.splitlines()[1:])
    assert result.returncode == 0
    assert "Duplex" not in settings and "ColorModel" not in settings
    assert settings["sides"] == "two-sided-long-edge" and settings["print-color-mode"] == "monochrome"
    assert settings["InputSlot"] == "Manual" and settings["media-source"] == "manual"


@pytest.mark.parametrize("driver_choice", [None, "Thick"])
def test_manual_media_type_overrides_both_generic_and_brother_renderer_defaults(shim_harness, tmp_path, driver_choice):
    options = {"media-type": "cardstock"}
    if driver_choice:
        options["BrMediaType"] = driver_choice
    result, _ = run_harness(shim_harness, tmp_path, options)
    settings = dict(line.split("=", 1) for line in result.stdout.splitlines()[1:])
    assert result.returncode == 0
    assert settings["media-type"] == "cardstock" and "MediaType" not in settings
    if driver_choice:
        assert settings["BrMediaType"] == "Thick"
    else:
        assert "BrMediaType" not in settings


def test_bridge_accepts_512_driver_and_alias_records_and_refuses_a_513th(shim_harness, tmp_path):
    options = {f"DriverOption{index}": "Selected" for index in range(512)}
    result, _ = run_harness(shim_harness, tmp_path, options)
    settings = dict(line.split("=", 1) for line in result.stdout.splitlines()[1:])
    assert result.returncode == 0 and all(settings[key] == value for key, value in options.items())
    content = (tmp_path / "options.bin").read_bytes() + b"\x00\x05\x00\x03ExtraYes"
    (tmp_path / "options.bin").unlink()
    result, receipt = run_harness(shim_harness, tmp_path, {}, corrupt=content)
    assert result.returncode == 1 and result.stdout == ""
    assert receipt.read_text() == "0\n"


@pytest.mark.parametrize("printer,title", [("epson", "FP-request source.docx"), ("brother", "FP-another source.docx"),
                                           ("brother", "FP-requestsuffix source.docx")])
def test_bridge_never_submits_to_a_different_queue_or_job(shim_harness, tmp_path, printer, title):
    result, receipt = run_harness(shim_harness, tmp_path, {}, printer=printer, title=title)
    assert result.returncode == 1 and result.stdout == ""
    assert receipt.read_text() == "0\n"


@pytest.mark.parametrize("content", [b"wrong\n", b"FPCO1\n\x00\x01", b"FPCO1\n\x00\x01\x00\x01\x00x",
                                     b"FPCO1\n\x00\x01\x00\x01x\x00"])
def test_malformed_private_settings_stop_before_the_real_submission(shim_harness, tmp_path, content):
    result, receipt = run_harness(shim_harness, tmp_path, {}, corrupt=content)
    assert result.returncode == 1 and result.stdout == ""
    assert receipt.read_text() == "0\n"
