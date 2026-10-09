"""Prepare the two existing household queues for driver/IPP profiles.

Run inside the deployed container as familyprint:
    python -m print_center.printer_profiles_cli           # inspect only
    python -m print_center.printer_profiles_cli --apply   # add/register profiles
    python -m print_center.printer_profiles_cli --printer epson_l6276 --align-ready-paper

After --apply, restart the application/container so its profile map is reloaded.
This helper never submits a print job, changes a password, or installs software.
"""
import argparse
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .admin import GENERIC_PCL, automatic_driver, build_driver_index, driver_candidates
from .config import settings
from .cups_client import CupsClient, CupsError
from .discovery import Discovery
from .printer_profiles import companion_name

HOUSEHOLD_PRINTERS = ("brother_mfc8515dn", "epson_l6276")

GENERIC_PCL_MODEL = "Generic PCL Laser Printer"
DUPLEX_DEFAULT = re.compile(rb"(?m)^(\*DefaultOption1:[ \t]*)(False|True)([ \t]*\r?$)")
PAPER_DEFAULT = re.compile(rb"(?m)^(\*Default(PageSize|PageRegion):[ \t]*)([^\s]+)([ \t]*\r?$)")


def _dnssd_identity(uri):
    """Use the queue's existing UUID, never a household identifier in source."""
    if not isinstance(uri, str):
        return None
    try:
        endpoint = urlsplit(uri)
    except ValueError:
        return None
    if endpoint.scheme != "dnssd":
        return None
    identities = parse_qs(endpoint.query, keep_blank_values=True).get("uuid", [])
    if len(identities) != 1:
        return None
    try:
        return str(uuid.UUID(identities[0]))
    except (ValueError, AttributeError):
        return None


def _confirmed_device(device):
    identity = _dnssd_identity(device.get("uri"))
    return bool(identity and device.get("identity") == identity
                and device.get("kind") in {"ipp", "ipps"} and device.get("driverless"))


def _read_queue_ppd(cups, queue):
    temporary = cups._call(lambda connection: connection.getPPD(queue))
    if not isinstance(temporary, (str, bytes, os.PathLike)):
        raise CupsError("無法讀取既有驅動設定，未更改隊列預設。")
    path = Path(os.fsdecode(temporary))
    try:
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def _generic_duplex_default(ppd):
    """Recognize only CUPS' exact generic PCL Duplexer definition."""
    required = (
        rb'\*PPD-Adobe:[ \t]+"4.3"',
        rb'\*ModelName:[ \t]+"Generic PCL Laser Printer"',
        rb'\*NickName:[ \t]+"Generic PCL Laser Printer"',
        rb'\*1284DeviceID:[ \t]+"CMD:PCL;"',
        rb'\*cupsFilter:[ \t]+"application/vnd.cups-raster 50 rastertohp"',
        rb'\*OpenGroup:[ \t]+InstallableOptions/Installable Options',
        rb'\*OpenUI[ \t]+\*Option1/Duplexer:[ \t]+Boolean',
        rb'\*Option1[ \t]+False/Not Installed:[ \t]+""',
        rb'\*Option1[ \t]+True/Installed:[ \t]+""',
        rb'\*UIConstraints:[ \t]+\*Duplex[ \t]+\*Option1[ \t]+False',
        rb'\*UIConstraints:[ \t]+\*Option1[ \t]+False[ \t]+\*Duplex',
    )
    if any(not re.search(rb"(?m)^" + line + rb"[ \t]*\r?$", ppd) for line in required):
        return None
    defaults = list(DUPLEX_DEFAULT.finditer(ppd))
    return defaults[0].group(2) if len(defaults) == 1 else None


