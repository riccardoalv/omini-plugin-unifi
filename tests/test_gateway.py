import json

import httpx
import pytest
from omini_sdk import Change, PluginError

import omini_unifi.collect as collect_module
import omini_unifi.gateway as gateway_module
from omini_unifi.client import Client
from omini_unifi.collect import apply, collect

UDM = "74:ac:b9:00:00:01"
SWITCH = "f4:92:bf:00:00:10"


@pytest.fixture
def site(unifi, monkeypatch):
    """The controller, keeping what is written to rest/user, rest/networkconf
    and the v2 API (traffic rules, static DNS)."""
    monkeypatch.setattr(gateway_module, "VERIFY_S", 0)
    monkeypatch.setattr(gateway_module, "POLL_S", 0)
    unifi.sent = []
    unifi.ignore = False  # answers ok to a write but keeps nothing
    unifi.view_only = False
    unifi.no_v2 = False  # an older controller: the v2 API does not exist
    inner = unifi.handler
    ok = {"meta": {"rc": "ok"}}

    def rest(method, endpoint, body):
        collection, _, _id = (
            endpoint.rpartition("/") if endpoint.count("/") > 1 else (endpoint, "", "")
        )
        rows = unifi.routes[collection]["data"]
        if method == "POST":
            made = {"_id": f"new{len(unifi.sent)}", **body}
            if not unifi.ignore:
                rows.append(made)
            return httpx.Response(200, json={**ok, "data": [made]})
        row = next((r for r in rows if r["_id"] == _id), None)
        if row is None:
            return httpx.Response(400, json={"meta": {"rc": "error", "msg": "api.err.IdInvalid"}})
        if not unifi.ignore:
            if method == "DELETE":
                rows.remove(row)
            else:
                row.update(body)
        return httpx.Response(200, json={**ok, "data": [row]})

    def v2(method, endpoint, body):
        name, _, _id = endpoint.partition("/")
        rows = unifi.routes.get(f"v2/{name}")
        if unifi.no_v2 or rows is None:
            return httpx.Response(404, text="Not Found")
        if method == "GET":
            return httpx.Response(200, json=rows)
        if unifi.view_only:
            return httpx.Response(403, json={"code": "api.err.NoPermission"})
        unifi.sent.append((method, f"v2/{endpoint}", body))
        if method == "POST":
            made = {"_id": f"new{len(unifi.sent)}", **body}
            if not unifi.ignore:
                rows.append(made)
            return httpx.Response(201, json=made)
        row = next((r for r in rows if r["_id"] == _id), None)
        if row is None:
            return httpx.Response(400, json={"code": "api.err.IdInvalid", "message": "no such id"})
        if not unifi.ignore:
            if method == "DELETE":
                rows.remove(row)
            else:
                row.clear()
                row.update(body)
        return httpx.Response(200, json=row if method != "DELETE" else {})

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if "/v2/api/site/default/" in path:
            return v2(method, path.split("/v2/api/site/default/", 1)[1], body)
        endpoint = path.split("/api/s/default/", 1)[-1]
        writes = ("rest/user", "rest/networkconf")
        if method in ("POST", "PUT", "DELETE") and endpoint.startswith(writes):
            check = inner(httpx.Request("GET", request.url, headers=request.headers))
            if check.status_code == 401:
                return check
            if unifi.view_only:
                return httpx.Response(
                    403, json={"meta": {"rc": "error", "msg": "api.err.NoPermission"}, "data": []}
                )
            unifi.sent.append((method, endpoint, body))
            return rest(method, endpoint, body)
        return inner(request)

    monkeypatch.setattr(
        collect_module,
        "Client",
        lambda *a, **k: Client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    return unifi


def gateway(devices):
    return next(d for d in devices if d.key == UDM)


def dump(items):
    return [x.model_dump(exclude_none=True, by_alias=True) for x in items or []]


def change(kind, **value):
    return Change(device=UDM, kind=kind, value=value)


# --- reads ----------------------------------------------------------------------


def test_reads_the_gateway_s_networks(site, cfg):
    udm = gateway(collect(cfg))
    assert dump(udm.networks) == [
        {
            "vlan": 1,
            "name": "Default",
            "gateway": "192.168.1.1/24",
            "dhcp": {"enabled": True, "from": "192.168.1.100", "to": "192.168.1.250"},
            "access": "all",
        },
        {
            "vlan": 20,
            "name": "IOT",
            "gateway": "192.168.20.1/24",
            "dhcp": {"enabled": True, "from": "192.168.20.100", "to": "192.168.20.250"},
            "access": "internet",
        },
    ]
    assert udm.dns_domain == "home.arpa"
    # Only the gateway serves them.
    assert all(d.networks is None for d in collect(cfg) if d.key != UDM)


def test_access_is_unknown_without_the_controller_s_switches(site, cfg):
    for n in site.routes["rest/networkconf"]["data"]:
        n.pop("internet_access_enabled", None)
        n.pop("network_isolation_enabled", None)
    assert all(n.access is None for n in gateway(collect(cfg)).networks)


def test_reads_fixed_addresses_and_dhcp_clients(site, cfg):
    udm = gateway(collect(cfg))
    # A client whose fixed address is off is not a reservation.
    assert dump(udm.dhcp_reservations) == [
        {"mac": "aa:bb:cc:00:00:03", "ip": "192.168.1.20", "name": "NAS"}
    ]
    assert dump(udm.dhcp_leases) == [
        {"ip": "192.168.1.50", "mac": "aa:bb:cc:00:00:01", "hostname": "pixel-8"},
        {"ip": "192.168.20.60", "mac": "aa:bb:cc:00:00:02", "hostname": "plug-kitchen"},
        {"ip": "192.168.1.20", "mac": "aa:bb:cc:00:00:03", "hostname": "nas"},
        {"ip": "192.168.1.21", "mac": "aa:bb:cc:00:00:04"},
    ]


def test_reads_the_static_dns_names(site, cfg):
    # Enabled A and AAAA records; a CNAME and a disabled record are left out.
    assert dump(gateway(collect(cfg)).dns_records) == [
        {"name": "nas.home.arpa", "ip": "192.168.1.20"},
        {"name": "nas.home.arpa", "ip": "fd00::20"},
    ]


def test_reads_the_paused_devices(site, cfg):
    # Omini's rule, and a simple internet block set by hand; a schedule, an
    # app block and a whole network's block are not pauses.
    assert dump(gateway(collect(cfg)).paused) == [
        {"mac": "aa:bb:cc:00:00:02", "ips": ["192.168.20.60"], "name": "plug-kitchen"},
        {"mac": "aa:bb:cc:00:00:07", "rule": "Kids bedtime"},
    ]


def test_reads_the_port_forwards(site, cfg):
    site.device("6501a1b2c3d4e5f600000001")["wan1"]["ifname"] = "eth8"
    assert dump(gateway(collect(cfg)).port_forwards) == [
        {
            "protocol": "tcp",
            "external_port": 32400,
            "internal_ip": "192.168.1.20",
            "internal_port": 32400,
            "interface": "eth8",
            "description": "Plex",
            "enabled": True,
            "upnp": False,
        },
        {
            "protocol": "tcp/udp",
            "external_port": 27015,
            "external_port_end": 27030,
            "internal_ip": "192.168.1.21",
            "internal_port": 27015,
            "source": "198.51.100.0/24",
            "description": "Game",
            "enabled": False,
            "upnp": False,
        },
        {
            "protocol": "tcp",
            "external_port": 80,
            "internal_ip": "192.168.1.20",
            "internal_port": 8080,
            "interface": "eth9",
            "description": "Web",
            "enabled": True,
            "upnp": False,
        },
        {
            "protocol": "tcp",
            "external_port": 443,
            "internal_ip": "192.168.1.20",
            "internal_port": 8443,
            "interface": "eth9",
            "description": "Web",
            "enabled": True,
            "upnp": False,
        },
    ]


def test_an_older_controller_still_collects(site, cfg):
    site.no_v2 = True
    del site.routes["rest/portforward"]
    site.forbidden.add("rest/user")
    udm = gateway(collect(cfg))
    assert udm.networks  # networkconf is still read
    assert (udm.paused, udm.dns_records, udm.dhcp_reservations, udm.port_forwards) == (
        None,
        None,
        None,
        None,
    )


# --- reservations -----------------------------------------------------------------


def test_fixes_a_known_client_s_address(site, cfg):
    udm = gateway(apply(cfg, change("reservation.set", mac="AA:BB:CC:00:00:01", ip="192.168.1.50")))
    assert site.sent == [
        (
            "PUT",
            "rest/user/u101",
            {"use_fixedip": True, "fixed_ip": "192.168.1.50", "network_id": "n1"},
        )
    ]
    assert {"mac": "aa:bb:cc:00:00:01", "ip": "192.168.1.50", "name": "Ricardo's phone"} in dump(
        udm.dhcp_reservations
    )


def test_fixes_the_address_of_a_client_never_seen(site, cfg):
    apply(cfg, change("reservation.set", mac="aa:bb:cc:00:00:09", ip="192.168.20.9", name="cam"))
    assert site.sent == [
        (
            "POST",
            "rest/user",
            {
                "mac": "aa:bb:cc:00:00:09",
                "name": "cam",
                "use_fixedip": True,
                "fixed_ip": "192.168.20.9",
                "network_id": "n2",
            },
        )
    ]


def test_a_reservation_not_kept_is_undone(site, cfg):
    site.ignore = True
    with pytest.raises(PluginError, match="undone on the controller"):
        apply(cfg, change("reservation.set", mac="aa:bb:cc:00:00:01", ip="192.168.1.50"))
    assert site.sent[-1] == ("PUT", "rest/user/u101", {"use_fixedip": False})


def test_a_reservation_outside_the_networks_is_refused(site, cfg):
    with pytest.raises(PluginError, match=r"no network of the gateway holds 10\.0\.0\.5"):
        apply(cfg, change("reservation.set", mac="aa:bb:cc:00:00:01", ip="10.0.0.5"))
    assert site.sent == []


def test_frees_a_fixed_address(site, cfg):
    udm = gateway(apply(cfg, change("reservation.delete", mac="aa:bb:cc:00:00:03")))
    assert site.sent == [("PUT", "rest/user/u100", {"use_fixedip": False})]
    assert udm.dhcp_reservations is None
    apply(cfg, change("reservation.delete", mac="aa:bb:cc:00:00:03"))  # already free
    assert len(site.sent) == 1


# --- networks ----------------------------------------------------------------------


def net(vlan=30, gateway="192.168.40.1/24", **kw):
    value = {
        "vlan": vlan,
        "name": "Cameras",
        "gateway": gateway,
        "dhcp": {"enabled": True, "from": "192.168.40.100", "to": "192.168.40.250"},
        "access": "local",
        **kw,
    }
    return change("network.set", **value)


def test_creates_a_network(site, cfg):
    udm = gateway(apply(cfg, net(vlan=40)))
    assert site.sent == [
        (
            "POST",
            "rest/networkconf",
            {
                "name": "Cameras",
                "ip_subnet": "192.168.40.1/24",
                "dhcpd_enabled": True,
                "dhcpd_start": "192.168.40.100",
                "dhcpd_stop": "192.168.40.250",
                "internet_access_enabled": False,
                "network_isolation_enabled": True,
                "purpose": "corporate",
                "networkgroup": "LAN",
                "vlan_enabled": True,
                "vlan": 40,
                "enabled": True,
            },
        )
    ]
    assert dump(udm.networks)[-1] == {
        "vlan": 40,
        "name": "Cameras",
        "gateway": "192.168.40.1/24",
        "dhcp": {"enabled": True, "from": "192.168.40.100", "to": "192.168.40.250"},
        "access": "local",
    }


def test_changes_a_network_s_dhcp_and_access(site, cfg):
    apply(
        cfg,
        change(
            "network.set",
            vlan=20,
            gateway="192.168.20.1/24",
            dhcp={"enabled": False},
            access="all",
        ),
    )
    assert site.sent == [
        (
            "PUT",
            "rest/networkconf/n2",
            {
                "ip_subnet": "192.168.20.1/24",
                "dhcpd_enabled": False,
                "internet_access_enabled": True,
                "network_isolation_enabled": False,
            },
        )
    ]


def test_the_lan_keeps_its_name(site, cfg):
    apply(
        cfg,
        change(
            "network.set", vlan=1, name="LAN", gateway="192.168.1.1/24", dhcp={"enabled": False}
        ),
    )
    assert site.sent == [
        ("PUT", "rest/networkconf/n1", {"ip_subnet": "192.168.1.1/24", "dhcpd_enabled": False})
    ]


def test_custom_access_is_left_as_it_is(site, cfg):
    apply(
        cfg,
        change(
            "network.set",
            vlan=20,
            gateway="192.168.20.1/24",
            access="custom",
            dhcp={"enabled": True, "from": "192.168.20.100", "to": "192.168.20.250"},
        ),
    )
    assert site.sent == []  # nothing else differs


def test_a_new_network_not_kept_is_deleted(site, cfg):
    site.ignore = True
    with pytest.raises(PluginError, match="undone"):
        apply(cfg, net(vlan=40))


def test_a_changed_network_not_kept_is_put_back(site, cfg):
    site.routes["rest/networkconf"]["data"][1]["dhcpd_enabled"] = True
    original = dict(site.routes["rest/networkconf"]["data"][1])
    site.ignore = True
    with pytest.raises(PluginError, match="undone on the controller"):
        apply(cfg, change("network.set", vlan=20, gateway="192.168.21.1/24"))
    assert site.sent[-1] == (
        "PUT",
        "rest/networkconf/n2",
        {"ip_subnet": original["ip_subnet"], "dhcpd_enabled": True},
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (net(vlan=30), "guest network"),
        (net(vlan=2000), "WAN"),
        (net(gateway="192.168.40.0/24"), "not a host address"),
        (
            net(dhcp={"enabled": True, "from": "10.0.0.1", "to": "10.0.0.9"}),
            "inside 192.168.40.0/24",
        ),
        (net(mdns=True), "mDNS"),
        (net(vlan=0), "1 to 4094"),
    ],
)
def test_network_set_refusals(site, cfg, value, message):
    with pytest.raises(PluginError, match=message):
        apply(cfg, value)
    assert site.sent == []


