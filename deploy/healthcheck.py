"""Container health check without sending any print job."""

import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path


def office_ready():
    try:
        result = json.loads(Path("/run/family-print-center/office-runtime/ready.json").read_text())
        components = result["components"]
        if (result.get("ok") is not True or result.get("print_jobs_submitted") != 0
                or not isinstance(components, list) or len(components) != 3
                or {item.get("component") for item in components} != {"Writer", "Calc", "Impress"}
                or not all(item.get("ok") is True and isinstance(item.get("pages"), int)
                           and item["pages"] > 0 for item in components)):
            raise ValueError("Incomplete Office readiness record")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError("Office startup preview check has not passed") from error


def main():
    office_ready()
    # HOST may be a specific LAN address; use that exact bound address.
    host = os.environ.get("HOST", "0.0.0.0")
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    authority = f"[{host}]" if ":" in host else host
    port = int(os.environ.get("PORT", "8080"))
    with socket.create_connection(("127.0.0.1", 631), timeout=3):
        pass
    subprocess.run(
        ["dbus-send", "--system", "--print-reply", "--reply-timeout=1500", "--type=method_call",
         "--dest=org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server.GetVersionString"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
    )
    with urllib.request.urlopen(f"http://{authority}:{port}/healthz", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Application health check failed")


if __name__ == "__main__":
    main()
