"""Print service diagnostics; never submits or cancels a print job."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def safe_message(value):
    return re.sub(r"//[^/@\s]*@", "//", str(value))


def office_smoke():
    # The container installs this CLI under /opt, while the package is in /app.
    project = Path("/app") if Path("/app/print_center").is_dir() else Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    from print_center.office_probe import check_office
    return check_office()


def main(argv=None):
    parser = argparse.ArgumentParser(description="檢查打印服務，不提交或取消打印。")
    parser.add_argument("--quick", action="store_true", help="略過實際 Office 預覽檢查")
    args = parser.parse_args(argv)
    failed = False
    for command in ("cupsd", "libreoffice", "driverless", "avahi-browse"):
        executable = shutil.which(command)
        print(f"{command}: {executable or 'MISSING'}")
        failed |= executable is None
    try:
        uno = subprocess.run(
            ["/usr/bin/python3", "-c", "import uno"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5,
        )
        print(f"LibreOffice UNO: {'OK' if uno.returncode == 0 else 'MISSING'}")
        failed |= uno.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        print("LibreOffice UNO: FAILED")
        failed = True
    if args.quick:
        print("Office preview: SKIPPED (--quick); executable checks do not prove preview readiness.")
    else:
        try:
            for result in office_smoke():
                print(f"LibreOffice {result['component']} preview: {'OK' if result['ok'] else 'FAILED'}")
                failed |= not result["ok"]
        except Exception:
            print("Office preview: FAILED; check LibreOffice, UNO and application permissions.")
            failed = True

    try:
        import cups
        cups.setUser(os.environ.get("CUPS_USER", "familyprint"))
        password_file = os.environ.get("CUPS_PASSWORD_FILE", "")
        if password_file:
            password = Path(password_file).read_text().strip()
            cups.setPasswordCB(lambda _prompt: password)
        connection = cups.Connection(
            host=os.environ.get("CUPS_SERVER", "127.0.0.1"),
            port=int(os.environ.get("CUPS_PORT", "631")),
        )
        printers = connection.getPrinters()
        print(f"CUPS: OK ({len(printers)} configured printers)")
        for name, attributes in printers.items():
            # Device URIs may embed credentials; keep them out of shared diagnostics.
            uri = safe_message(attributes.get("device-uri", "URI unavailable"))
            print(f"  {name}: {uri}")
    except Exception as error:
        print(f"CUPS: FAILED ({safe_message(error)})")
        failed = True

    data = Path(os.environ.get("DATA_DIR", "/var/lib/family-print-center"))
    try:
        usage = shutil.disk_usage(data)
        print(f"Data storage: {usage.free // (1024 ** 2)} MiB free")
    except OSError:
        print("Data storage: FAILED; the configured directory is unavailable.")
        failed = True
    print("DNS-SD advertisements (8-second observation):")
    try:
        result = subprocess.run(
            ["avahi-browse", "-a", "-r", "-t", "-p"],
            timeout=8,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        print(safe_message(result.stdout.strip()) or "  No advertisements observed.")
        failed |= result.returncode != 0
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b""
        print(safe_message(output.decode(errors="replace") if isinstance(output, bytes) else output))
        print("  Observation timed out; check LAN multicast connectivity.")
    except OSError as error:
        print(f"  FAILED ({error})")
        failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