def configure_generic_duplexer(cups, plan, apply=False):
    """Enable hardware only when this verified printer announces IPP duplex."""
    name, queues, device = plan["name"], plan["queues"], plan["device"]
    result = {"needs_configuration": False, "changed": False, "status": "not_applicable"}
    if name != "brother_mfc8515dn" or "driver" not in queues or "driverless" not in queues:
        return result
    if plan["operation"] == "add_driver" and plan["recommended_driver"] != GENERIC_PCL:
        return result
    if not _confirmed_device(device):
        raise ValueError("雙面器設定缺少已確認的打印機身份，未更改設定。")
    current = cups._call(lambda connection: connection.getPrinters())
    if plan["operation"] == "add_driver" and queues["driver"] not in current and not apply:
        return {**result, "needs_configuration": None, "status": "check_after_driver_creation"}
    if any(queue not in current or current[queue].get("device-uri") != device["uri"] for queue in queues.values()):
        raise ValueError("打印模式的連線已變更，未更改雙面器。")
    driver_queue = queues["driver"]
    if current[driver_queue].get("printer-make-and-model") != GENERIC_PCL_MODEL:
        return result
    capabilities = cups._call(lambda connection: connection.getPrinterAttributes(
        queues["driverless"], requested_attributes=["sides-supported"]))
    sides = capabilities.get("sides-supported", [])
    sides = [sides] if isinstance(sides, str) else sides
    if not isinstance(sides, (list, tuple)) or not {"two-sided-long-edge", "two-sided-short-edge"}.intersection(
            item for item in sides if isinstance(item, str)):
        return {**result, "status": "ipp_duplex_not_confirmed"}
    original = _read_queue_ppd(cups, driver_queue)
    default = _generic_duplex_default(original)
    if default is None:
        return {**result, "status": "unrecognized_ppd"}
    if default == b"True":
        return {**result, "status": "enabled"}
    result.update(needs_configuration=True, status="enable_duplexer")
    if not apply:
        return result
    patched = DUPLEX_DEFAULT.sub(lambda match: match.group(1) + b"True" + match.group(3), original, count=1)
    with tempfile.TemporaryDirectory(prefix="fpc-duplexer-") as directory:
        backup, updated = Path(directory) / "original.ppd", Path(directory) / "enabled.ppd"
        backup.write_bytes(original)
        updated.write_bytes(patched)
        cups.check_ppd(updated)
        try:
            cups.modify_printer(driver_queue, ppd_file=updated)
            if _read_queue_ppd(cups, driver_queue) != patched:
                raise CupsError("CUPS 未保留已確認的雙面器設定。")
        except Exception as error:
            try:
                cups.modify_printer(driver_queue, ppd_file=backup)
            except Exception as rollback_error:
                raise CupsError("雙面器設定失敗且未能回復原有 PPD；請管理員檢查 CUPS。") from rollback_error
            raise CupsError("雙面器設定失敗，已回復原有 PPD；請管理員檢查後再試。") from error
    return {**result, "needs_configuration": False, "changed": True, "status": "enabled"}


def _ready_paper_attributes(cups, device):
    """Read the physical printer; CUPS' media-ready may only repeat its PPD."""
    uri = device.get("ipp_uri", "")
    endpoint = urlsplit(uri)
    if (endpoint.scheme not in {"ipp", "ipps"} or not endpoint.hostname
            or not endpoint.hostname.lower().endswith(".local")
            or endpoint.username is not None or endpoint.password is not None):
        raise ValueError("缺少已探索到的區域網路 IPP 位址，未更改紙張預設。")
    # This connection does not use the authenticated CUPS administration client.
    cups.cups.setPasswordCB(lambda _prompt: "")
    encryption = cups.cups.HTTP_ENCRYPT_ALWAYS if endpoint.scheme == "ipps" else cups.cups.HTTP_ENCRYPT_NEVER
    try:
        connection = cups.cups.Connection(host=endpoint.hostname, port=endpoint.port or 631, encryption=encryption)
        return connection.getPrinterAttributes(uri=uri, requested_attributes=[
            "printer-uuid", "media-ready", "media-default"])
    except Exception as error:
        raise CupsError("無法讀取打印機實際裝紙，未更改紙張預設；請確認打印機已開機。") from error


def _single_keyword(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, str) for item in value):
        distinct = set(value)
        return next(iter(distinct)) if len(distinct) == 1 else None
    return None


