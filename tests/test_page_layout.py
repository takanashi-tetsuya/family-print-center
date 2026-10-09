import io
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pymupdf
import pytest

from deploy.office_worker import apply_page_margins
from print_center.conversion import ConversionError
from print_center.cups_client import CupsError
from print_center.page_layout import (MM_TO_PT, PageLayoutError, fitted_pdf, media_dimensions,
                                      paper_for_options, validate_margins, validate_pdf_layout)
from tests.helpers import office_bytes, pdf_bytes
from tests.test_api import app, client, headers, uploaded, print_payload
from tests.test_native_office import printer


MARGINS = {"top_mm": 12, "right_mm": 15, "bottom_mm": 18, "left_mm": 20}


@pytest.mark.parametrize("value", [False, [], {}, {"top_mm": 10}, {**MARGINS, "extra": 0},
                                   {**MARGINS, "top_mm": True}, {**MARGINS, "top_mm": "10"},
                                   {**MARGINS, "top_mm": -1}, {**MARGINS, "top_mm": 101},
                                   {**MARGINS, "top_mm": float("inf")}, {**MARGINS, "top_mm": float("nan")},
                                   {**MARGINS, "top_mm": 10 ** 1000}])
def test_margins_reject_invalid_fields_and_values(value):
    with pytest.raises(PageLayoutError):
        validate_margins(value)


def test_auto_margins_and_mm_precision():
    assert validate_margins(None) is None
    assert validate_margins({**MARGINS, "left_mm": 25.401}) == {**MARGINS, "left_mm": 25.4}


@pytest.mark.parametrize("media,expected", [("iso_a4_210x297mm", (210, 297)), ("na_letter_8.5x11in", (215.9, 279.4)),
                                           ("Custom.100x148.5mm", (100, 148.5)), ("A5", (148, 210)),
                                           ("jis_b5_182x257mm", (182, 257)), ("Legal", (215.9, 355.6))])
def test_media_dimensions(media, expected):
    assert media_dimensions(media) == pytest.approx(expected)


def test_unknown_paper_cannot_silently_get_a_different_size():
    with pytest.raises(PageLayoutError, match="實際尺寸"):
        paper_for_options({"media": "PrinterSpecial"}, {})
    assert paper_for_options({}, {"default_media": "iso_a4_210x297mm"}) == (210, 297)
    assert paper_for_options({}, {}) is None
    assert media_dimensions("B5") is None


def test_pdf_margins_fit_vector_text_on_chosen_paper_preserving_original(tmp_path):
    source = tmp_path / "original.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=200, height=200)
        page.insert_text((1, 12), "VECTOR MARGIN TEST", fontsize=8)
        page.draw_rect(page.rect, color=(1, 0, 0), width=1)
        document.save(source)
    original_bytes = source.read_bytes()
    with fitted_pdf(source, MARGINS, (210, 297), "3") as target:
        assert target.is_file() and target != source
        with pymupdf.open(target) as document:
            page = document[0]
            assert page.rect.width == pytest.approx(210 * MM_TO_PT, abs=.001)
            assert page.rect.height == pytest.approx(297 * MM_TO_PT, abs=.001)
            assert "VECTOR MARGIN TEST" in page.get_text()
            assert page.get_images() == []
            drawing = page.get_drawings()[0]["rect"]
            assert drawing.x0 >= MARGINS["left_mm"] * MM_TO_PT - .01
            assert drawing.y0 >= MARGINS["top_mm"] * MM_TO_PT - .01
            assert drawing.x1 <= (210 - MARGINS["right_mm"]) * MM_TO_PT + .01
            assert drawing.y1 <= (297 - MARGINS["bottom_mm"]) * MM_TO_PT + .01
    assert not target.exists()
    assert source.read_bytes() == original_bytes
    assert list(tmp_path.glob("page-layout-*")) == []