def test_access_the_controller_cannot_express_is_refused(site, cfg):
    for n in site.routes["rest/networkconf"]["data"]:
        n.pop("internet_access_enabled", None)
        n.pop("network_isolation_enabled", None)
    with pytest.raises(PluginError, match="no network isolation"):
        apply(cfg, net(vlan=40))
    apply(cfg, net(vlan=40, access=None))  # without access it is created
    assert site.sent[0][0] == "POST"


def test_deletes_a_network(site, cfg):
    site.routes["rest/wlanconf"] = {"meta": {"rc": "ok"}, "data": []}
    udm = gateway(apply(cfg, change("network.delete", vlan=20)))
    assert site.sent == [("DELETE", "rest/networkconf/n2", {})]
    assert [n.vlan for n in udm.networks] == [1]


def test_network_delete_refusals(site, cfg):
    site.routes["rest/wlanconf"] = {
        "meta": {"rc": "ok"},
        "data": [{"_id": "w1", "name": "Things", "networkconf_id": "n2"}],
    }
    with pytest.raises(PluginError, match="the Wi-Fi Things"):
        apply(cfg, change("network.delete", vlan=20))
    with pytest.raises(PluginError, match="LAN cannot be removed"):
        apply(cfg, change("network.delete", vlan=1))
    with pytest.raises(PluginError, match="no network on VLAN 30"):
        apply(cfg, change("network.delete", vlan=30))  # a guest network
    assert site.sent == []


