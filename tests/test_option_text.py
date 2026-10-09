from print_center.app import validate_options
from print_center.option_text import choice_label, option_label, print_presets


def select(key, choices, label=None, default=""):
    return {"key": key, "label": label or key, "group": "quality", "type": "select", "default": default,
            "choices": [{"value": value, "label": text} for value, text in choices]}


def test_driver_words_become_everyday_chinese():
    assert option_label("BrMediaType", "MediaType") == "紙張種類"
    assert option_label("TonerSaveMode", "Toner Save") == "省碳粉"
    assert option_label("VendorThing", "Vendor Thing") == "Vendor Thing"
    assert option_label("Quality", "列印品質（驅動）") == "列印品質（驅動）"
    assert choice_label("MPTRAY", "MP Tray") == "多用途紙匣"
    assert choice_label("TRAY2", "Tray2") == "紙匣 2"
    assert choice_label("PLAIN", "Plain Paper") == "普通紙"
    assert choice_label("ENVTHICK", "Env. Thick") == "厚信封"
    assert choice_label("600dpi", "600dpi") == "600 DPI"
    assert choice_label("2400x600dpi", "") == "2400 × 600 DPI"
    assert choice_label("ON", "On") == "開啟"


def test_driver_description_is_kept_when_it_is_not_a_known_word():
    # The internal value "normal" must not override a driver's own unusual description.
    assert choice_label("normal", "Choice normal") == "Choice normal"
    assert choice_label("HQ1200", "HQ1200dpi") == "HQ1200dpi"


def test_colour_inkjet_gets_economy_fine_and_photo_modes():
    options = [
        select("print-quality", [("3", "草稿"), ("4", "標準"), ("5", "最佳")]),
        select("media-type", [("stationery", "普通紙"), ("photographic-glossy", "光面相片紙")]),
        select("print-content-optimize", [("auto", "自動"), ("photo", "照片")]),
    ]
    presets = {preset["id"]: preset for preset in print_presets(options, ["monochrome", "color"])}
    assert list(presets) == ["standard", "economy", "fine", "photo"]
    assert presets["standard"]["basic"] == {} and presets["standard"]["advanced"] == {}
    assert presets["economy"]["basic"] == {"color_mode": "monochrome"}
    assert presets["economy"]["advanced"] == {"print-quality": "3"}
    assert presets["fine"]["advanced"] == {"print-quality": "5"}
    assert presets["photo"]["basic"] == {"color_mode": "color"}
    assert presets["photo"]["advanced"] == {"media-type": "photographic-glossy", "print-quality": "5",
                                            "print-content-optimize": "photo"}
    assert presets["economy"]["summary"] == "黑白、草稿"


def test_mono_laser_uses_toner_save_and_highest_square_resolution():
    options = [
        select("printer-resolution", [("300dpi", "300 DPI"), ("600dpi", "600 DPI"), ("2400x600dpi", "2400 × 600 DPI"),
                                      ("1200dpi", "1200 DPI")]),
        select("TonerSaveMode", [("OFF", "關閉"), ("ON", "開啟")], "省碳粉"),
        select("BrMediaType", [("PLAIN", "普通紙"), ("THICK", "厚紙")], "紙張種類"),
    ]
    presets = {preset["id"]: preset for preset in print_presets(options, ["monochrome"])}
    assert list(presets) == ["standard", "economy", "fine"]
    assert presets["economy"]["basic"] == {} and presets["economy"]["advanced"] == {"TonerSaveMode": "ON"}
    assert presets["economy"]["summary"] == "省碳粉：開啟"
    assert presets["fine"]["advanced"] == {"printer-resolution": "1200dpi"}
    assert presets["fine"]["summary"] == "最高清晰度"


def test_driver_quality_names_are_used_when_ipp_quality_is_missing():
    options = [select("StpQuality", [("Draft", "Draft"), ("Standard", "Standard"), ("BestPhoto", "Best Photo")]),
               select("MediaType", [("Plain", "普通紙"), ("GlossyPhoto", "光面相片紙")])]
    presets = {preset["id"]: preset for preset in print_presets(options, ["monochrome", "color"])}
    assert presets["economy"]["advanced"] == {"StpQuality": "Draft"}
    assert presets["photo"]["advanced"] == {"MediaType": "GlossyPhoto", "StpQuality": "BestPhoto"}


def test_printer_without_adjustable_quality_gets_no_mode_buttons():
    assert print_presets([select("number-up", [("1", "1 頁"), ("2", "2 頁")])], ["monochrome"]) == []


def test_every_mode_is_accepted_by_the_print_validator():
    options = [
        select("print-quality", [("3", "草稿"), ("5", "最佳")]),
        select("media-type", [("photographic-glossy", "光面相片紙")]),
        select("TonerSaveMode", [("ON", "開啟")]),
    ]
    printer = {"capabilities": {"sides": ["one-sided"], "color_modes": ["monochrome", "color"], "media": [],
                                "print_options": options, "option_aliases": {}}}
    for preset in print_presets(options, ["monochrome", "color"]):
        payload = {"copies": 1, "advanced_options": preset["advanced"], **preset["basic"]}
        result = validate_options(payload, printer, 3)
        assert all(result[key] == value for key, value in preset["advanced"].items())