def test_pdf_layout_uses_landscape_and_keeps_all_blank_pages(tmp_path):
    source = tmp_path / "empty.pdf"
    with pymupdf.open() as document:
        document.new_page(width=150, height=200)
        document.new_page(width=200, height=150)
        document.save(source)
    with fitted_pdf(source, MARGINS, (210, 297), "4") as target, pymupdf.open(target) as document:
        assert len(document) == 2
        for page in document:
            assert page.rect.width == pytest.approx(297 * MM_TO_PT, abs=.001)
            assert page.rect.height == pytest.approx(210 * MM_TO_PT, abs=.001)


def test_pdf_fitting_preserves_visible_form_field_appearance(tmp_path):
    source = tmp_path / "filled-form.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=300)
        widget = pymupdf.Widget()
        widget.field_name = "family_form"
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        widget.field_value = "FILLED CONTENT"
        widget.rect = pymupdf.Rect(20, 20, 220, 50)
        page.add_widget(widget)
        document.save(source)
    with fitted_pdf(source, MARGINS) as target, pymupdf.open(target) as document:
        assert "FILLED CONTENT" in document[0].get_text()
    with pymupdf.open(source) as document:
        assert next(document[0].widgets()).field_value == "FILLED CONTENT"


def test_margin_area_validation_and_cleanup_do_not_change_the_original(tmp_path):
    source = tmp_path / "tiny.pdf"
    with pymupdf.open() as document:
        document.new_page(width=40, height=40)
        document.save(source)
    original_bytes = source.read_bytes()
    with pytest.raises(PageLayoutError, match="至少 1 mm"):
        validate_pdf_layout(source, MARGINS)
    with pytest.raises(PageLayoutError):
        with fitted_pdf(source, MARGINS):
            pytest.fail("An impossible layout was yielded")
    assert source.read_bytes() == original_bytes and list(tmp_path.glob("page-layout-*")) == []


def test_pdf_api_applies_margins_once_retains_range_and_cleans_temporary_files(app, client, headers, uploaded, monkeypatch):
    store, cups = app.extensions["uploads"], app.extensions["cups_client"]
    seen = []
    original = store.directory(uploaded) / "document.pdf"
    original_bytes = original.read_bytes()

    def capture(printer, source, title, options):
        assert source != original
        assert options["page-ranges"] == "1,3" and options["copies"] == "2"
        assert options["print-scaling"] == "none"
        assert all("margin" not in name for name in options)
        with pymupdf.open(source) as document:
            assert len(document) == 3 and document[0].rect.width == pytest.approx(297 * MM_TO_PT, abs=.001)
        seen.append(source)
        return 42

    monkeypatch.setattr(cups, "print_file", capture)
    payload = print_payload(uploaded, margins=MARGINS, media="iso_a4_210x297mm", orientation="landscape", copies=2, page_ranges="1,3")
    assert client.post("/api/print", json=payload, headers=headers).json == {"job_id": 42}
    assert client.post("/api/print", json=payload, headers=headers).json == {"job_id": 42, "reused": True}
    assert len(seen) == 1 and not seen[0].exists()
    assert original.read_bytes() == original_bytes


