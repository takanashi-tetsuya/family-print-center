"""Disposable native PPD reader; never connects to CUPS or submits a job.

pycups 2.0.4 bulk PPD getters retain native references and their file handles.
Process exit releases those handles without relying on Python garbage collection.
Only plain JSON leaves this process; the caller owns and removes the input PPD.
"""
import json
import sys


def main():
    payload = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(payload) > 4 * 1024 * 1024:
        return 1
    try:
        request = json.loads(payload)
        if not isinstance(request, dict) or not isinstance(request.get("path"), str):
            return 1
        operation = request.get("operation")
        options = request.get("options")
        if operation not in {"options", "validate"}:
            return 1
        if operation == "options" and (not isinstance(options, list)
                                       or not isinstance(request.get("attrs"), dict)):
            return 1
        if operation == "validate" and not isinstance(options, dict):
            return 1
        import cups
        from .cups_client import CupsClient

        client = CupsClient.__new__(CupsClient)
        client.cups = cups
        if operation == "options":
            controls, aliases = client._ppd_options_local(request["path"], request["attrs"], options)
            reply = {"operation": operation, "options": controls, "aliases": aliases}
        else:
            try:
                available = client._validate_ppd_options_local(request["path"], options)
                reply = {"operation": operation, "available": available, "conflict": False}
            except ValueError:
                reply = {"operation": operation, "available": True, "conflict": True}
        output = json.dumps(reply, ensure_ascii=False)
        if len(output.encode("utf-8")) > 4 * 1024 * 1024:
            return 1
        sys.stdout.write(output)
        return 0
    except Exception:
        # Do not expose native errors, paths or potentially private PPD strings.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