# --- DNS ----------------------------------------------------------------------------


def test_sets_a_dns_name(site, cfg):
    udm = gateway(apply(cfg, change("dns.set", name="printer.home.arpa", ip="192.168.1.30")))
    assert site.sent[0][:2] == ("POST", "v2/static-dns")
    assert site.sent[0][2]["key"] == "printer.home.arpa"
    assert {"name": "printer.home.arpa", "ip": "192.168.1.30"} in dump(udm.dns_records)


def test_changes_a_dns_name_s_address(site, cfg):
    apply(cfg, change("dns.set", name="NAS.home.arpa", ip="192.168.1.21"))
    method, endpoint, body = site.sent[0]
    assert (method, endpoint, body["value"], body["key"]) == (
        "PUT",
        "v2/static-dns/d1",
        "192.168.1.21",
        "nas.home.arpa",
    )


def test_deletes_a_dns_name_keeping_its_aaaa_record(site, cfg):
    udm = gateway(apply(cfg, change("dns.delete", name="nas.home.arpa")))
    assert site.sent == [("DELETE", "v2/static-dns/d1", {})]
    assert dump(udm.dns_records) == [{"name": "nas.home.arpa", "ip": "fd00::20"}]


def test_a_dns_name_not_kept_is_undone(site, cfg):
    site.ignore = True
    with pytest.raises(PluginError, match="undone"):
        apply(cfg, change("dns.set", name="nas.home.arpa", ip="192.168.1.21"))
    assert site.sent[-1][0] == "PUT" and site.sent[-1][2]["value"] == "192.168.1.20"