def configure_ready_paper(cups, plan, apply=False):
    """Opt-in correction of this Epson driverless queue's factory Letter default.

    Do not infer that every household uses A4, nor replace another chosen default.
    Checking the physical printer is necessary for native Office print jobs,
    whose printer setup may use the PPD before per-job CUPS options are applied.
    """
    result = {"needs_configuration": False, "changed": False, "status": "not_requested"}
    if not plan.get("align_ready_paper"):
        return result
    result["status"] = "not_applicable"
    device, queues = plan["device"], plan["queues"]
    if plan["name"] != "epson_l6276" or "driverless" not in queues:
        return result
    if not _confirmed_device(device):
        raise ValueError("紙張設定缺少已確認的打印機身份，未更改設定。")
    queue = queues["driverless"]
    current = cups._call(lambda connection: connection.getPrinters())
    if queue not in current or current[queue].get("device-uri") != device["uri"]:
        raise ValueError("打印模式的連線已變更，未更改紙張預設。")
    if not re.search(r"\bEPSON\s+L6270\s+Series\b.*\bdriverless\b",
                     str(current[queue].get("printer-make-and-model") or ""), re.I):
        return result
    original = _read_queue_ppd(cups, queue)
    if (not re.search(rb'(?im)^\*ModelName:[ \t]+"EPSON L6270 Series"[ \t]*\r?$', original)
            or not re.search(rb'(?im)^\*NickName:[ \t]+"EPSON L6270 Series[^"\r\n]*\bdriverless\b[^"\r\n]*"[ \t]*\r?$', original)):
        return {**result, "status": "unrecognized_ppd"}
    defaults = list(PAPER_DEFAULT.finditer(original))
    if len(defaults) != 2 or {match.group(2) for match in defaults} != {b"PageSize", b"PageRegion"}:
        return {**result, "status": "unrecognized_ppd"}
    values = {match.group(3) for match in defaults}
    if values == {b"A4"}:
        return {**result, "status": "aligned"}
    if values != {b"Letter"}:
        return {**result, "status": "custom_default_preserved"}
    if any(not re.search(rb"(?m)^\*" + key + rb"[ \t]+A4(?:/[^:\r\n]*)?:", original)
           for key in (b"PageSize", b"PageRegion")):
        return {**result, "status": "a4_not_supported"}
    attributes = _ready_paper_attributes(cups, device)
    reported_uuid = _single_keyword(attributes.get("printer-uuid"))
    if not reported_uuid or reported_uuid.lower() != "urn:uuid:" + device["identity"]:
        raise ValueError("IPP 打印機身份與已確認的隊列不符，未更改紙張預設。")
    if (_single_keyword(attributes.get("media-ready")) != "iso_a4_210x297mm"
            or _single_keyword(attributes.get("media-default")) != "na_letter_8.5x11in"):
        return {**result, "status": "ready_a4_not_confirmed"}
    pending = cups._call(lambda connection: connection.getJobs(
        which_jobs="not-completed", my_jobs=False, requested_attributes=["job-printer-uri"]))
    if not isinstance(pending, dict) or any(not isinstance(job, dict) or not job.get("job-printer-uri")
                                            for job in pending.values()):
        return {**result, "status": "pending_jobs_unknown"}
    if any(unquote(str(job["job-printer-uri"]).rsplit("/", 1)[-1]).lower() == queue.lower()
           for job in pending.values()):
        return {**result, "status": "pending_jobs"}
    result.update(needs_configuration=True, status="align_a4")
    if not apply:
        return result
    latest = cups._call(lambda connection: connection.getPrinters())
    if queue not in latest or latest[queue].get("device-uri") != device["uri"]:
        raise ValueError("打印模式的連線已變更，未更改紙張預設。")
    if _read_queue_ppd(cups, queue) != original:
        raise ValueError("打印機紙張設定已被更改，請重新檢查；未覆寫既有設定。")
    patched = PAPER_DEFAULT.sub(lambda match: match.group(1) + b"A4" + match.group(4), original)
    with tempfile.TemporaryDirectory(prefix="fpc-paper-default-") as directory:
        backup, updated = Path(directory) / "original.ppd", Path(directory) / "a4.ppd"
        backup.write_bytes(original)
        updated.write_bytes(patched)
        cups.check_ppd(updated)
        try:
            cups.modify_printer(queue, ppd_file=updated)
            if _read_queue_ppd(cups, queue) != patched:
                raise CupsError("CUPS 未保留已確認的紙張預設。")
        except Exception as error:
            try:
                cups.modify_printer(queue, ppd_file=backup)
                if _read_queue_ppd(cups, queue) != original:
                    raise CupsError("CUPS 未保留回復的原有紙張設定。")
            except Exception as rollback_error:
                raise CupsError("紙張設定失敗且未能回復原有 PPD；請管理員檢查 CUPS。") from rollback_error
            raise CupsError("紙張設定失敗，已回復原有 PPD；請管理員檢查後再試。") from error
    return {**result, "needs_configuration": False, "changed": True, "status": "aligned"}


