"""Multipart limits must not reject ordinary files at read-chunk boundaries."""
import io

import pytest

from print_center.app import create_app
from tests.helpers import pdf_bytes
from tests.test_api import FakeCups, FakeDiscovery


@pytest.fixture
def upload_client(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path),
                      "CONVERSION_SYNC": True, "MAX_UPLOAD_MB": 1,
                      "SECRET_KEY": "test-session-secret", "ADMIN_PASSWORD": "test-password"},
                     cups_client=FakeCups(), discovery=FakeDiscovery())
    client = app.test_client()
    headers = {"X-CSRF-Token": client.get("/api/status").json["csrf_token"]}
    yield app, client, headers
    app.extensions["uploads"].executor.shutdown(wait=True)


def multipart_pdf_with_split_newline(size):
    """A valid PDF comment splits CRLF at the parser's first 64 KiB read."""
    boundary = "FPC-Synthetic-Upload-Boundary"
    prefix = (f'--{boundary}\r\n'
              'Content-Disposition: form-data; name="file"; filename="synthetic.pdf"\r\n'
              'Content-Type: application/pdf\r\n\r\n').encode()
    head = pdf_bytes(pages=1).rsplit(b"%%EOF", 1)[0]
    split = 64 * 1024 - len(prefix) - 1
    before = head + b"%" + b"a" * (split - len(head) - 1)
    after = b"\r\n%"
    ending = b"\n%%EOF\n"
    content = before + after + b"a" * (size - len(before) - len(after) - len(ending)) + ending
    assert len(content) == size
    assert (prefix + content)[64 * 1024 - 1:64 * 1024 + 1] == b"\r\n"
    suffix = f"\r\n--{boundary}--\r\n".encode()
    return prefix + content + suffix, f"multipart/form-data; boundary={boundary}"


@pytest.mark.parametrize("file_size", [128 * 1024, 200 * 1024, 1024 * 1024])
def test_valid_pdf_at_multipart_chunk_boundary_uploads_and_previews(upload_client, file_size):
    app, client, headers = upload_client
    body, content_type = multipart_pdf_with_split_newline(file_size)
    result = client.post("/api/uploads", data=body, content_type=content_type, headers=headers)
    assert result.status_code == 202
    upload = client.get(f"/api/uploads/{result.json['id']}").json
    assert upload["status"] == "ready" and upload["pages"] == 1
    assert client.get(f"/api/uploads/{result.json['id']}/pdf").status_code == 200
    assert app.extensions["uploads"].get(result.json["id"])["size"] == file_size
    assert app.extensions["cups_client"].prints == []


def test_actual_oversized_file_is_rejected_with_file_limit(upload_client):
    app, client, headers = upload_client
    result = client.post("/api/uploads", data={"file": (io.BytesIO(b"x" * (1200 * 1024)), "large.pdf")},
                         headers=headers)
    assert result.status_code == 413
    assert "文件太大" in result.json["error"] and "1 MB" in result.json["error"]
    assert list(app.extensions["uploads"].root.iterdir()) == []


def test_oversized_nonfile_form_field_is_rejected_without_false_file_size_message(upload_client):
    app, client, headers = upload_client
    result = client.post("/api/uploads", data={"file": (io.BytesIO(pdf_bytes(pages=1)), "small.pdf"),
                                               "extra": "x" * (app.config["MAX_FORM_MEMORY_SIZE"] + 1)},
                         headers=headers)
    assert result.status_code == 413
    assert "表單" in result.json["error"] and "文件太大" not in result.json["error"]
    assert "MB" not in result.json["error"]
    assert list(app.extensions["uploads"].root.iterdir()) == []


def test_excess_multipart_parts_are_rejected(upload_client):
    app, client, headers = upload_client
    fields = {f"extra{number}": "x" for number in range(app.config["MAX_FORM_PARTS"])}
    fields["file"] = (io.BytesIO(pdf_bytes(pages=1)), "small.pdf")
    result = client.post("/api/uploads", data=fields, headers=headers)
    assert result.status_code == 413
    assert "項目太多" in result.json["error"] and "MB" not in result.json["error"]
    assert list(app.extensions["uploads"].root.iterdir()) == []
