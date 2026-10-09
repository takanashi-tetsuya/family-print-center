import time
from types import SimpleNamespace

import pytest

from print_center.discovery import Discovery, SERVICES, device_from_info, parse_avahi, unescape_avahi


def service_info(address="192.168.8.40", server="Epson.local.", properties=None):
    return SimpleNamespace(properties=properties or {b"ty": b"Epson", b"rp": b"ipp/print",
                                                     b"pdl": b"application/pdf,image/pwg-raster"},
                           server=server, port=631, parsed_addresses=lambda: [address])


def test_ipp_discovery_keeps_service_name_so_dhcp_changes_do_not_break_queue():
    name = "Epson Living Room._ipp._tcp.local."
    first = device_from_info(SERVICES[0], name, service_info("192.168.8.40"))
    second = device_from_info(SERVICES[0], name, service_info("192.168.8.100"))
    assert first["id"] == second["id"]
    assert first["uri"] == second["uri"] == "dnssd://Epson%20Living%20Room._ipp._tcp.local./"
    assert first["ipp_uri"] == "ipp://Epson.local:631/ipp/print"
    assert first["driverless"] is True
    assert "192.168" not in first["uri"]


@pytest.mark.parametrize("address,server", [("8.8.8.8", "Printer.local."),
                                          ("192.168.1.2", "external.example.com.")])
def test_public_or_non_local_printer_advertisements_are_ignored(address, server):
    assert device_from_info(SERVICES[0], "Printer._ipp._tcp.local.", service_info(address, server)) is None


def test_legacy_printer_does_not_offer_driverless_without_capability():
    value = device_from_info(SERVICES[2], "Brother._pdl-datastream._tcp.local.", service_info())
    assert value["kind"] == "legacy"
    assert value["driverless"] is False


def test_discovered_resource_is_escaped_before_making_printer_request():
    value = device_from_info(SERVICES[0], "Printer._ipp._tcp.local.",
                             service_info(properties={b"rp": b"ipp/print?payload=#bad", b"pdl": b"application/pdf"}))
    assert value["ipp_uri"] == "ipp://Epson.local:631/ipp/print%3Fpayload%3D%23bad"


def test_device_registration_requires_completed_recent_scan():
    discovery = Discovery()
    discovery.devices = {"example": {"id": "example", "label": "Epson"}}
    with pytest.raises(ValueError, match="過期"):
        discovery.get("example")
    discovery.last_scan = time.time()
    discovery.scanning = True
    with pytest.raises(ValueError, match="完成"):
        discovery.get("example")
    discovery.scanning = False
    with pytest.raises(ValueError, match="找不到"):
        discovery.get("arbitrary-uri")
    assert discovery.get("example")["label"] == "Epson"


def test_discovery_response_hides_internal_connection_data():
    discovery = Discovery()
    discovery.devices = {"example": {"id": "example", "label": "Epson", "ipp_uri": "private URI",
                                       "identity": "internal identity", "priority": 0}}
    assert discovery.snapshot()["devices"] == [{"id": "example", "label": "Epson"}]


def test_avahi_output_parses_escaped_service_names_and_capabilities():
    output = r'=;br-lan;IPv4;Epson\032Living\032Room;_ipp._tcp;local;Epson.local;192.168.8.40;631;"ty=Epson Home" "rp=ipp/print" "pdl=image/pwg-raster,application/pdf" "UUID=ABC-123"'
    devices = parse_avahi(output)
    assert len(devices) == 1
    assert devices[0]["service_name"] == "Epson Living Room"
    assert devices[0]["label"] == "Epson Home"
    assert devices[0]["identity"] == "abc-123"
    assert devices[0]["driverless"] is True


def test_avahi_non_printers_and_unresolved_entries_are_ignored():
    output = '\n'.join([
        '=;eth0;IPv4;Web Server;_http._tcp;local;Web.local;192.168.8.10;80;"path=/"',
        '+;eth0;IPv4;Epson;_ipp._tcp;local',
        '=;eth0;IPv4;Epson;_ipp._tcp;local;Epson.local;8.8.8.8;631;"pdl=application/pdf"',
    ])
    assert parse_avahi(output) == []


def test_avahi_decodes_utf8_without_treating_decimal_escapes_as_code():
    assert unescape_avahi(r"\229\174\182\229\186\173\032Printer") == "家庭 Printer"


def test_scan_deduplicates_same_printer_advertised_multiple_ways(monkeypatch):
    output = '\n'.join([
        '=;br-lan;IPv4;Brother;_printer._tcp;local;Brother.local;192.168.8.30;515;"UUID=ONE"',
        '=;br-lan;IPv4;Brother;_ipp._tcp;local;Brother.local;192.168.8.30;631;"UUID=ONE" "pdl=application/pdf"',
    ])
    monkeypatch.setattr("print_center.discovery.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output))
    discovery = Discovery()
    discovery.scanning = True
    discovery._scan()
    assert len(discovery.devices) == 1
    assert list(discovery.devices.values())[0]["kind"] == "ipp"
    assert discovery.scanning is False
    assert discovery.last_scan > 0


@pytest.mark.parametrize("device_id", [None, [], {}])
def test_device_identifier_requires_a_string(device_id):
    with pytest.raises(ValueError):
        Discovery().get(device_id)