def test_dns_on_a_controller_without_static_dns(site, cfg):
    site.no_v2 = True
    with pytest.raises(PluginError, match=r"8\.2 or later"):
        apply(cfg, change("dns.set", name="nas.home.arpa", ip="192.168.1.21"))


# --- pause --------------------------------------------------------------------------


def test_pauses_a_device_s_internet_with_a_traffic_rule(site, cfg):
    udm = gateway(
        apply(cfg, change("pause.set", mac="AA:BB:CC:00:00:01", ips=["192.168.1.50"], name="Phone"))
    )
    method, endpoint, body = site.sent[0]
    assert (method, endpoint) == ("POST", "v2/trafficrules")
    assert {k: body[k] for k in ("description", "action", "enabled", "matching_target")} == {
        "description": "Omini: pause Phone",
        "action": "BLOCK",
        "enabled": True,
        "matching_target": "INTERNET",  # the internet only: the LAN stays reachable
    }
    assert body["target_devices"] == [{"client_mac": "aa:bb:cc:00:00:01", "type": "CLIENT"}]
    assert body["schedule"] == {"mode": "ALWAYS"}
    assert "aa:bb:cc:00:00:01" in [p.mac for p in udm.paused]
    # Never the client block, which also cuts the LAN.
    assert not any("stamgr" in path for _, path in site.calls)


