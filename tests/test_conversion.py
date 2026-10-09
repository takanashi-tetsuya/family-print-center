import io
import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from print_center.config import settings
from print_center.conversion import ConversionError, QueueFull, UploadStore, office_environment, preview_office
from tests.helpers import epub_bytes, office_bytes, pdf_bytes, upload_file


@pytest.fixture
def store(tmp_path):
    config = settings()
    config.update(DATA_DIR=str(tmp_path), CONVERSION_SYNC=True)
    value = UploadStore(config)
    yield value
    value.executor.shutdown(wait=True)


def test_upload_uses_private_generated_path_and_removes_original(store, tmp_path):
    upload_id = store.create(upload_file(pdf_bytes(), "../../旅行.pdf"))
    result = store.public(upload_id)
    assert result["status"] == "ready"
    assert result["pages"] == 3
    assert result["name"] == "旅行.pdf"
    assert len(upload_id) == 32
    assert (store.directory(upload_id) / "document.pdf").is_file()
    assert not (store.directory(upload_id) / "input.pdf").exists()
    assert not (tmp_path / "旅行.pdf").exists()
    assert store.directory(upload_id).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("content", [b"not a PDF", b"%PDF-1.7\ninvalid body"])
def test_malformed_pdf_is_unprintable_and_not_retained(store, content):
    upload_id = store.create(upload_file(content))
    assert store.public(upload_id)["status"] == "error"
    assert store.public(upload_id)["error"]
    assert not (store.directory(upload_id) / "document.pdf").exists()
    assert not (store.directory(upload_id) / "input.pdf").exists()


def test_password_protected_pdf_fails_with_actionable_error(store):
    upload_id = store.create(upload_file(pdf_bytes(encrypted=True)))
    result = store.public(upload_id)
    assert result["status"] == "error"
    assert "密碼" in result["error"]


def test_document_page_limit_is_enforced(store):
    store.config["MAX_DOCUMENT_PAGES"] = 2
    upload_id = store.create(upload_file(pdf_bytes(3)))
    assert store.public(upload_id)["status"] == "error"


@pytest.mark.parametrize("name", ["script.exe", "payload.html", "printer.ppd", "no-extension"])
def test_unknown_format_is_rejected_before_saving(store, name):
    with pytest.raises(ConversionError):
        store.create(upload_file(b"payload", name))
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize("upload_id", ["../outside", "a" * 31, "A" * 32, "a" * 32 + "/../x", None])
def test_untrusted_upload_ids_cannot_escape_storage(store, upload_id):
    with pytest.raises(KeyError):
        store.directory(upload_id)


def test_empty_and_oversized_uploads_release_conversion_slots(store):
    store.config.update(MAX_UPLOAD_MB=1, MAX_PENDING_UPLOADS=1)
    store.slots = threading.BoundedSemaphore(1)
    for content in (b"", b"0" * (1024 * 1024 + 1)):
        with pytest.raises(ConversionError):
            store.create(upload_file(content))
    assert list(store.root.iterdir()) == []
    assert store.public(store.create(upload_file(pdf_bytes())))["status"] == "ready"


def test_queue_capacity_is_bounded_and_released_after_conversion(store, monkeypatch):
    store.config["CONVERSION_SYNC"] = False
    store.slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(store.executor, "submit", lambda *_: None)
    first = store.create(upload_file(pdf_bytes()))
    with pytest.raises(QueueFull):
        store.create(upload_file(pdf_bytes()))
    store.convert(first)
    assert store.public(store.create(upload_file(pdf_bytes())))["status"] == "queued"


def test_missing_office_converter_explains_how_to_proceed(store, monkeypatch):
    monkeypatch.setattr("print_center.conversion.shutil.which", lambda _: None)
    with pytest.raises(ConversionError, match="Office"):
        store.create(upload_file(b"content", "homework.pptx"))
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize("format_name, extension, expected_pages", [("TIFF", "tiff", 2), ("GIF", "gif", 1)])
def test_image_document_and_animation_have_expected_pages(store, format_name, extension, expected_pages):
    stream = io.BytesIO()
    with Image.new("RGB", (32, 32), "red") as first, Image.new("RGB", (32, 32), "blue") as second:
        first.save(stream, format=format_name, save_all=True, append_images=[second])
    upload_id = store.create(upload_file(stream.getvalue(), f"photo.{extension}"))
    assert store.public(upload_id)["status"] == "ready"
    assert store.public(upload_id)["pages"] == expected_pages


def test_image_pixel_limit_prevents_large_conversion(store):
    store.config["MAX_IMAGE_PIXELS"] = 100
    stream = io.BytesIO()
    with Image.new("RGB", (11, 10), "red") as image:
        image.save(stream, "PNG")
    upload_id = store.create(upload_file(stream.getvalue(), "photo.png"))
    assert store.public(upload_id)["status"] == "error"


