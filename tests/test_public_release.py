"""Public bundles exclude deployment data and never follow source symlinks."""
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "prepare_public_release.py"
spec = importlib.util.spec_from_file_location("public_release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in release.ROOT_FILES | release.FONT_SUPPORT | {
        release.POPPINS, "print_center/static/fonts/OFL-Poppins.txt", "print_center/app.py",
        ".github/workflows/tests.yml",
    }:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("GNU AFFERO GENERAL PUBLIC LICENSE\n" if name == "LICENSE" else "source\n")
    return root


def put(root, name, content="PRIVATE-TEST-DATA"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_public_bundle_excludes_data_credentials_cached_and_private_fonts(source, tmp_path):
    private_names = [".env", ".env.production", "data/admin.json", "artifacts/report.json",
                     ".claude/launch.json", ".aws/credentials", "deploy/fonts/cache/download.zip",
                     "deploy/fonts/private/windows/font.ttf", "deploy/fonts/private/.env",
                     "print_center/__pycache__/app.pyc"]
    for name in private_names:
        put(source, name)
    records = release.validate_source(source)
    archive = release.export_bundle(source, tmp_path / "out", records)
    with tarfile.open(archive) as bundle:
        names = {member.name.removeprefix("family-print-center/") for member in bundle.getmembers()}
        assert not names.intersection(private_names)
        assert "deploy/fonts/private/README.txt" in names
        assert ".github/workflows/tests.yml" in names
        assert all(b"PRIVATE-TEST-DATA" not in bundle.extractfile(member).read() for member in bundle.getmembers())


@pytest.mark.parametrize("name", ["print_center/secret.py", "README.md", "print_center/linked-dir"])
def test_source_symlinks_are_rejected_without_reading_target(source, tmp_path, name):
    outside = put(tmp_path, "outside.txt")
    link = source / name
    link.unlink(missing_ok=True)
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        release.validate_source(source)


@pytest.mark.parametrize("name", ["print_center/admin.json", "deploy/credentials.json", "tools/.env.local"])
def test_runtime_or_hidden_data_under_source_is_rejected(source, name):
    put(source, name)
    with pytest.raises(ValueError, match="Runtime data|hidden file|public file type"):
        release.validate_source(source)


def test_embedded_token_is_rejected_without_echoing_the_token(source):
    token = "ghp_" + "a" * 36
    put(source, "print_center/config.py", token)
    with pytest.raises(ValueError, match="GitHub token") as error:
        release.validate_source(source)
    assert token not in str(error.value)


def test_binary_files_and_missing_licenses_fail_validation(source):
    put(source, "print_center/font.ttf")
    with pytest.raises(ValueError, match="Unexpected public file type"):
        release.validate_source(source)
    (source / "print_center/font.ttf").unlink()
    (source / "print_center/static/fonts/OFL-Poppins.txt").unlink()
    with pytest.raises(ValueError, match="Missing required source"):
        release.validate_source(source)


def test_export_is_repeatable_and_manifest_matches_source(source, tmp_path):
    records = release.validate_source(source)
    first = release.export_bundle(source, tmp_path / "first", records)
    second = release.export_bundle(source, tmp_path / "second", records)
    assert first.read_bytes() == second.read_bytes()
    manifest = json.loads((first.parent / "source-manifest.json").read_text())
    assert manifest["source_files"] == records
    assert manifest["license"] == "AGPL-3.0-only"


def test_changed_source_is_rejected_and_output_cannot_pollute_source(source):
    records = release.validate_source(source)
    with pytest.raises(ValueError, match="outside source"):
        release.export_bundle(source, source / "deploy/release", records)
    put(source, "print_center/app.py", "changed")
    with pytest.raises(ValueError, match="changed during export"):
        release.export_bundle(source, source / "artifacts/release", records)
