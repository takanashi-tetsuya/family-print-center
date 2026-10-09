"""Font bundle: pinned downloads, build-time unpacking and the name mappings."""
import hashlib
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from print_center.conversion import PROFILE_XML

PROJECT = Path(__file__).resolve().parents[1]
FONTS = PROJECT / "deploy" / "fonts"


def sources():
    for line in (FONTS / "sources.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            yield line.split()


def test_every_download_is_pinned_to_a_checksum_and_https():
    entries = list(sources())
    assert len(entries) >= 14
    for entry in entries:
        assert len(entry) == 3, entry
        checksum, name, url = entry
        assert re.fullmatch(r"[0-9a-f]{64}", checksum), name
        assert "/" not in name and url.startswith("https://") and url.endswith("/" + name)
    assert len({name for _, name, _ in entries}) == len(entries)


def aliases():
    root = ElementTree.parse(FONTS / "60-family-print-center.conf").getroot()
    result = {}
    for alias in root.iter("alias"):
        assert alias.get("binding") == "same"
        # <accept> keeps the requested family first, so a real font in private/ wins.
        assert alias.find("prefer") is None and alias.find("default") is None
        result[alias.find("family").text] = [family.text for family in alias.find("accept")]
    return result


@pytest.mark.parametrize("name, substitute", [
    ("標楷體", "TW-Kai"), ("DFKai-SB", "TW-Kai"), ("新細明體", "Noto Serif CJK TC"),
    ("微軟正黑體", "Noto Sans CJK TC"), ("仿宋_GB2312", "Zhuque Fangsong (technical preview)"),
    ("宋体", "Noto Serif CJK SC"), ("微软雅黑", "Noto Sans CJK SC"), ("楷体", "AR PL UKai CN"),
    ("ＭＳ ゴシック", "IPAGothic"), ("MS PMincho", "IPAPMincho"), ("游ゴシック", "Noto Sans CJK JP"),
    ("Calibri", "Carlito"), ("Cambria", "Caladea"),
])
def test_document_font_names_map_to_installed_substitutes(name, substitute):
    assert [family for family in aliases()[name] if family not in COMMERCIAL][0] == substitute


# Same-design fonts a household may copy into private/; never installed by the image itself.
COMMERCIAL = {
    "FangSong", "FangSong_GB2312", "FZFangSong-Z02S", "KaiTi", "KaiTi_GB2312", "FZKai-Z03S", "STKaiti",
    "FZHei-B01S", "SimHei", "FZShuSong-Z01S", "SimSun", "FZXiaoBiaoSong-B05S", "STZhongsong", "STSong",
    "DFKai-SB", "Yu Gothic", "Yu Mincho", "Meiryo", "MS PGothic", "Microsoft JhengHei", "Microsoft YaHei",
    "Calibri", "Segoe UI", "Aptos", "Consolas",
}


@pytest.mark.parametrize("name, real", [
    ("方正小标宋_GBK", "FZXiaoBiaoSong-B05S"), ("方正黑体_GBK", "FZHei-B01S"), ("方正仿宋_GBK", "FZFangSong-Z02S"),
    ("仿宋_GB2312", "FangSong"), ("楷体", "KaiTi_GB2312"), ("BiauKai", "DFKai-SB"), ("游ゴシック体", "Yu Gothic"),
])
def test_other_names_of_a_private_font_reach_it_before_free_substitutes(name, real):
    assert aliases()[name][0] == real


def test_free_substitutes_always_follow_the_private_ones():
    for name, accepted in aliases().items():
        commercial = [family in COMMERCIAL for family in accepted]
        assert commercial == sorted(commercial, reverse=True), name
        assert not all(commercial), name


def test_mapped_fonts_are_installed_by_the_image():
    dockerfile = (PROJECT / "Dockerfile").read_text()
    for package in ("fonts-crosextra-carlito", "fonts-crosextra-caladea", "fonts-ipafont-gothic", "fonts-ipafont-mincho",
                    "fonts-ipaexfont-gothic", "fonts-morisawa-bizud-mincho", "fonts-arphic-ukai", "fonts-arphic-uming",
                    "fonts-wqy-microhei", "fonts-noto-cjk", "fonts-open-sans", "fonts-urw-base35"):
        assert package in dockerfile, package
    assert "COPY deploy/fonts/60-family-print-center.conf /etc/fonts/conf.d/" in dockerfile
    assert "RUN fc-cache -f" in dockerfile


def test_untagged_asian_text_defaults_to_traditional_chinese():
    assert re.search(r'oor:name="DefaultLocale_CJK"[^<]*><value>zh-TW</value>', PROFILE_XML)


@pytest.mark.skipif(not shutil.which("unzip"), reason="unzip is not installed")
def test_install_keeps_public_licences_but_only_fonts_from_private_files(tmp_path):
    src = tmp_path / "src"
    (src / "cache").mkdir(parents=True)
    for name in ("good.zip", "altered.zip"):
        with zipfile.ZipFile(src / "cache" / name, "w") as archive:
            archive.writestr(f"{name[:-4]}.ttf", b"font")
            archive.writestr("licence.txt", b"text")
            archive.writestr("OFL.txt", b"font licence")
            archive.writestr("COPYRIGHT.md", b"copyright notice")
            archive.writestr("README.txt", b"unneeded notes")
            archive.writestr("desktop.ini", b"unneeded metadata")
    good = hashlib.sha256((src / "cache" / "good.zip").read_bytes()).hexdigest()
    (src / "sources.txt").write_text(
        f"# comment\n{good}  good.zip  https://example.invalid/good.zip\n"
        f"{'0' * 64}  altered.zip  https://example.invalid/altered.zip\n"
        f"{'1' * 64}  missing.zip  https://example.invalid/missing.zip\n")
    (src / "private" / "windows").mkdir(parents=True)
    (src / "private" / "README.txt").write_text("note")
    (src / "private" / "windows" / "kaiu.TTF").write_bytes(b"font")
    (src / "private" / "windows" / "desktop.ini").write_text("junk")
    (src / "private" / "windows" / "LICENSE.txt").write_text("private notes")
    result = subprocess.run(["sh", str(FONTS / "install.sh"), str(src), str(tmp_path / "out")],
                            capture_output=True, text=True, check=True)
    installed = sorted(str(path.relative_to(tmp_path / "out")) for path in (tmp_path / "out").rglob("*") if path.is_file())
    assert installed == ["good/COPYRIGHT.md", "good/OFL.txt", "good/good.ttf", "good/licence.txt",
                         "private/windows/kaiu.TTF"]
    assert "checksum mismatch, skipped: altered.zip" in result.stderr
    assert "missing, skipped: missing.zip" in result.stderr


def test_installer_keeps_archive_eula_without_installer_programs(tmp_path):
    src = tmp_path / "src"
    (src / "cache").mkdir(parents=True)
    archive = src / "cache" / "example.exe"
    archive.write_bytes(b"verified fixture archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (src / "sources.txt").write_text(f"{digest} example.exe https://example.invalid/example.exe\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    extractor = bin_dir / "cabextract"
    extractor.write_text('''#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = -d ]; then shift; destination=$1; fi
    shift
done
mkdir -p "$destination"
printf font > "$destination/example.ttf"
printf 'licence terms' > "$destination/license.txt"
printf 'EULA terms' > "$destination/EULA.txt"
printf 'installer bytes' > "$destination/fontinst.exe"
printf 'installer metadata' > "$destination/fontinst.inf"
''')
    extractor.chmod(0o755)
    subprocess.run(["sh", str(FONTS / "install.sh"), str(src), "out"], cwd=tmp_path,
                   capture_output=True, text=True, check=True,
                   env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    installed = sorted(str(path.relative_to(tmp_path / "out")) for path in (tmp_path / "out").rglob("*")
                       if path.is_file())
    assert installed == ["microsoft-core/EULA.txt", "microsoft-core/example.ttf", "microsoft-core/license.txt"]


def test_fetch_downloads_only_missing_files_and_rejects_altered_ones(tmp_path):
    fonts = tmp_path / "fonts"
    (fonts / "cache").mkdir(parents=True)
    shutil.copy(FONTS / "fetch.sh", fonts / "fetch.sh")
    (fonts / "cache" / "cached.zip").write_bytes(b"cached")
    (fonts / "sources.txt").write_text(
        f"{hashlib.sha256(b'cached').hexdigest()}  cached.zip  https://example.invalid/cached.zip\n"
        f"{hashlib.sha256(b'served').hexdigest()}  new.zip  https://example.invalid/new.zip\n"
        f"{'0' * 64}  altered.zip  https://example.invalid/altered.zip\n")
    # A stand-in curl that logs each URL and always serves the same bytes.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text('#!/bin/sh\nfor last; do :; done\necho "$last" >> "$CURL_LOG"\n'
                                  'while [ "$1" != -o ]; do shift; done\nprintf served > "$2"\n')
    (bin_dir / "curl").chmod(0o755)
    log = tmp_path / "curl.log"
    result = subprocess.run(["sh", str(fonts / "fetch.sh")], capture_output=True, text=True,
                            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "CURL_LOG": str(log)})
    assert result.returncode == 1
    assert log.read_text().split() == ["https://example.invalid/new.zip", "https://example.invalid/altered.zip"]
    assert sorted(path.name for path in (fonts / "cache").iterdir()) == ["cached.zip", "new.zip"]
    assert "altered.zip" in result.stderr
