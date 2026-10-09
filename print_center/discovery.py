"""Browse local DNS-SD printers; preserve service names instead of DHCP addresses."""
import hashlib
import ipaddress
import re
import shlex
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote

SERVICES = ["_ipp._tcp.local.", "_ipps._tcp.local.", "_pdl-datastream._tcp.local.", "_printer._tcp.local."]


def device_from_info(service_type, name, info):
    properties = {key.decode("utf-8", "replace").lower(): (value or b"").decode("utf-8", "replace")
                  for key, value in info.properties.items()}
    addresses = info.parsed_addresses()
    # Only devices on a private/link-local network are offered to the web app.
    if not any(ipaddress.ip_address(address.split("%", 1)[0]).is_private
               or ipaddress.ip_address(address.split("%", 1)[0]).is_link_local for address in addresses):
        return None
    label = properties.get("ty") or properties.get("product", "").strip("()") or name.removesuffix("." + service_type)
    instance = name.removesuffix("." + service_type)
    uri = "dnssd://" + quote(name, safe=".") + "/"
    uuid = properties.get("uuid")
    if uuid:
        uri += "?uuid=" + quote(uuid, safe="")
    ipp = service_type in SERVICES[:2]
    pdl = properties.get("pdl", "").lower()
    driverless = ipp and (bool(properties.get("urf")) or any(fmt in pdl for fmt in ("image/pwg-raster", "application/pdf", "application/pclm")))
    resource = properties.get("rp", "ipp/print").lstrip("/")
    server = (info.server or "").rstrip(".")
    # Hostnames learned from mDNS only; never accept arbitrary user-supplied URLs.
    if not server or not server.lower().endswith(".local"):
        return None
    # Bonjour printers repeat their USB identity in TXT records; drivers are matched on it.
    usb = {field: properties.get(f"usb_{field.lower()}", "").strip() for field in ("MFG", "MDL", "CMD")}
    device_id = "".join(f"{field}:{value};" for field, value in usb.items() if value and ";" not in value)
    return {"id": hashlib.sha256(name.encode()).hexdigest()[:24],
            "label": label[:200], "service_name": instance[:200],
            "kind": "ipps" if service_type == SERVICES[1] else "ipp" if ipp else "legacy",
            "uri": uri, "driverless": driverless, "model_hint": properties.get("product", label)[:200],
            "make_model": (properties.get("ty") or label)[:200], "device_id": device_id[:500], "pdl": pdl[:500],
            "ipp_uri": f"{'ipps' if service_type == SERVICES[1] else 'ipp'}://{server}:{info.port}/{quote(resource, safe='/')}",
            "identity": uuid.lower() if uuid else server.lower(), "priority": SERVICES.index(service_type)}


class Discovery:
    def __init__(self, seconds=8, sources=()):
        self.seconds = seconds
        # Extra callables (such as CUPS USB/SNMP backends) run beside Avahi.
        self.sources = list(sources)
        self.lock = threading.RLock()
        self.devices = {}
        self.scanning = False
        self.error = None
        self.last_scan = 0

    def snapshot(self):
        with self.lock:
            devices = [{key: value for key, value in device.items()
                        if key not in {"ipp_uri", "identity", "priority"}}
                       for device in self.devices.values()]
            return {"devices": devices, "scanning": self.scanning, "error": self.error}

    def get(self, device_id):
        with self.lock:
            if not isinstance(device_id, str):
                raise ValueError("請選擇已探索到的打印機。")
            if self.scanning:
                raise ValueError("請等待探索完成後再新增打印機。")
            if time.time() - self.last_scan > 300:
                raise ValueError("探索結果已過期，請重新掃描。")
            if device_id not in self.devices:
                raise ValueError("找不到此打印機，請重新掃描。")
            return dict(self.devices[device_id])

    def start(self):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
            self.error = None
        threading.Thread(target=self._scan, daemon=True, name="printer-discovery").start()

    def _extra(self, source, results):
        try:
            results.extend(source())
        except Exception:
            pass

    def _scan(self):
        extra = []
        workers = [threading.Thread(target=self._extra, args=(source, extra), daemon=True) for source in self.sources]
        for worker in workers:
            worker.start()
        found = {}
        try:
            try:
                process = subprocess.run(["avahi-browse", "--all", "--resolve", "--terminate", "--parsable", "--no-db-lookup"],
                                         capture_output=True, text=True, timeout=self.seconds + 5, check=False)
                if process.returncode:
                    raise RuntimeError("Avahi discovery failed")
                for device in parse_avahi(process.stdout):
                    previous = found.get(device["identity"])
                    if not previous or device["priority"] < previous["priority"]:
                        found[device["identity"]] = device
            except Exception:
                with self.lock:
                    self.error = "無法自動尋找網路打印機，請確認打印機已開機並與路由器在同一個網路。也可以改用下方「輸入 IP 位址」。"
            for worker in workers:
                worker.join(30)
            for device in list(extra):
                device = dict(device, id=hashlib.sha256(("device:" + device["uri"]).encode()).hexdigest()[:24])
                found.setdefault("device:" + device["uri"], device)
            with self.lock:
                self.devices = {device["id"]: device for device in found.values()}
        finally:
            with self.lock:
                self.scanning = False
                self.last_scan = time.time()


def unescape_avahi(value):
    # Parsable mode escapes separators and UTF-8 bytes as decimal sequences.
    output = bytearray()
    position = 0
    while position < len(value):
        match = re.match(r"\\(\d{3})", value[position:])
        if match:
            output.append(int(match.group(1)))
            position += 4
        else:
            output.extend(value[position].encode())
            position += 1
    return output.decode("utf-8", "replace")


def parse_avahi(output):
    devices = []
    for line in output.splitlines():
        fields = line.split(";", 9)
        if len(fields) != 10 or fields[0] != "=":
            continue
        fields = [unescape_avahi(field) for field in fields]
        _, interface, protocol, instance, kind, domain, server, address, port, txt = fields
        service_type = f"{kind}.{domain}."
        if service_type not in SERVICES:
            continue
        try:
            properties = {}
            for entry in shlex.split(txt):
                key, separator, value = entry.partition("=")
                if separator:
                    properties[key.encode()] = value.encode()
            info = SimpleNamespace(properties=properties, server=server, port=int(port),
                                   parsed_addresses=lambda address=address: [address])
            device = device_from_info(service_type, f"{instance}.{service_type}", info)
            if device:
                devices.append(device)
        except (ValueError, OverflowError):
            continue
    return devices
