import pytest
from omini_sdk import Config, PluginError

from omini_unifi.collect import collect, mac
from omini_unifi.collect import test as connection_test


def by_key(devices):
    return {d.key: d for d in devices}


def test_one_device_per_adopted_device(unifi, cfg):
    devices = by_key(collect(cfg))
    # The disconnected AP (state 0) is left out: its data would be stale.
    assert list(devices) == [
        "74:ac:b9:00:00:01",
        "f4:92:bf:00:00:10",
        "78:45:58:00:00:20",
        "78:45:58:00:00:30",
    ]
    roles = {d.name: d.role for d in devices.values()}
    assert roles == {
        "Dream Machine Pro": "router",
        "Rack Switch": "switch",
        "Office AP": "ap",
        "Garden AP": "ap",
    }
    udm = devices["74:ac:b9:00:00:01"]
    assert (udm.vendor, udm.model, udm.serial) == ("Ubiquiti", "UDMPRO", "74ACB9000001")
    # Reached at its LAN address; stat/device gives the WAN one.
    assert udm.host == "192.168.1.1"
    assert udm.ips == ["203.0.113.10", "192.168.1.1", "192.168.20.1", "192.168.30.1"]
    assert [m.root for m in udm.macs] == ["74:ac:b9:00:00:01", "74:ac:b9:00:00:02"]
    assert (udm.uptime_s, udm.cpu_pct, udm.mem_pct) == (864000, 12.5, 61.3)
    assert udm.load_avg == [0.42, 0.38, 0.35]


def test_health_and_firmware(unifi, cfg):
    devices = by_key(collect(cfg))
    udm = devices["74:ac:b9:00:00:01"]
    assert [(t.sensor, t.kind, t.celsius) for t in udm.temperatures] == [
        ("CPU", "cpu", 58.2),
        ("Local", "board", 44.0),
        ("PHY", "other", 61.5),
    ]
    assert [(s.mount, s.total_bytes, s.used_bytes) for s in udm.storage] == [
        ("/volume1", 2000000000000, 500000000000)
    ]
    fw = udm.firmware
    assert (fw.current, fw.latest, fw.update_available, fw.updates) == (
        "4.0.21.12345",
        "4.1.13.23456",
        True,
        1,
    )
    switch = devices["f4:92:bf:00:00:10"]
    assert switch.firmware.update_available is False and switch.firmware.latest is None
    assert [(t.sensor, t.celsius) for t in switch.temperatures] == [("General", 52.0)]
    garden = devices["78:45:58:00:00:30"]
    assert garden.cpu_pct is None and garden.firmware.update_available is None


def test_ports(unifi, cfg):
    switch = by_key(collect(cfg))["f4:92:bf:00:00:10"]
    ports = {i.name: i for i in switch.interfaces}
    assert list(ports) == ["Port 1", "Office AP", "Port 3", "SFP 1"]
    p1 = ports["Port 1"]
    assert (p1.up, p1.speed_mbps, p1.duplex, p1.connector) == (True, 1000, "full", "rj45")
    assert (p1.rx_bytes, p1.tx_bytes, p1.tx_errors) == (1111, 2222, 1)
    assert ports["Port 3"].duplex == "half" and ports["Port 3"].speed_mbps == 100
    sfp = ports["SFP 1"]
    assert sfp.connector == "sfp" and sfp.media == "SFP"
    t = sfp.transceiver
    assert (t.vendor, t.part, t.temperature_c, t.tx_power_dbm, t.rx_power_dbm) == (
        "Ubiquiti Inc.",
        "UF-MM-1G",
        38.5,
        -5.2,
        -6.8,
    )
    udm = by_key(collect(cfg))["74:ac:b9:00:00:01"]
    down = next(i for i in udm.interfaces if i.name == "Port 2")
    assert (down.up, down.speed_mbps, down.duplex) == (False, None, None)


