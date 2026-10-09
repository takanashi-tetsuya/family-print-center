import os
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit


def source_code_url(value):
    """Accept an absolute source link without credentials or executable schemes."""
    if value is None or value == "":
        return ""
    message = "SOURCE_CODE_URL 必須是沒有帳號或密碼的完整 HTTP(S) 網址。"
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError(message)
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127
           for character in value) or "\\" in value:
        raise ValueError(message)
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or not parts.hostname:
            raise ValueError(message)
        if parts.username is not None or parts.password is not None:
            raise ValueError(message)
        # Accessing port rejects malformed or out-of-range ports.
        parts.port
        host = parts.hostname
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.rstrip(".").encode("idna").decode("ascii").split(".")
            if len(host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                     for label in labels):
                raise ValueError(message)
    except (ValueError, UnicodeError) as error:
        raise ValueError(message) from error
    return value


def settings():
    return {
        "DATA_DIR": os.environ.get("DATA_DIR", str(Path.cwd() / "data")),
        "MAX_UPLOAD_MB": int(os.environ.get("MAX_UPLOAD_MB", "50")),
        "MAX_PENDING_UPLOADS": 8,
        "RETENTION_HOURS": int(os.environ.get("RETENTION_HOURS", "24")),
        "CONVERSION_TIMEOUT": int(os.environ.get("CONVERSION_TIMEOUT", "180")),
        "MAX_DOCUMENT_PAGES": 1000,
        "MAX_IMAGE_PIXELS": 40_000_000,
        "CONVERSION_SYNC": False,
        "DISCOVERY_SECONDS": 8,
        "ALLOWED_HOSTS": [value.strip().lower() for value in os.environ.get("ALLOWED_HOSTS", "").split(",") if value.strip()],
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Strict",
        "MAX_FORM_MEMORY_SIZE": 64 * 1024,
        "MAX_FORM_PARTS": 20,
        # Only used the first time; otherwise a random password is written to DATA_DIR.
        "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD") or None,
        "SOURCE_CODE_URL": os.environ.get("SOURCE_CODE_URL", ""),
    }
