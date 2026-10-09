"""Fonts a document asked for but the server lacks, and the administrator's own font files."""
import io
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from print_center import fonts as font_module
from print_center.conversion import UploadStore, preview_office
from print_center.fonts import FontError, FontStore
from tests.helpers import office_bytes, pdf_bytes, upload_file

# What fc-list reports: one line per font, with every name of that font.
INSTALLED = "DFKai-SB,標楷體\nTW-Kai\nNoto Sans CJK TC,Noto Sans CJK TC Regular\nCarlito\nLiberation Sans\nOpenSymbol\n"
TTF = b"\x00\x01\x00\x00" + b"\x00" * 252


class Tools:
    """Stand-ins for fc-list, fc-scan and fc-cache that record how they were called."""

    def __init__(self, families=INSTALLED, scanned="Family Kai,家庭楷書\n"):
        self.families, self.scanned, self.calls = families, scanned, []

    def __call__(self, command, **kwargs):
        self.calls.append((Path(command[0]).name, command[1:], kwargs))
        name = Path(command[0]).name
        if name == "fc-list":
            return SimpleNamespace(returncode=0, stdout=self.families)
        if name == "fc-scan":
            return SimpleNamespace(returncode=0 if self.scanned else 1, stdout=self.scanned)
        return SimpleNamespace(returncode=0, stdout="")


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(font_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    return Tools()


@pytest.fixture
def store(tmp_path, tools):
    return FontStore(tmp_path, run=tools)


def font_file(data=TTF, name="kai.ttf"):
    return SimpleNamespace(filename=name, stream=io.BytesIO(data))


def test_only_fonts_the_server_lacks_are_reported_in_document_order(store):
    report = [
        ["Segoe UI;Tahoma;DejaVu Sans", ["DejaVu Sans/Book"]],   # LibreOffice's own interface font
        ["華康少女文字W5", ["Noto Sans CJK TC/Regular"]],
        ["標楷體", ["DFKai-SB/Regular"]],                         # installed under its other name
        ["DFKai-SB", ["Noto Sans CJK TC/Regular"]],               # installed; one glyph came from elsewhere
        ["Calibri", ["Carlito/Regular"]],                         # same character widths
        ["Arial", ["Liberation Sans/Bold"]],
        ["Wingdings", ["OpenSymbol/Regular"]],                    # list bullets
        ["新細明體", ["Noto Serif CJK TC/Regular"]],              # our mapping's own choice
        ["微軟正黑體", ["DejaVu Sans/Book"]],                     # mapped, but its substitute is not installed
        ["華康少女文字W5", ["DejaVu Sans/Book"]],
        "not an entry", [None, []], ["", ["X/Regular"]], ["名" * 81, ["X/Regular"]], ["Bad", "X/Regular"],
    ]
    assert store.substitutions(report) == [
        {"name": "華康少女文字W5", "used": "Noto Sans CJK TC", "similar": False},
        {"name": "新細明體", "used": "Noto Serif CJK TC", "similar": True},
        {"name": "微軟正黑體", "used": "DejaVu Sans", "similar": False},
    ]
    assert store.substitutions(None) == [] and store.substitutions({"x": 1}) == []


def test_missing_fonts_are_counted_newest_first_and_drop_out_once_installed(store, tools):
    store.note([["華康少女文字W5", ["TW-Kai/Regular"]], ["新細明體", ["Noto Serif CJK TC/Regular"]]])
    time.sleep(0.01)
    store.note([["新細明體", ["Noto Serif CJK TC/Regular"]]])
    missing = store.missing()
    assert [(item["name"], item["count"], item["used"], item["similar"]) for item in missing] == [
        ("新細明體", 2, "Noto Serif CJK TC", True), ("華康少女文字W5", 1, "TW-Kai", False)]
    tools.families += "PMingLiU,新細明體\n"
    store._families = None
    assert [item["name"] for item in store.missing()] == ["華康少女文字W5"]


def test_another_name_of_the_same_font_is_not_a_substitution(store, tools):
    tools.families += "KaiTi,楷体\n"
    report = [
        ["BiauKai", ["DFKai-SB/Regular"]],    # the macOS name of 標楷體
        ["楷体_GB2312", ["KaiTi/Regular"]],    # the older edition of the same design
        ["华文楷体", ["KaiTi/Regular"]],       # a different design our mapping tries first
    ]
    assert store.substitutions(report) == [{"name": "华文楷体", "used": "KaiTi", "similar": True}]


def test_other_names_of_one_font_leave_the_missing_list_together(store, tools):
    tools.families = "TW-Kai\n"
    store.note([["BiauKai", ["TW-Kai/Regular"]], ["楷体_GB2312", ["TW-Kai/Regular"]]])
    assert {item["name"] for item in store.missing()} == {"BiauKai", "楷体_GB2312"}
    tools.families += "DFKai-SB,標楷體\n"
    store._families = None
    assert [item["name"] for item in store.missing()] == ["楷体_GB2312"]


def test_same_font_names_are_ones_the_mapping_knows_written_as_fontconfig_compares_them():
    text = (Path(__file__).resolve().parent.parent / "deploy" / "fonts" / "60-family-print-center.conf").read_text(encoding="utf-8")
    mapped = {font_module.normalize(name) for name in re.findall(r"<family>([^<]+)</family>", text)}
    for group in font_module.SAME_FONTS:
        assert all(name == font_module.normalize(name) and name in mapped for name in group), group


def test_missing_list_is_bounded_expires_and_survives_a_damaged_file(store):
    store.note([[f"Font {index}", ["TW-Kai/Regular"]] for index in range(25)])
    store.note([[f"Font {index}", ["TW-Kai/Regular"]] for index in range(25, 50)])
    assert len(store.missing()) == font_module.MISSING_LIMIT
    data = json.loads((store.root / "missing.json").read_text())
    data["fonts"][0]["last_seen"] = time.time() - (font_module.MISSING_DAYS + 1) * 86400
    (store.root / "missing.json").write_text(json.dumps(data))
    assert len(store.missing()) == font_module.MISSING_LIMIT - 1
    (store.root / "missing.json").write_text("{broken")
    assert store.missing() == []
    assert store.note([["Font X", ["TW-Kai/Regular"]]])[0]["name"] == "Font X"


def test_font_check_never_breaks_a_conversion(store, monkeypatch):
    monkeypatch.setattr(store, "families", lambda: 1 / 0)
    assert store.note([["華康少女文字W5", ["TW-Kai/Regular"]]]) == []


def test_without_uploaded_fonts_office_keeps_the_system_configuration(store):
    assert store.environment() == {}
    assert not (store.root / "fonts.conf").exists()


def test_uploaded_font_is_private_scanned_and_added_on_top_of_the_system_fonts(store, tools, tmp_path):
    result = store.save(font_file(name="../../evil/kai.ttf"))
    assert result["families"] == ["Family Kai", "家庭楷書"] and result["name"] == "kai.ttf"
    stored = store.files / f"{result['id']}.ttf"
    assert stored.read_bytes() == TTF and stored.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in store.files.iterdir()] == [stored.name]
    assert not list(store.root.glob("incoming-*"))
    config = (store.root / "fonts.conf").read_text()
    assert f"<dir>{store.files.resolve()}</dir>" in config
    assert '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>' in config
    assert config.index("<cachedir>") < config.index("<include")
    environment = {"FONTCONFIG_FILE": str((store.root / "fonts.conf").resolve())}
    assert store.environment() == environment
    scan, cache = (call for call in tools.calls if call[0] in {"fc-scan", "fc-cache"})
    assert scan[2]["timeout"] and "ADMIN_PASSWORD" not in scan[2]["env"]
    assert cache[1] == [str(store.files)] and cache[2]["env"]["FONTCONFIG_FILE"] == environment["FONTCONFIG_FILE"]
    assert store.items()[0]["id"] == result["id"]


