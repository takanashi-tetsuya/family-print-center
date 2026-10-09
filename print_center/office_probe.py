"""Verify the real Writer/Calc/Impress preview path without submitting any jobs."""
import argparse
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pymupdf as fitz

from .conversion import preview_office


MARKER_TEXT = "FPC Office runtime check"
NAMESPACES = '''xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"'''
FIXTURES = {
    "Writer": ("odt", "text", f"<office:text><text:p>{MARKER_TEXT}</text:p></office:text>"),
    "Calc": ("ods", "spreadsheet", f'''<office:spreadsheet><table:table table:name="Runtime">
      <table:table-row><table:table-cell office:value-type="string"><text:p>{MARKER_TEXT}</text:p>
      </table:table-cell></table:table-row></table:table></office:spreadsheet>'''),
    "Impress": ("odp", "presentation", f'''<office:presentation>
      <draw:page draw:name="Runtime" draw:master-page-name="Default">
      <draw:frame svg:x="1cm" svg:y="1cm" svg:width="24cm" svg:height="4cm">
      <draw:text-box><text:p>{MARKER_TEXT}</text:p></draw:text-box>
      </draw:frame></draw:page></office:presentation>'''),
}


def write_fixture(path, kind, body):
    media_type = f"application/vnd.oasis.opendocument.{kind}"
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
      <office:document-content {NAMESPACES} office:version="1.2">
      <office:body>{body}</office:body></office:document-content>'''
    styles = f'''<?xml version="1.0" encoding="UTF-8"?>
      <office:document-styles {NAMESPACES} office:version="1.2">
      <office:styles/><office:automatic-styles>
      <style:page-layout style:name="pm1"><style:page-layout-properties
      fo:page-width="28cm" fo:page-height="21cm" style:print-orientation="landscape"/>
      </style:page-layout></office:automatic-styles><office:master-styles>
      <style:master-page style:name="Default" style:page-layout-name="pm1"/>
      </office:master-styles></office:document-styles>'''
    manifest = f'''<?xml version="1.0" encoding="UTF-8"?>
      <manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
      manifest:version="1.2"><manifest:file-entry manifest:full-path="/" manifest:media-type="{media_type}"/>
      <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
      <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
      </manifest:manifest>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", media_type, compress_type=zipfile.ZIP_STORED)
        archive.writestr("content.xml", content)
        archive.writestr("styles.xml", styles)
        archive.writestr("META-INF/manifest.xml", manifest)


def check_office(timeout=30):
    """Use private generated documents, never uploads or an actual printer."""
    results = []
    with tempfile.TemporaryDirectory(prefix="fpc-office-probe-") as temporary:
        root = Path(temporary)
        for name, (extension, kind, body) in FIXTURES.items():
            directory = root / name.lower()
            directory.mkdir(mode=0o700)
            source, target = directory / f"check.{extension}", directory / "preview.pdf"
            write_fixture(source, kind, body)
            try:
                preview_office(source, target, directory, timeout=timeout)
                with fitz.open(target) as document:
                    if len(document) < 1 or MARKER_TEXT not in " ".join(
                            " ".join(page.get_text().split()) for page in document):
                        raise ValueError("The generated document was not rendered correctly.")
                    results.append({"component": name, "ok": True, "pages": len(document)})
            except Exception:
                # No document contents, environment variables or credentials in diagnostics.
                results.append({"component": name, "ok": False,
                                "error": "實際預覽失敗；請檢查 LibreOffice、UNO、檔案權限與 headless 設定。"})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="實際檢查 Office 預覽，不提交打印。")
    parser.add_argument("--marker", type=Path, help="三項成功後寫入啟動就緒記錄")
    args = parser.parse_args(argv)
    if args.marker:
        args.marker.unlink(missing_ok=True)
    results = check_office()
    ok = all(result["ok"] for result in results)
    for result in results:
        print(f"LibreOffice {result['component']} preview: {'OK' if result['ok'] else 'FAILED'}")
    if ok and args.marker:
        with tempfile.NamedTemporaryFile(mode="w", dir=args.marker.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump({"ok": True, "components": results, "print_jobs_submitted": 0}, stream)
                stream.flush()
                os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, args.marker)
            finally:
                temporary.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