@pytest.mark.parametrize("phase", ["validation", "fitting"])
def test_pdf_preparation_keeps_upload_protected_without_blocking_other_uploads(app, client, headers, uploaded, monkeypatch, phase):
    store = app.extensions["uploads"]
    started, release = threading.Event(), threading.Event()
    other = client.post("/api/uploads", data={"file": (io.BytesIO(pdf_bytes()), "other.pdf")}, headers=headers).json["id"]

    def gate():
        assert uploaded in store.active_prints
        started.set()
        assert release.wait(timeout=5)

    @contextmanager
    def prepare(*args, **kwargs):
        gate()
        with fitted_pdf(*args, **kwargs) as target:
            yield target

    def validate(*args, **kwargs):
        gate()
        return validate_pdf_layout(*args, **kwargs)

    def first_request():
        browser = app.test_client()
        token = browser.get("/api/status").json["csrf_token"]
        return browser.post("/api/print", json=print_payload(uploaded, margins=MARGINS), headers={"X-CSRF-Token": token})

    monkeypatch.setattr("print_center.app." + ("fitted_pdf" if phase == "fitting" else "validate_pdf_layout"), prepare if phase == "fitting" else validate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_request)
        try:
            assert started.wait(timeout=2)
            assert client.get(f"/api/uploads/{uploaded}").status_code == 200
            assert client.get(f"/api/uploads/{other}").status_code == 200
            assert client.delete(f"/api/uploads/{other}", headers=headers).status_code == 200
            assert client.delete(f"/api/uploads/{uploaded}", headers=headers).status_code == 400
            with pytest.raises(ConversionError, match="文件正在準備或提交打印"):
                store.delete(uploaded)
            duplicate = client.post("/api/print", json=print_payload(uploaded, margins=MARGINS), headers=headers)
            assert duplicate.status_code == 409 and "重複打印" in duplicate.json["error"]
            competing = client.post("/api/print", json=print_payload(uploaded, request_id="another_request_123456"), headers=headers)
            assert competing.status_code == 409 and "另一個任務" in competing.json["error"]
            store.cleanup()
            assert store.get(uploaded)["requests"] == {"request_unique_123456": None}
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 200
    assert uploaded not in store.active_prints