def test_expired_documents_are_removed_but_active_conversions_are_kept(store):
    expired = store.create(upload_file(pdf_bytes()))
    old = store.get(expired)
    old["created_at"] = time.time() - 2 * 86400
    store.write(old)
    queued_id = "1" * 32
    store.directory(queued_id).mkdir()
    store.write({"id": queued_id, "created_at": old["created_at"], "status": "converting"})
    store.cleanup()
    with pytest.raises(KeyError):
        store.get(expired)
    assert store.get(queued_id)["status"] == "converting"


def test_active_print_original_cannot_be_deleted_or_expired(store):
    upload_id = store.create(upload_file(pdf_bytes()))
    record = store.get(upload_id)
    record["created_at"] = time.time() - 2 * 86400
    store.write(record)
    store.active_prints.add(upload_id)
    store.cleanup()
    assert store.get(upload_id)["status"] == "ready"
    with pytest.raises(ConversionError):
        store.delete(upload_id)
    store.active_prints.remove(upload_id)
    store.cleanup()
    with pytest.raises(KeyError):
        store.get(upload_id)


def test_restart_marks_interrupted_conversion_as_retryable_error(store):
    upload_id = "2" * 32
    store.directory(upload_id).mkdir()
    store.write({"id": upload_id, "created_at": time.time(), "status": "converting"})
    restarted = UploadStore(store.config)
    try:
        assert restarted.get(upload_id)["status"] == "error"
        assert "重新上傳" in restarted.get(upload_id)["error"]
    finally:
        restarted.executor.shutdown(wait=True)


@pytest.mark.parametrize("extension", ["docx", "xlsx", "pptx", "docm", "xlsm", "pptm", "ppsx"])
def test_office_original_is_preserved_for_native_printing_alongside_preview(store, monkeypatch, extension):
    monkeypatch.setattr(store, "capabilities", lambda: {"office": True})
    monkeypatch.setattr(store, "_office_preview", lambda source, target, directory: target.write_bytes(pdf_bytes()))
    original = office_bytes(extension)
    upload_id = store.create(upload_file(original, f"homework.{extension}"))
    record = store.public(upload_id)
    assert record["status"] == "ready" and record["native"] is True
    assert record["pages"] == 3 and record["preview_url"]
    assert (store.directory(upload_id) / f"input.{extension}").read_bytes() == original
    assert (store.directory(upload_id) / "document.pdf").is_file()


def test_mismatched_office_archive_fails_before_native_printing(store, monkeypatch):
    monkeypatch.setattr(store, "capabilities", lambda: {"office": True})
    upload_id = store.create(upload_file(office_bytes("xlsx"), "pretend.docx"))
    assert store.public(upload_id)["status"] == "error"
    assert not (store.directory(upload_id) / "input.docx").exists()


def test_epub_becomes_an_a4_printable_document(store):
    upload_id = store.create(upload_file(epub_bytes(), "storybook.epub"))
    record = store.public(upload_id)
    assert record["status"] == "ready" and record["pages"] >= 1
    assert record["native"] is False and record["preview_url"]
    assert not (store.directory(upload_id) / "input.epub").exists()


def test_archive_with_excessive_members_is_rejected(store, monkeypatch):
    monkeypatch.setattr(store, "capabilities", lambda: {"office": True})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for number in range(10001):
            archive.writestr(str(number), "")
        archive.writestr("word/document.xml", "<document/>")
    upload_id = store.create(upload_file(stream.getvalue(), "many.docx"))
    assert store.public(upload_id)["status"] == "error"


def test_office_preview_runs_in_private_profile_with_macros_disabled(store, monkeypatch):
    monkeypatch.setattr("print_center.conversion.shutil.which", lambda _: "/usr/bin/libreoffice")
    invocations = []

    def launch(command, **kwargs):
        invocations.append((command, kwargs))
        preview_request = json.loads(Path(command[-1]).read_text())
        assert preview_request["mode"] == "preview"
        profile = Path(preview_request["profile"])
        xml = (profile / "user" / "registrymodifications.xcu").read_text()
        assert 'oor:name="DisableMacrosExecution"' in xml and '<value>true</value>' in xml
        assert 'oor:name="DisableActiveContent"' in xml
        assert Path(preview_request["source"]).name == "input.docx"
        Path(preview_request["target"]).write_bytes(pdf_bytes())
        return SimpleNamespace(returncode=0, wait=lambda timeout: 0)

    monkeypatch.setattr("print_center.conversion.subprocess.Popen", launch)
    upload_id = store.create(upload_file(office_bytes(), "../../untrusted.docx"))
    assert store.public(upload_id)["status"] == "ready"
    command, kwargs = invocations[0]
    assert command[0] == "/usr/bin/python3" and Path(command[1]).name == "office_worker.py"
    assert kwargs["start_new_session"] is True
    assert not (store.directory(upload_id) / "preview-request.json").exists()
    assert not (store.directory(upload_id) / "office-profile").exists()
    assert (store.directory(upload_id) / "input.docx").is_file()