def test_wan_ports_and_gateways(unifi, cfg):
    udm = by_key(collect(cfg))["74:ac:b9:00:00:01"]
    ifaces = {i.name: i for i in udm.interfaces}
    wan = ifaces["Port 9"]
    assert (wan.wan, wan.description, wan.speed_mbps) == (True, "WAN", 1000)
    assert wan.ips == ["203.0.113.10/24"]
    # WAN 2 is not in port_table: added from wan2.
    wan2 = ifaces["eth9"]
    assert (wan2.wan, wan2.description, wan2.up, wan2.ips) == (
        True,
        "WAN 2",
        False,
        ["198.51.100.20/30"],
    )
    assert not ifaces["Port 1"].wan
    gws = [(g.name, g.interface, g.address, g.status, g.rtt_ms) for g in udm.gateways]
    assert gws == [
        ("WAN", "Port 9", "203.0.113.1", "up", 9.0),
        ("WAN 2", "eth9", "198.51.100.21", "down", None),
    ]
    # The gateway's uplink is the internet: no neighbor for it.
    assert udm.neighbors is None


def test_vlans_on_the_gateway(unifi, cfg):
    devices = by_key(collect(cfg))
    udm = devices["74:ac:b9:00:00:01"]
    assert [(v.id, v.name, v.subnet) for v in udm.vlans] == [
        (20, "IOT", "192.168.20.0/24"),
        (30, "Guests", "192.168.30.0/24"),
    ]
    assert devices["f4:92:bf:00:00:10"].vlans is None


def test_lldp_and_uplinks(unifi, cfg):
    devices = by_key(collect(cfg))
    switch = devices["f4:92:bf:00:00:10"]
    # LLDP already names the uplink to the UDM: no second neighbor for it.
    assert [(n.local_port, n.protocol, n.remote_mac, n.remote_port) for n in switch.neighbors] == [
        ("SFP 1", "lldp", "74:ac:b9:00:00:01", "eth10")
    ]
    office = devices["78:45:58:00:00:20"]
    [up] = office.neighbors
    assert (up.local_port, up.protocol, up.remote_mac, up.remote_port, up.remote_name) == (
        "Main",
        "other",
        "f4:92:bf:00:00:10",
        "Office AP",  # the switch's port 2, by its name
        "Rack Switch",
    )
    [mesh] = devices["78:45:58:00:00:30"].neighbors
    assert (mesh.local_port, mesh.remote_mac, mesh.remote_port) == (
        "Wi-Fi uplink",
        "78:45:58:00:00:20",
        "5 GHz",
    )


def test_wifi_clients_and_networks(unifi, cfg):
    devices = by_key(collect(cfg))
    office = devices["78:45:58:00:00:20"]
    [phone] = office.wireless_clients
    assert phone.mac == "aa:bb:cc:00:00:01"
    assert (phone.interface, phone.ssid, phone.band, phone.signal_dbm) == (
        "Home · 5 GHz",
        "Home",
        "5ghz",
        -58,
    )
    assert (phone.tx_rate_mbps, phone.rx_rate_mbps) == (1201.0, 864.7)
    # The AP sends 250 kB/s to the phone: the phone downloads 2 Mbit/s.
    assert (phone.rx_bps, phone.tx_bps) == (2_000_000, 100_000)
    wifi = {i.name: i.description for i in office.interfaces if i.type == "wireless"}
    assert wifi == {"wlan0": "Home · 2.4 GHz", "wlan1": "Home · 5 GHz"}
    [plug] = devices["78:45:58:00:00:30"].wireless_clients
    assert (plug.interface, plug.band, plug.rx_bps) == ("IOT · 2.4 GHz", "2.4ghz", 0)


def test_wired_clients_in_the_mac_table(unifi, cfg):
    devices = by_key(collect(cfg))
    switch = devices["f4:92:bf:00:00:10"]
    assert [(f.mac, f.port, f.vlan) for f in switch.fdb] == [("aa:bb:cc:00:00:03", "Port 3", 1)]
    udm = devices["74:ac:b9:00:00:01"]
    assert [(f.mac, f.port) for f in udm.fdb] == [("aa:bb:cc:00:00:04", "Port 1")]