@pytest.mark.parametrize("error,status", [(ValueError("failed to prepare PDF"), 400), (OSError("disk is full"), 500)])
def test_pdf_preparation_failure_is_safe_to_retry_without_any_cups_submission(app, client, headers, uploaded, monkeypatch, error, status):
    store = app.extensions["uploads"]
    source = store.directory(uploaded) / "document.pdf"
    before = source.read_bytes()
    save, attempts = pymupdf.Document.save, []

    def fail_once(document, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise error
        return save(document, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Document, "save", fail_once)
    payload = print_payload(uploaded, margins=MARGINS)
    assert client.post("/api/print", json=payload, headers=headers).status_code == status
    assert app.extensions["cups_client"].prints == []
    assert store.get(uploaded)["requests"] == {} and store.active_prints == set()
    assert source.read_bytes() == before
    assert list(store.directory(uploaded).glob("page-layout-*")) == []
    assert client.post("/api/print", json=payload, headers=headers).json == {"job_id": 42}
    assert len(app.extensions["cups_client"].prints) == 1


def test_ambiguous_cups_failure_after_pdf_preparation_keeps_duplicate_guard(app, client, headers, uploaded, monkeypatch):
    store = app.extensions["uploads"]
    print_call = Mock(side_effect=CupsError("提交結果尚未確認"))
    monkeypatch.setattr(app.extensions["cups_client"], "print_file", print_call)
    payload = print_payload(uploaded, margins=MARGINS)
    assert client.post("/api/print", json=payload, headers=headers).status_code == 503
    assert store.get(uploaded)["requests"] == {payload["request_id"]: None}
    assert store.active_prints == set()
    retry = client.post("/api/print", json=payload, headers=headers)
    assert retry.status_code == 409 and "重複打印" in retry.json["error"]
    print_call.assert_called_once()
    assert list(store.directory(uploaded).glob("page-layout-*")) == []


@pytest.mark.parametrize("extra", [{"margins": {**MARGINS, "left_mm": "15"}},
                                   {"margins": {**MARGINS, "left_mm": 101}},
                                   {"margins": MARGINS, "advanced_options": {"print-scaling": "fill"}}])
def test_invalid_margin_request_does_not_reserve_or_submit(app, client, headers, uploaded, extra):
    payload = print_payload(uploaded, **extra)
    assert client.post("/api/print", json=payload, headers=headers).status_code == 400
    assert app.extensions["cups_client"].prints == []
    assert payload["request_id"] not in app.extensions["uploads"].get(uploaded)["requests"]


def test_custom_paper_can_be_too_small_for_selected_margins(app, client, headers, uploaded):
    app.extensions["cups_client"].printer["capabilities"]["custom_media"] = {
        "min_width": 30, "max_width": 300, "min_height": 30, "max_height": 400}
    payload = print_payload(uploaded, margins=MARGINS, media="custom", custom_media={"width_mm": 35, "height_mm": 40})
    response = client.post("/api/print", json=payload, headers=headers)
    assert response.status_code == 400 and "內容區域" in response.json["error"]
    assert payload["request_id"] not in app.extensions["uploads"].get(uploaded)["requests"]


def native_upload(client, headers, app, monkeypatch, extension):
    monkeypatch.setattr(app.extensions["uploads"], "capabilities", lambda: {"office": True})
    monkeypatch.setattr(app.extensions["uploads"], "_office_preview", lambda source, target, directory: target.write_bytes(pdf_bytes()))
    return client.post("/api/uploads", data={"file": (io.BytesIO(office_bytes(extension)), "document." + extension)}, headers=headers).json["id"]


def test_powerpoint_margin_rejection_is_explicit_and_happens_before_reservation(app, client, headers, monkeypatch):
    upload_id = native_upload(client, headers, app, monkeypatch, "pptx")
    printing = Mock(return_value=80)
    monkeypatch.setattr(app.extensions["native_printer"], "print_file", printing)
    payload = print_payload(upload_id, margins=MARGINS)
    response = client.post("/api/print", json=payload, headers=headers)
    assert response.status_code == 400 and "PowerPoint" in response.json["error"]
    assert app.extensions["uploads"].get(upload_id)["requests"] == {}
    printing.assert_not_called()
    payload.pop("margins")
    assert client.post("/api/print", json=payload, headers=headers).json == {"job_id": 80}


@pytest.mark.parametrize("extension", ["docx", "xlsx"])
def test_office_api_keeps_original_native_submission_and_separate_margins(app, client, headers, monkeypatch, extension):
    upload_id = native_upload(client, headers, app, monkeypatch, extension)
    source_bytes = (app.extensions["uploads"].directory(upload_id) / ("input." + extension)).read_bytes()

    def capture(printer, source, title, options, request_id, *, margins, paper):
        assert source.suffix == "." + extension and source.read_bytes() == source_bytes
        assert margins == MARGINS and paper == (210, 297)
        assert not any("margin" in name for name in options)
        return 80

    monkeypatch.setattr(app.extensions["native_printer"], "print_file", capture)
    payload = print_payload(upload_id, margins=MARGINS, media="iso_a4_210x297mm")
    assert client.post("/api/print", json=payload, headers=headers).json == {"job_id": 80}
    assert app.extensions["cups_client"].prints == []


def test_office_auto_paper_margins_preserve_original_size_without_using_preview(app, client, headers, monkeypatch):
    upload_id = native_upload(client, headers, app, monkeypatch, "docx")
    store = app.extensions["uploads"]
    (store.directory(upload_id) / "document.pdf").unlink()
    app.extensions["cups_client"].printer["capabilities"]["default_media"] = "iso_a5_148x210mm"

    def capture(printer, source, title, options, request_id, *, margins, paper):
        assert paper is None and margins == MARGINS
        assert source.suffix == ".docx" and source.is_file()
        return 80

    monkeypatch.setattr(app.extensions["native_printer"], "print_file", capture)
    payload = print_payload(upload_id, margins=MARGINS, media="auto")
    assert client.post("/api/print", json=payload, headers=headers).json == {"job_id": 80}


def test_known_unsubmitted_uno_layout_error_can_be_corrected_using_same_request_id(app, client, headers, monkeypatch):
    upload_id = native_upload(client, headers, app, monkeypatch, "docx")
    printer = Mock(side_effect=[PageLayoutError("頁邊距過大。"), 80])
    monkeypatch.setattr(app.extensions["native_printer"], "print_file", printer)
    payload = print_payload(upload_id, margins=MARGINS)
    assert client.post("/api/print", json=payload, headers=headers).status_code == 400
    assert app.extensions["uploads"].get(upload_id)["requests"] == {}
    assert app.extensions["uploads"].active_prints == set()
    assert client.post("/api/print", json=payload, headers=headers).json == {"job_id": 80}


class PageStyle:
    def __init__(self, width=21000, height=29700, follow="", used=True):
        self.values = {"Width": width, "Height": height, "FollowStyle": follow, "TopMargin": 2000,
                       "RightMargin": 2000, "BottomMargin": 2000, "LeftMargin": 2000}
        self.used = used
        self.writes = []

    def getPropertyValue(self, name):
        return self.values[name]

    def setPropertyValue(self, name, value):
        self.writes.append((name, value))
        self.values[name] = value

    def isInUse(self):
        return self.used


def style_document(styles, service="com.sun.star.text.TextDocument", sheets=None):
    page_styles = SimpleNamespace(getByName=lambda name: styles[name], getElementNames=lambda: list(styles))
    families = SimpleNamespace(getByName=lambda name: page_styles)
    document = Mock()
    document.supportsService.side_effect = lambda name: name == service
    document.getStyleFamilies.return_value = families
    if sheets is not None:
        document.getSheets.return_value = SimpleNamespace(getCount=lambda: len(sheets),
                getByIndex=lambda index: SimpleNamespace(getPropertyValue=lambda name: sheets[index]))
    return document


def test_writer_margins_change_used_and_following_page_styles_without_export():
    styles = {"First": PageStyle(follow="Rest"), "Rest": PageStyle(used=False), "Unused": PageStyle(width=1000, used=False)}
    document = style_document(styles)
    apply_page_margins(document, MARGINS, (210, 297), "4")
    for name in ("First", "Rest"):
        assert styles[name].values["TopMargin"] == 1200 and styles[name].values["LeftMargin"] == 2000
        assert styles[name].values["Width"] == 29700 and styles[name].values["Height"] == 21000
    assert styles["Unused"].writes == []
    document.storeToURL.assert_not_called()
    document.storeAsURL.assert_not_called()
    document.print.assert_not_called()


def test_calc_applies_all_sheet_page_styles_and_validates_before_modifying_any():
    styles = {"Normal": PageStyle(), "Small": PageStyle(width=3500, height=4000)}
    document = style_document(styles, "com.sun.star.sheet.SpreadsheetDocument", ["Normal", "Small"])
    with pytest.raises(PageLayoutError, match="內容區域"):
        apply_page_margins(document, MARGINS)
    assert all(not style.writes for style in styles.values())
    apply_page_margins(document, MARGINS, (210, 297))
    assert all(style.values["BottomMargin"] == 1800 for style in styles.values())


def test_auto_office_margins_leave_all_page_properties_alone():
    document = Mock()
    apply_page_margins(document, None)
    document.supportsService.assert_not_called()
    document.getStyleFamilies.assert_not_called()


def test_native_margin_override_with_auto_paper_keeps_original_letter_dimensions():
    style = PageStyle(width=21590, height=27940)
    document = style_document({"Letter": style})
    apply_page_margins(document, MARGINS)
    assert style.values["Width"] == 21590 and style.values["Height"] == 27940
    assert {name for name, _ in style.writes} == {"TopMargin", "RightMargin", "BottomMargin", "LeftMargin"}


def test_native_printer_passes_layout_outside_cups_options_and_decodes_known_errors(printer, tmp_path, monkeypatch):
    source = tmp_path / "input.docx"
    source.write_bytes(b"original office document")

    def launch(command, **kwargs):
        import json
        request = json.loads(Path(command[-1]).read_text())
        assert request["margins"] == MARGINS and request["paper"] == [210, 297]
        assert "margin" not in Path(kwargs["env"]["FPC_CUPS_OPTIONS_FILE"]).read_bytes().decode("latin1")
        return SimpleNamespace(returncode=2, communicate=lambda timeout: (b'{"layout_error":"margins too large"}', b""))

    monkeypatch.setattr("print_center.native_office.subprocess.Popen", launch)
    with pytest.raises(PageLayoutError, match="margins too large"):
        printer.print_file("brother", source, "original", {"copies": "1"}, "request_unique_123456", margins=MARGINS, paper=(210, 297))
    assert source.read_bytes() == b"original office document"
    assert list(tmp_path.glob("office-print-*")) == []
