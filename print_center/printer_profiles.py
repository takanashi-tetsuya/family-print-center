"""Persistent physical-printer profiles; each mode uses its own fixed CUPS queue."""
import hashlib
import json
import os
import re
import secrets
import threading
from pathlib import Path

QUEUE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
MODES = {"driver", "driverless"}
MODE_LABELS = {"driver": "驅動程式", "driverless": "免驅動 IPP（相容）"}
COMPANION_NAME = re.compile(r"_fpc_(?:ipp|drv)_[a-f0-9]{8}\Z", re.I)


def is_companion_name(name):
    return isinstance(name, str) and bool(COMPANION_NAME.search(name))


def companion_name(name, mode="driverless"):
    suffix = f"_fpc_{'ipp' if mode == 'driverless' else 'drv'}_{hashlib.sha256(name.encode()).hexdigest()[:8]}"
    return name[:64 - len(suffix)] + suffix


def validate_profile(name, queues, default_mode):
    if not isinstance(name, str) or not QUEUE_NAME.fullmatch(name) or is_companion_name(name):
        raise ValueError("打印機代號格式不正確。")
    if (not isinstance(queues, dict) or not queues or set(queues) - MODES
            or not isinstance(default_mode, str) or default_mode not in queues):
        raise ValueError("打印模式資料不正確。")
    if any(not isinstance(queue, str) or not QUEUE_NAME.fullmatch(queue) for queue in queues.values()):
        raise ValueError("打印模式資料不正確。")
    if name not in queues.values() or len({queue.lower() for queue in queues.values()}) != len(queues):
        raise ValueError("打印模式資料不正確。")
    return {"queues": dict(queues), "default_mode": default_mode}


class PrinterProfiles:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "printer-profiles.json"
        self.lock = threading.RLock()
        self.records = {}
        try:
            records = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # A damaged map must not silently send jobs through a different queue.
            raise ValueError("打印模式設定檔損壞，請管理員檢查 printer-profiles.json。") from exc
        try:
            if not isinstance(records, dict):
                raise ValueError()
            seen = set()
            for name, record in records.items():
                profile = validate_profile(name, record["queues"], record["default_mode"])
                names = {queue.lower() for queue in profile["queues"].values()}
                if seen & names:
                    raise ValueError()
                seen.update(names)
                self.records[name] = profile
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("打印模式設定檔損壞，請管理員檢查 printer-profiles.json。") from exc

    def snapshot(self):
        with self.lock:
            return {name: {"queues": dict(record["queues"]), "default_mode": record["default_mode"]}
                    for name, record in self.records.items()}

    def _save(self):
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.records, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def register(self, name, queues, default_mode):
        profile = validate_profile(name, queues, default_mode)
        with self.lock:
            taken = {queue.lower() for other, record in self.records.items() if other != name
                     for queue in record["queues"].values()}
            if taken & {queue.lower() for queue in queues.values()}:
                raise ValueError("這個打印隊列已屬於另一台打印機。")
            previous = self.records.get(name)
            self.records[name] = profile
            try:
                self._save()
            except BaseException:
                if previous is None:
                    self.records.pop(name, None)
                else:
                    self.records[name] = previous
                raise

    def remove(self, name):
        with self.lock:
            previous = self.records.pop(name, None)
            if previous is None:
                return
            try:
                self._save()
            except BaseException:
                self.records[name] = previous
                raise
