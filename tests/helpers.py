import io
import zipfile

import fitz
from werkzeug.datastructures import FileStorage


def pdf_bytes(pages=3, encrypted=False):
    with fitz.open() as document:
        for number in range(pages):
            document.new_page().insert_text((40, 40), f"Family document {number + 1}")
        if encrypted:
            return document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                                   owner_pw="owner-secret", user_pw="private")
        return document.tobytes()


def upload_file(content, name="document.pdf"):
    return FileStorage(stream=io.BytesIO(content), filename=name)


def office_bytes(extension="docx"):
    members = {"docx": "word/document.xml", "docm": "word/document.xml",
               "xlsx": "xl/workbook.xml", "xlsm": "xl/workbook.xml",
               "pptx": "ppt/presentation.xml", "pptm": "ppt/presentation.xml", "ppsx": "ppt/presentation.xml"}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(members[extension], "<document>Home work</document>")
        if extension in {"docm", "xlsm", "pptm"}:
            folder = members[extension].split("/", 1)[0]
            archive.writestr(f"{folder}/vbaProject.bin", b"macro content that must never be executed")
    return stream.getvalue()


def epub_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''')
        archive.writestr("OEBPS/content.opf", '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id" version="2.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Family book</dc:title>
  <dc:language>en</dc:language><dc:identifier id="id">family-book</dc:identifier></metadata>
 <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="chapter"/></spine>
</package>''')
        archive.writestr("OEBPS/chapter.xhtml", '''<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Family book</title></head>
<body><h1>Our Family</h1><p>This is a printable book chapter.</p></body></html>''')
    return stream.getvalue()