def test_client_names_as_hosts(unifi, cfg):
    udm = by_key(collect(cfg))["74:ac:b9:00:00:01"]
    hosts = {h.mac: (h.ip, h.hostnames, h.sources) for h in udm.hosts}
    assert hosts["aa:bb:cc:00:00:01"] == ("192.168.1.50", ["Ricardo's phone", "pixel-8"], ["unifi"])
    assert hosts["aa:bb:cc:00:00:04"] == ("192.168.1.21", ["Desktop"], ["unifi"])
    assert "aa:bb:cc:00:00:05" not in hosts  # no IP yet


def test_self_hosted_application(classic, cfg):
    devices = collect(cfg)
    assert len(devices) == 4
    assert ("POST", "/api/login") in classic.calls
    assert all(not p.startswith("/proxy") for _, p in classic.calls)
    assert connection_test(cfg).startswith('Connected to UniFi Network Application, site "default"')


def test_session_is_reused(unifi, cfg):
    collect(cfg)
    collect(cfg)
    assert unifi.logins == 1
    assert sum(1 for _, p in unifi.calls if p == "/") == 1  # flavor detected once
    unifi.expire_sessions()
    assert len(collect(cfg)) == 4  # signs in again
    assert unifi.logins == 2


def test_session_file_has_no_password(unifi, cfg):
    collect(cfg)
    saved = (cfg.state_dir / "session.json").read_text()
    assert "s3cret" not in saved and "unifi_os" in saved


def test_connection_test(unifi, cfg):
    assert connection_test(cfg) == 'Connected to UniFi OS, site "default": 4 devices'


def test_wrong_credentials(unifi, cfg):
    cfg["password"] = "wrong"
    with pytest.raises(PluginError, match="rejected the username or password") as e:
        collect(cfg)
    assert "wrong" not in str(e.value)
    # A second try within minutes does not hit the controller again.
    calls = len(unifi.calls)
    with pytest.raises(PluginError, match="waiting"):
        collect(cfg)
    assert len(unifi.calls) == calls


def test_wrong_credentials_self_hosted(classic, cfg):
    cfg["password"] = "wrong"
    with pytest.raises(PluginError, match="rejected the username or password"):
        connection_test(cfg)


def test_unknown_site(unifi, cfg):
    cfg["site"] = "branch"
    with pytest.raises(PluginError, match='no site "branch"'):
        collect(cfg)


def test_optional_endpoints_failing(unifi, cfg):
    unifi.forbidden = {"stat/sta", "rest/networkconf"}
    del unifi.routes["stat/health"]
    devices = by_key(collect(cfg))
    assert len(devices) == 4
    udm = devices["74:ac:b9:00:00:01"]
    assert udm.vlans is None and udm.hosts is None and udm.fdb is None
    assert udm.gateways[0].status == "up" and udm.gateways[0].rtt_ms is None
    assert connection_test(cfg) == (
        'Connected to UniFi OS, site "default": 4 devices. '
        "Could not read: clients, networks (VLANs), WAN status"
    )


def test_not_a_controller(cfg, monkeypatch):
    import httpx

    import omini_unifi.collect as module
    from omini_unifi.client import Client

    def handler(request):
        return httpx.Response(404, text="not here")

    monkeypatch.setattr(
        module, "Client", lambda *a, **kw: Client(*a, **kw, transport=httpx.MockTransport(handler))
    )
    with pytest.raises(PluginError, match="does not look like"):
        collect(cfg)


def test_required_fields():
    with pytest.raises(PluginError, match="required"):
        collect(Config({"url": "https://192.168.1.1"}))


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        ("aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff"),
        ("aabbccddeeff", "aa:bb:cc:dd:ee:ff"),
        ("00:00:00:00:00:00", None),
        ("nope", None),
        (None, None),
    ],
)
def test_mac_normalization(raw, want):
    assert mac(raw) == want


def test_client_macs_are_normalized(unifi, cfg):
    udm = by_key(collect(cfg))["74:ac:b9:00:00:01"]
    assert "aa:bb:cc:00:00:04" in {f.mac for f in udm.fdb}  # was aa-bb-cc-00-00-04
