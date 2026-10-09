"""Validated page margins shared by the web application and the UNO worker.

Only the PDF fitting function imports PyMuPDF: Debian's python3-uno worker
can use the validation and page-size helpers with the standard library.
"""
from contextlib import contextmanager
import math
from pathlib import Path
import re
import tempfile


MM_TO_PT = 72 / 25.4
MARGIN_FIELDS = ("top_mm", "right_mm", "bottom_mm", "left_mm")
PRESENTATION_EXTENSIONS = {"ppt", "pptx", "pptm", "pps", "ppsx", "odp"}


class PageLayoutError(ValueError):
    """A layout was rejected before any print submission happened."""


def validate_margins(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(MARGIN_FIELDS):
        raise PageLayoutError("頁邊距必須同時提供上、下、左、右四個毫米數值。")
    if any(type(value[key]) not in {int, float} or not 0 <= value[key] <= 100
           or not math.isfinite(value[key]) for key in MARGIN_FIELDS):
        raise PageLayoutError("每個頁邊距必須為 0 到 100 mm 的有限數值。")
    return {key: round(value[key], 2) for key in MARGIN_FIELDS}


def media_dimensions(value):
    """IPP PWG names, CUPS custom sizes and common PPD paper keywords, in mm."""
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:_|^Custom\.)([\d.]+)x([\d.]+)(mm|in)(?:\.borderless)?$", value, re.I)
    if match:
        try:
            dimensions = tuple(float(number) * (25.4 if match[3].lower() == "in" else 1)
                               for number in match.group(1, 2))
        except ValueError:
            return None
        if all(math.isfinite(number) and 0 < number <= 5000 for number in dimensions):
            return dimensions
        return None
    sizes = {
        "a0": (841, 1189), "a1": (594, 841), "a2": (420, 594),
        "a3": (297, 420), "a4": (210, 297), "a5": (148, 210), "a6": (105, 148),
        "a7": (74, 105), "a8": (52, 74), "a9": (37, 52), "a10": (26, 37),
        "isob4": (250, 353), "isob5": (176, 250), "isob6": (125, 176),
        "jisb4": (257, 364), "jisb5": (182, 257), "jisb6": (128, 182),
        "letter": (215.9, 279.4), "legal": (215.9, 355.6), "executive": (184.15, 266.7),
        "ledger": (431.8, 279.4), "tabloid": (279.4, 431.8),
        "statement": (139.7, 215.9), "folio": (215.9, 330.2),
        "envelop10": (104.775, 241.3), "com10": (104.775, 241.3),
        "envdl": (110, 220), "dl": (110, 220), "envc5": (162, 229), "c5": (162, 229),
        "envc6": (114, 162), "c6": (114, 162), "monarch": (98.425, 190.5),
        "number10": (104.775, 241.3), "env10": (104.775, 241.3),
        "hagaki": (100, 148), "postcard": (100, 148), "oufuku": (148, 200),
        "l": (89, 127), "photol": (89, 127), "2l": (127, 178),
        "4x6": (101.6, 152.4), "index4x6": (101.6, 152.4), "5x7": (127, 177.8),
        "5x8": (127, 203.2), "index5x8": (127, 203.2), "8x10": (203.2, 254),
        "you4": (105, 235), "chou3": (120, 235), "chou4": (90, 205), "kaku2": (240, 332),
    }
    name = re.sub(r"[._-]?(?:borderless|fullbleed)", "", value.lower())
    return sizes.get(re.sub(r"[-_\s]", "", name))


def paper_for_options(options, capabilities):
    media = next((options[key] for key in ("media", "PageSize", "PageRegion") if options.get(key)), None)
    if media is None:
        media = capabilities.get("default_media")
    dimensions = media_dimensions(media)
    if media and dimensions is None:
        raise PageLayoutError("無法確認這個紙張選項的實際尺寸；調整頁邊距時請選擇 A4 等明確尺寸。")
    return dimensions


def oriented_paper(paper, orientation, original):
    dimensions = paper or original
    width, height = dimensions
    landscape = (orientation in {"4", "5"} if orientation else original[0] > original[1])
    if orientation in {"3", "6"}:
        landscape = False
    return (max(width, height), min(width, height)) if landscape else (min(width, height), max(width, height))


def validate_area(width_mm, height_mm, margins):
    if width_mm - margins["left_mm"] - margins["right_mm"] < 1 or height_mm - margins["top_mm"] - margins["bottom_mm"] < 1:
        raise PageLayoutError("頁邊距過大，紙張必須保留至少 1 mm 寬、高的內容區域。")


def validate_pdf_layout(source, margins, paper=None, orientation=None):
    import pymupdf
    with pymupdf.open(source) as document:
        for page in document:
            size = oriented_paper(paper, orientation, (page.rect.width / MM_TO_PT, page.rect.height / MM_TO_PT))
            validate_area(*size, margins)


@contextmanager
def fitted_pdf(source, margins, paper=None, orientation=None):
    """Fit vector pages into real paper margins; keep original bytes intact."""
    import pymupdf
    source = Path(source)
    with tempfile.TemporaryDirectory(prefix="page-layout-", dir=source.parent) as temporary:
        target = Path(temporary) / "document.pdf"
        with pymupdf.open(source) as original, pymupdf.open() as output:
            # show_pdf_page does not copy annotations/widgets. Flatten their
            # visible appearance in memory so printed form contents survive.
            original.bake(annots=True, widgets=True)
            for index, page in enumerate(original):
                width, height = oriented_paper(paper, orientation, (page.rect.width / MM_TO_PT, page.rect.height / MM_TO_PT))
                validate_area(width, height, margins)
                result = output.new_page(width=width * MM_TO_PT, height=height * MM_TO_PT)
                area = pymupdf.Rect(margins["left_mm"] * MM_TO_PT, margins["top_mm"] * MM_TO_PT,
                                    (width - margins["right_mm"]) * MM_TO_PT,
                                    (height - margins["bottom_mm"]) * MM_TO_PT)
                if page.get_contents():
                    result.show_pdf_page(area, original, index, keep_proportion=True)
            output.save(target, garbage=3, deflate=True)
        yield target