def test_a_paused_device_is_left_as_it_is(site, cfg):
    apply(cfg, change("pause.set", mac="aa:bb:cc:00:00:02"))
    assert site.sent == []


def test_a_pause_not_kept_is_undone(site, cfg):
    site.ignore = True
    with pytest.raises(PluginError, match="undone"):
        apply(cfg, change("pause.set", mac="aa:bb:cc:00:00:01"))


def test_resumes_a_device(site, cfg):
    udm = gateway(apply(cfg, change("pause.delete", mac="aa:bb:cc:00:00:02")))
    assert site.sent == [("DELETE", "v2/trafficrules/tr1", {})]
    assert [p.mac for p in udm.paused] == ["aa:bb:cc:00:00:07"]


def test_resuming_a_block_set_by_hand_turns_it_off(site, cfg):
    apply(cfg, change("pause.delete", mac="aa:bb:cc:00:00:07"))
    method, endpoint, body = site.sent[0]
    assert (method, endpoint, body["enabled"], body["description"]) == (
        "PUT",
        "v2/trafficrules/tr2",
        False,
        "Kids bedtime",
    )


def test_a_block_set_by_hand_for_several_devices_is_refused(site, cfg):
    site.routes["v2/trafficrules"][1]["target_devices"].append(
        {"client_mac": "aa:bb:cc:00:00:01", "type": "CLIENT"}
    )
    with pytest.raises(PluginError, match="several devices"):
        apply(cfg, change("pause.delete", mac="aa:bb:cc:00:00:07"))


def test_pause_refusals(site, cfg):
    with pytest.raises(PluginError, match="UniFi device of the site"):
        apply(cfg, change("pause.set", mac=SWITCH))
    site.no_v2 = True
    with pytest.raises(PluginError, match=r"Network 7 or later.*never by blocking"):
        apply(cfg, change("pause.set", mac="aa:bb:cc:00:00:01"))
    assert site.sent == []


def test_a_view_only_account_is_told_the_role_it_needs(site, cfg):
    site.view_only = True
    with pytest.raises(PluginError, match="Full Management"):
        apply(cfg, change("pause.set", mac="aa:bb:cc:00:00:01"))
    with pytest.raises(PluginError, match="Full Management"):
        apply(cfg, change("reservation.delete", mac="aa:bb:cc:00:00:03"))


def test_only_the_gateway_takes_these_changes(site, cfg):
    with pytest.raises(PluginError, match="not the site's UniFi gateway"):
        apply(cfg, Change(device=SWITCH, kind="pause.set", value={"mac": "aa:bb:cc:00:00:01"}))
    site.routes["stat/device"]["data"] = [
        d for d in site.routes["stat/device"]["data"] if d["type"] != "udm"
    ]
    with pytest.raises(PluginError, match="no UniFi gateway"):
        apply(cfg, change("pause.set", mac="aa:bb:cc:00:00:01"))
