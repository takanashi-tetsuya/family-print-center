#!/usr/bin/env python3
"""Validate and export source only; never read local data or upload anything."""
import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

ROOT_FILES = {
    ".gitignore", ".dockerignore", ".env.example", ".gitattributes",
    "README.md", "LICENSE", "NOTICE.md", "Dockerfile", "compose.yaml",
    "main.py", "requirements.txt", "requirements-dev.txt",
}
SOURCE_DIRS = {"print_center", "deploy", "tests", "docs", "tools", ".github"}
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
FONT_SUPPORT = {
    "deploy/fonts/60-family-print-center.conf", "deploy/fonts/fetch.sh",
    "deploy/fonts/install.sh", "deploy/fonts/sources.txt",
    "deploy/fonts/private/README.txt",
}
POPPINS = "print_center/static/fonts/Poppins-SemiBold.ttf"
TEXT_EXTENSIONS = {".py", ".js", ".css", ".html", ".sh", ".c", ".conf",
                   ".md", ".txt", ".yml", ".yaml", ".json"}
MAX_BYTES = 2 * 1024 * 1024
RUNTIME_NAMES = {"admin.json", "printer-profiles.json", "cups-password", "credentials.json", "secrets.json"}
SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)


def validate_source(root):
    root = Path(root).resolve()
    paths = [root / name for name in sorted(ROOT_FILES) if (root / name).exists()]
    for folder in sorted(SOURCE_DIRS):
        directory = root / folder
        if directory.is_symlink():
            raise ValueError(f"Source directory is a symlink: {folder}")
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            name = relative.as_posix()
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            if name.startswith("deploy/fonts/") and name not in FONT_SUPPORT:
                continue
            if path.is_symlink():
                raise ValueError(f"Source symlink is not allowed: {name}")
            if path.is_file():
                paths.append(path)
    required = FONT_SUPPORT | {"LICENSE", "NOTICE.md", "README.md", "Dockerfile", "compose.yaml",
                ".env.example", "main.py", "print_center/static/fonts/OFL-Poppins.txt", POPPINS}
    present = {path.relative_to(root).as_posix() for path in paths}
    missing = required - present
    if missing:
        raise ValueError("Missing required source: " + ", ".join(sorted(missing)))
    records = []
    for path in sorted(paths):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"Source symlink is not allowed: {name}")
        if name not in ROOT_FILES and name != POPPINS and path.suffix not in TEXT_EXTENSIONS:
            raise ValueError(f"Unexpected public file type: {name}")
        if any(part.startswith(".") for part in path.relative_to(root).parts if part != ".github") and name not in ROOT_FILES:
            raise ValueError(f"Unexpected hidden file: {name}")
        if path.name in RUNTIME_NAMES:
            raise ValueError(f"Runtime data cannot be published: {name}")
        if path.stat().st_size > MAX_BYTES:
            raise ValueError(f"Unexpected large public file: {name}")
        data = path.read_bytes()
        if name != POPPINS:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"Public source is not UTF-8 text: {name}") from error
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(data):
                    raise ValueError(f"Possible {label} in {name}; no bundle was created")
        records.append({"path": name, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "mode": "0755" if path.stat().st_mode & 0o111 else "0644"})
    if b"GNU AFFERO GENERAL PUBLIC LICENSE" not in (root / "LICENSE").read_bytes():
        raise ValueError("Expected AGPL license text is missing")
    return records


def export_bundle(root, output, records):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or any(output == root / name or (root / name) in output.parents for name in SOURCE_DIRS):
        raise ValueError("Output must be outside source directories")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "family-print-center-source.tar.gz"
    with archive.open("wb") as target:
        with gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as bundle:
                # Implicit parent directories inherit the extracting process's
                # umask. Include them so a root extraction under umask 077 is
                # still readable and traversable by the application user.
                directories = {"family-print-center"}
                for record in records:
                    directories.update("family-print-center/" + parent.as_posix()
                                       for parent in PurePosixPath(record["path"]).parents if parent.parts)
                for name in sorted(directories):
                    info = tarfile.TarInfo(name + "/")
                    info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
                    bundle.addfile(info)
                for record in records:
                    data = (root / record["path"]).read_bytes()
                    if hashlib.sha256(data).hexdigest() != record["sha256"]:
                        raise ValueError(f"Source changed during export: {record['path']}")
                    info = tarfile.TarInfo("family-print-center/" + record["path"])
                    info.size, info.mode, info.mtime = len(data), int(record["mode"], 8), 0
                    bundle.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {"format": 1, "license": "AGPL-3.0-only", "source_files": records,
                "archive": archive.name, "archive_sha256": digest}
    (output / "source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (output / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without creating files")
    parser.add_argument("--output", type=Path, help="Default: artifacts/public-release")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        records = validate_source(root)
        result = {"files": len(records), "bytes": sum(item["bytes"] for item in records),
                  "license": "AGPL-3.0-only", "private_fonts_included": False, "uploads_performed": 0}
        if not args.check:
            result["archive"] = str(export_bundle(root, args.output or root / "artifacts/public-release", records))
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
