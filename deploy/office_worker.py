"""Run with Debian's /usr/bin/python3 (python3-uno), in a private process group.

Preview mode exports a PDF. Print mode submits the original through LibreOffice to CUPS.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

# This module uses only the standard library; system python3-uno does not need
# the web application's virtualenv just to get the correct headless backend.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from print_center.office_runtime import office_environment
from print_center.page_layout import (PageLayoutError, oriented_paper,
                                     validate_area, validate_margins)


def apply_page_margins(document, margins, paper=None, orientation=None):
    """Change the opened original's page styles in memory; never save it.

    Writer and Calc use actual layout margins, so text and tables paginate
    again. Impress border properties alone do not move existing slide objects
    reliably; refuse an override rather than pretend that it was applied.
    """
    margins = validate_margins(margins)
    if margins is None:
        return
    if document.supportsService("com.sun.star.presentation.PresentationDocument"):
        raise PageLayoutError("PowerPoint 使用原始版面；調整頁邊距時請先另存為 PDF 再上傳。")
    writer = document.supportsService("com.sun.star.text.TextDocument")
    calc = document.supportsService("com.sun.star.sheet.SpreadsheetDocument")
    if not writer and not calc:
        raise PageLayoutError("這個文件格式無法直接調整頁邊距，請先另存為 PDF。")
    try:
        page_styles = document.getStyleFamilies().getByName("PageStyles")
        if calc:
            sheets = document.getSheets()
            names = {sheets.getByIndex(index).getPropertyValue("PageStyle") for index in range(sheets.getCount())}
        else:
            names = {name for name in page_styles.getElementNames() if page_styles.getByName(name).isInUse()}
            if not names:
                raise PageLayoutError("無法確認文件使用的頁面樣式，請先另存為 PDF。")
            # A first-page style can point at a different style for later pages.
            pending = list(names)
            while pending:
                style = page_styles.getByName(pending.pop())
                follow = style.getPropertyValue("FollowStyle")
                if follow and follow not in names:
                    names.add(follow)
                    pending.append(follow)
        planned = []
        for name in names:
            style = page_styles.getByName(name)
            original = (style.getPropertyValue("Width") / 100, style.getPropertyValue("Height") / 100)
            dimensions = oriented_paper(paper, orientation, original)
            validate_area(*dimensions, margins)
            planned.append((style, dimensions))
        # Validate every used style before changing any. A second section or
        # spreadsheet sheet must not quietly ignore impossible margins.
        for style, dimensions in planned:
            if paper is not None or orientation:
                style.setPropertyValue("Width", round(dimensions[0] * 100))
                style.setPropertyValue("Height", round(dimensions[1] * 100))
                style.setPropertyValue("IsLandscape", dimensions[0] > dimensions[1])
            for property_name, key in (("TopMargin", "top_mm"), ("RightMargin", "right_mm"),
                                       ("BottomMargin", "bottom_mm"), ("LeftMargin", "left_mm")):
                style.setPropertyValue(property_name, round(margins[key] * 100))
    except PageLayoutError:
        raise
    except Exception as exc:
        raise PageLayoutError("LibreOffice 無法套用這份文件的頁邊距，請先另存為 PDF。") from exc


def write_font_report(toolkit, path):
    """Font names only, never document text; a failure here leaves the preview intact."""
    try:
        items = toolkit.finishTrackingFontMappingUse()
        report = [[str(item.originalFont)[:200], [str(name)[:200] for name in list(item.usedFonts)[:10]]]
                  for item in list(items)[:300]]
        path.write_text(json.dumps(report, ensure_ascii=False))
    except Exception:
        pass


def run(request):
    import uno
    from com.sun.star.beans import PropertyValue

    def prop(name, value):
        property_value = PropertyValue()
        property_value.Name = name
        property_value.Value = value
        return property_value

    context = uno.getComponentContext()
    resolver = context.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", context)
    pipe_name = "familyprint_" + uuid.uuid4().hex
    profile_url = Path(request["profile"]).resolve().as_uri()
    environment = office_environment()
    bridge = environment.pop("FPC_CUPS_SHIM", None)
    if request.get("mode") != "preview" and request.get("cups_receipt"):
        if not bridge or not Path(bridge).is_file():
            raise RuntimeError("Native CUPS options bridge is unavailable")
        # Only LibreOffice loads the bridge; neither this Python worker nor the
        # application's normal CUPS connections inherit LD_PRELOAD from us.
        environment["LD_PRELOAD"] = bridge
    process = subprocess.Popen([shutil.which("libreoffice") or shutil.which("soffice"),
                                f"-env:UserInstallation={profile_url}", "--headless", "--nologo", "--nodefault", "--norestore",
                                f"--accept=pipe,name={pipe_name};urp;StarOffice.ComponentContext"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
    desktop = document = None
    try:
        remote = None
        for attempt in range(100):
            if process.poll() is not None:
                raise RuntimeError("LibreOffice stopped before connecting")
            try:
                remote = resolver.resolve(f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext")
                break
            except Exception:
                time.sleep(0.1)
        if remote is None:
            raise RuntimeError("LibreOffice connection timed out")
        desktop = remote.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", remote)
        toolkit = None
        if request.get("mode") == "preview" and request.get("fonts"):
            # Records each font the document asks for and the font actually drawn (LibreOffice 7.3+).
            try:
                toolkit = remote.ServiceManager.createInstanceWithContext("com.sun.star.awt.Toolkit", remote)
                toolkit.startTrackingFontMappingUse()
            except Exception:
                toolkit = None
        source = Path(request["source"])
        load_options = [prop("Hidden", True), prop("ReadOnly", True), prop("Silent", True),
                        prop("MacroExecutionMode", uno.getConstantByName("com.sun.star.document.MacroExecMode.NEVER_EXECUTE")),
                        prop("UpdateDocMode", uno.getConstantByName("com.sun.star.document.UpdateDocMode.NO_UPDATE"))]
        if source.suffix.lower() in {".txt", ".md"}:
            load_options += [prop("FilterName", "Text (encoded)"), prop("FilterOptions", "UTF8,LF,,,")]
        if source.suffix.lower() in {".csv", ".tsv"}:
            separator = "44" if source.suffix.lower() == ".csv" else "9"
            load_options += [prop("FilterName", "Text - txt - csv (StarCalc)"), prop("FilterOptions", f"{separator},34,76,1")]
        document = desktop.loadComponentFromURL(source.resolve().as_uri(), "_blank", 0, tuple(load_options))
        if document is None:
            raise RuntimeError("Document cannot be opened")
        if request.get("margins") is not None:
            apply_page_margins(document, request["margins"], request.get("paper"), request.get("options", {}).get("orientation-requested"))
        if request.get("mode") == "preview":
            if document.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
                export_filter = "calc_pdf_Export"
            elif document.supportsService("com.sun.star.presentation.PresentationDocument"):
                export_filter = "impress_pdf_Export"
            elif document.supportsService("com.sun.star.text.TextDocument"):
                export_filter = "writer_pdf_Export"
            else:
                raise RuntimeError("Document has no supported preview filter")
            document.storeToURL(Path(request["target"]).resolve().as_uri(),
                                (prop("FilterName", export_filter), prop("Overwrite", True)))
            if toolkit is not None:
                write_font_report(toolkit, Path(request["fonts"]))
            return {"preview": True}
        document.setPrinter((prop("Name", request["printer"]),))
        actual_printer = {item.Name: item.Value for item in document.getPrinter()}
        if actual_printer.get("Name") != request["printer"]:
            raise RuntimeError("Requested printer is unavailable")
        # IsValid is not part of the standard PrinterDescriptor and Debian's
        # LibreOffice omits it. A reported false is meaningful; absence is not.
        if actual_printer.get("IsValid") is False:
            raise RuntimeError("Requested printer is invalid")
        options = request["options"]
        print_options = [prop("CopyCount", uno.Any("short", int(options["copies"]))),
                         prop("Wait", True), prop("SinglePrintJobs", False), prop("JobName", request["job_name"])]
        if options.get("page-ranges"):
            print_options.append(prop("Pages", options["page-ranges"].replace(",", ";")))
        document.print(tuple(print_options))
        if request.get("cups_receipt"):
            receipt = Path(request["cups_receipt"])
            job_id = receipt.read_text().strip() if receipt.is_file() else ""
            if not job_id.isdigit() or not 0 < int(job_id) <= 2147483647:
                raise RuntimeError("Native CUPS settings were not submitted")
            return {"submitted": True, "job_id": int(job_id)}
        return {"submitted": True}
    finally:
        if document is not None:
            try:
                document.close(True)
            except Exception:
                try:
                    document.dispose()
                except Exception:
                    # A failed document close must not skip desktop/process
                    # cleanup or turn an already submitted job into a retry.
                    pass
        if desktop is not None:
            try:
                desktop.terminate()
            except Exception:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()


if __name__ == "__main__":
    try:
        result = run(json.loads(Path(sys.argv[1]).read_text()))
        print(json.dumps(result))
    except PageLayoutError as error:
        # A known pre-submission failure is safe to fix and resubmit; do not
        # confuse it with an ambiguous error after the CUPS call.
        print(json.dumps({"layout_error": str(error)}, ensure_ascii=False))
        sys.exit(2)
    except Exception:
        # Detailed document data is intentionally not returned to the web application.
        print("Native Office printing failed", file=sys.stderr)
        sys.exit(1)