@pytest.mark.parametrize("data,name,message", [
    (TTF, "kai.exe", "字體檔"),
    (b"MZ" + b"\x00" * 100, "kai.ttf", "不是字體檔"),
    (b"", "kai.ttf", "不是字體檔"),
])
def test_wrong_files_are_refused_without_leftovers(store, data, name, message):
    with pytest.raises(FontError, match=message):
        store.save(font_file(data, name))
    assert store.items() == [] and store.environment() == {}
    assert not list(store.root.glob("incoming-*")) if store.root.exists() else True


def test_unreadable_duplicate_and_oversized_fonts_are_refused(store, tools, monkeypatch):
    tools.scanned = ""
    with pytest.raises(FontError, match="無法讀取"):
        store.save(font_file())
    tools.scanned = "Family Kai\n"
    store.save(font_file())
    with pytest.raises(FontError, match="已經上傳過"):
        store.save(font_file())
    monkeypatch.setattr(font_module, "TOTAL_LIMIT_MB", 0)
    with pytest.raises(FontError, match="合計"):
        store.save(font_file(TTF + b"y"))
    monkeypatch.setattr(font_module, "FONT_LIMIT_MB", 0)
    with pytest.raises(FontError, match="超過"):
        store.save(font_file(TTF + b"x"))
    assert len(store.items()) == 1 and not list(store.root.glob("incoming-*"))