def plan_profiles(cups, discovery, names, align_ready_paper=False):
    installed = cups.ppds()
    index = build_driver_index(installed)
    current = cups._call(lambda connection: connection.getPrinters())
    profiles = cups._profile_records()
    snapshot = discovery.snapshot()
    if snapshot["scanning"]:
        raise ValueError("請等待打印機探索完成。")
    devices = [discovery.get(item["id"]) for item in snapshot["devices"]]
    plans = []
    for name in names:
        if name not in current:
            raise ValueError(f"找不到指定的家庭打印隊列：{name}")
        identity = _dnssd_identity(current[name].get("device-uri"))
        matches = [device for device in devices if identity and device.get("identity") == identity
                   and _confirmed_device(device)]
        if len(matches) != 1:
            raise ValueError(f"{name} 尚未探索到唯一且已確認的 IPP 打印機，請確認已開機。")
        device = matches[0]
        if current[name].get("device-uri") != device["uri"]:
            raise ValueError(f"{name} 的現有連線與已確認的打印機不符，已停止遷移。")
        existing = profiles.get(name)
        if existing:
            if any(queue not in current or current[queue].get("device-uri") != device["uri"]
                   for queue in existing["queues"].values()):
                raise ValueError(f"{name} 已登記的模式隊列不完整，請管理員先檢查。")
        elif not re.search(r"driverless|IPP Everywhere", str(current[name].get("printer-make-and-model") or ""), re.I):
            raise ValueError(f"{name} 已有其他驅動設定，請透過管理後台保留並管理該驅動。")
        driver = automatic_driver(device, index, installed)
        if driver is None:
            raise ValueError(f"{name} 找不到已確認相容的打印模式。")
        queues = dict(existing["queues"]) if existing else {"driverless": name}
        operation = "keep"
        if "driver" not in queues and driver != "everywhere":
            target = companion_name(name, "driver")
            if target.lower() in {queue.lower() for queue in current}:
                raise ValueError(f"{name} 的輔助隊列名稱已存在，已停止遷移。")
            queues["driver"] = target
            operation = "add_driver"
        plan = {"name": name, "queues": queues, "default_mode": "driver" if "driver" in queues else "driverless",
                      "recommended_driver": driver, "driver_label": installed.get(driver, {}).get("ppd-make-and-model") or "免驅動 IPP（相容）",
                      "operation": operation, "device": device,
                      "align_ready_paper": align_ready_paper,
                      "candidates": driver_candidates(device, index, installed)}
        plan["duplexer"] = configure_generic_duplexer(cups, plan)
        plan["paper_defaults"] = configure_ready_paper(cups, plan)
        plans.append(plan)
    return plans


def apply_profiles(cups, plans):
    for plan in plans:
        if plan["operation"] == "add_driver":
            cups.modify_profiled_printer(plan["name"], driver=plan["recommended_driver"], driverless=True,
                                         ipp_uri=plan["device"]["ipp_uri"])
        else:
            cups.register_profiles(plan["name"], plan["queues"], plan["default_mode"])
        plan["duplexer"] = configure_generic_duplexer(cups, plan, apply=True)
        plan["paper_defaults"] = configure_ready_paper(cups, plan, apply=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="準備家庭打印機的驅動與 IPP 相容模式；預設只檢查。")
    parser.add_argument("--apply", action="store_true", help="建立固定驅動隊列並儲存模式映射（不試印）")
    parser.add_argument("--printer", action="append", help="只處理指定既有 DNS-SD 隊列；預設兩台家庭隊列")
    parser.add_argument("--align-ready-paper", action="store_true",
                        help="核對 Epson 實際 A4 裝紙，把未調整的 Letter 隊列預設對齊 A4；需搭配 --apply 才更改")
    args = parser.parse_args(argv)
    cups = CupsClient()
    cups.configure_profiles(settings()["DATA_DIR"])
    discovery = Discovery(seconds=8, sources=[cups.devices])
    discovery.start()
    deadline = time.monotonic() + 60
    while discovery.snapshot()["scanning"]:
        if time.monotonic() > deadline:
            raise TimeoutError("打印機探索逾時，未更改設定。")
        time.sleep(0.2)
    plans = plan_profiles(cups, discovery, args.printer or list(HOUSEHOLD_PRINTERS),
                          align_ready_paper=args.align_ready_paper)
    if args.apply:
        apply_profiles(cups, plans)
    public = [{key: value for key, value in plan.items() if key != "device"} for plan in plans]
    print(json.dumps({"applied": args.apply, "application_restart_required": args.apply,
                      "printers": public, "print_jobs_submitted": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