def test_office_preview_timeout_kills_process_group_and_releases_slot(store, monkeypatch):
    import signal
    import subprocess
    monkeypatch.setattr("print_center.conversion.shutil.which", lambda _: "/usr/bin/libreoffice")
    process = Mock(pid=9876)
    process.wait.side_effect = [subprocess.TimeoutExpired("preview", 1), 0]
    monkeypatch.setattr("print_center.conversion.subprocess.Popen", lambda *args, **kwargs: process)
    kill = Mock()
    monkeypatch.setattr("print_center.conversion.os.killpg", kill)
    store.slots = threading.BoundedSemaphore(1)
    upload_id = store.create(upload_file(office_bytes(), "slides.docx"))
    assert store.public(upload_id)["status"] == "error"
    assert "逾時" in store.public(upload_id)["error"]
    kill.assert_called_once_with(9876, signal.SIGKILL)
    assert not (store.directory(upload_id) / "input.docx").exists()
    assert store.public(store.create(upload_file(pdf_bytes())))["status"] == "ready"


def test_headless_office_environment_cannot_regress_to_x11_or_inherit_a_stale_bridge(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("SAL_USE_VCLPLUGIN", "gen")
    monkeypatch.setenv("LD_PRELOAD", "/old-job/cups-bridge.so")
    monkeypatch.setenv("ADMIN_PASSWORD", "not-for-office")
    environment = office_environment(SAL_USE_VCLPLUGIN="gen", HOME="/tmp/private-office")
    assert environment["SAL_USE_VCLPLUGIN"] == "svp"
    assert environment["HOME"] == "/tmp/private-office"
    assert "DISPLAY" not in environment and "LD_PRELOAD" not in environment
    assert "ADMIN_PASSWORD" not in environment


def test_standalone_office_preview_uses_isolated_svp_without_touching_upload_records(tmp_path, monkeypatch):
    source, target = tmp_path / "input.docx", tmp_path / "preview.pdf"
    source.write_bytes(b"original office")
    old_upload = tmp_path / "uploads" / "old-record"
    old_upload.mkdir(parents=True)
    metadata = old_upload / "metadata.json"
    metadata.write_text('{"created_at":0,"status":"ready"}')
    observed = []
    monkeypatch.setenv("SAL_USE_VCLPLUGIN", "gen")
    monkeypatch.delenv("DISPLAY", raising=False)

    def launch(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        observed.append(Path(command[-1]).parent)
        assert request["mode"] == "preview" and "printer" not in request
        assert request["source"] == str(source.resolve())
        assert kwargs["env"]["SAL_USE_VCLPLUGIN"] == "svp"
        assert kwargs["start_new_session"] is True
        assert observed[-1].stat().st_mode & 0o777 == 0o700
        target.write_bytes(pdf_bytes())
        return SimpleNamespace(returncode=0, wait=lambda timeout: 0 if timeout == 30 else pytest.fail("wrong timeout"))

    monkeypatch.setattr("print_center.conversion.subprocess.Popen", launch)
    preview_office(source, target, tmp_path, timeout=30)
    assert target.read_bytes().startswith(b"%PDF-") and source.read_bytes() == b"original office"
    assert metadata.read_text() == '{"created_at":0,"status":"ready"}'
    assert not observed[0].exists()


@pytest.mark.parametrize("failure", ["spawn", "worker", "stale-preview"])
def test_standalone_preview_cleans_profiles_and_partial_or_stale_pdf_after_failure(tmp_path, monkeypatch, failure):
    source, target = tmp_path / "input.docx", tmp_path / "preview.pdf"
    source.write_bytes(b"original office")
    target.write_bytes(pdf_bytes())
    observed = []

    def launch(command, **kwargs):
        observed.append(Path(command[-1]).parent)
        assert not target.exists()
        if failure == "spawn":
            raise OSError("worker unavailable")
        if failure == "worker":
            target.write_bytes(b"partial PDF")
        return SimpleNamespace(returncode=1 if failure == "worker" else 0, wait=lambda timeout: 0)

    monkeypatch.setattr("print_center.conversion.subprocess.Popen", launch)
    with pytest.raises(ConversionError):
        preview_office(source, target, tmp_path, timeout=30)
    assert not target.exists() and not observed[0].exists()
    assert source.read_bytes() == b"original office"


def test_preview_timeout_is_reported_even_when_process_group_already_exited(tmp_path, monkeypatch):
    import subprocess
    source, target = tmp_path / "input.docx", tmp_path / "preview.pdf"
    source.write_bytes(b"original office")
    process = Mock(pid=9876, returncode=1)
    process.wait.side_effect = [subprocess.TimeoutExpired("preview", 30), 0]
    monkeypatch.setattr("print_center.conversion.subprocess.Popen", Mock(return_value=process))
    monkeypatch.setattr("print_center.conversion.os.killpg", Mock(side_effect=ProcessLookupError))
    with pytest.raises(ConversionError, match="逾時"):
        preview_office(source, target, tmp_path, timeout=30)
    assert not list(tmp_path.glob("office-preview-*"))