def test_deleting_the_last_font_returns_to_the_system_configuration(store):
    font_id = store.save(font_file())["id"]
    with pytest.raises(FontError):
        store.delete("../" + font_id)
    store.delete(font_id)
    assert store.items() == [] and store.environment() == {}
    assert not (store.root / "fonts.conf").exists() and not (store.root / "cache").exists()
    with pytest.raises(FontError):
        store.delete(font_id)


def test_preview_returns_the_font_report_and_uses_the_uploaded_fonts(tmp_path, monkeypatch):
    source, target = tmp_path / "input.docx", tmp_path / "preview.pdf"
    source.write_bytes(b"original office")
    seen = {}

    def launch(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        seen.update(env=kwargs["env"], fonts=request["fonts"])
        assert Path(request["fonts"]).parent == Path(command[-1]).parent
        Path(request["fonts"]).write_text(json.dumps([["標楷體", ["TW-Kai/Regular"]]]))
        target.write_bytes(pdf_bytes())
        return SimpleNamespace(returncode=0, wait=lambda timeout: 0)

    monkeypatch.setattr("print_center.conversion.subprocess.Popen", launch)
    report = preview_office(source, target, tmp_path, timeout=30, environment={"FONTCONFIG_FILE": "/data/fonts/fonts.conf"})
    assert report == [["標楷體", ["TW-Kai/Regular"]]]
    assert seen["env"]["FONTCONFIG_FILE"] == "/data/fonts/fonts.conf"
    assert not Path(seen["fonts"]).exists()


@pytest.mark.parametrize("written", ["{broken", json.dumps({"not": "a list"}), json.dumps([["x", []]] * 60000), None],
                         ids=["broken", "not-a-list", "oversized", "absent"])
def test_a_bad_or_missing_font_report_does_not_affect_the_preview(tmp_path, monkeypatch, written):
    source, target = tmp_path / "input.docx", tmp_path / "preview.pdf"
    source.write_bytes(b"original office")

    def launch(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        if written is not None:
            Path(request["fonts"]).write_text(written)
        target.write_bytes(pdf_bytes())
        return SimpleNamespace(returncode=0, wait=lambda timeout: 0)

    monkeypatch.setattr("print_center.conversion.subprocess.Popen", launch)
    assert preview_office(source, target, tmp_path, timeout=30) is None
    assert target.read_bytes().startswith(b"%PDF-")


def test_uploads_carry_a_short_font_note_and_feed_the_admin_list(tmp_path, store, monkeypatch):
    uploads = UploadStore({"DATA_DIR": str(tmp_path), "MAX_UPLOAD_MB": 1, "MAX_PENDING_UPLOADS": 2, "RETENTION_HOURS": 1,
                           "CONVERSION_TIMEOUT": 30, "MAX_DOCUMENT_PAGES": 10, "MAX_IMAGE_PIXELS": 1000,
                           "CONVERSION_SYNC": True}, fonts=store)

    def preview(source, target, directory):
        target.write_bytes(pdf_bytes())
        return [["華康少女文字W5", ["Noto Sans CJK TC/Regular"]], ["標楷體", ["DFKai-SB/Regular"]]]

    monkeypatch.setattr(uploads, "_office_preview", preview)
    upload_id = uploads.create(upload_file(office_bytes(), "homework.docx"))
    public = uploads.public(upload_id)
    assert public["status"] == "ready"
    assert public["substituted_fonts"] == [{"name": "華康少女文字W5", "similar": False}]
    assert [item["name"] for item in store.missing()] == ["華康少女文字W5"]
    pdf = uploads.public(uploads.create(upload_file(pdf_bytes(), "plain.pdf")))
    assert pdf["substituted_fonts"] == []
    uploads.executor.shutdown(wait=True)


@pytest.mark.skipif(not all(shutil.which(name) for name in ("fc-scan", "fc-list", "fc-cache")), reason="fontconfig tools missing")
def test_real_fontconfig_reads_the_upload_through_the_generated_file(tmp_path):
    sample = next((Path(path) for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                                           "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")
                   if Path(path).is_file()), None)
    if sample is None:
        pytest.skip("no sample font")
    store = FontStore(tmp_path)
    with sample.open("rb") as stream:
        result = store.save(SimpleNamespace(filename=sample.name, stream=stream))
    assert result["families"]
    listed = subprocess.run(["fc-list", "--format", "%{file}\n"], capture_output=True, text=True,
                            env={**os.environ, **store.environment()}, check=True).stdout
    assert str(store.files.resolve() / f"{result['id']}.ttf") in listed.splitlines()
    assert list((store.root / "cache").iterdir())
