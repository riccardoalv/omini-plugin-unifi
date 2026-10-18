import copy
import json

import httpx
import pytest
from omini_sdk import Change, PluginError

import omini_unifi.collect as collect_module
from omini_unifi.client import Client
from omini_unifi.collect import apply, collect

SWITCH = "f4:92:bf:00:00:10"
SWITCH_ID = "6501a1b2c3d4e5f600000002"
GATEWAY = "74:ac:b9:00:00:01"
ALL_PROFILE = "6501a1b2c3d4e5f6000000a1"


@pytest.fixture
def site(unifi, monkeypatch):
    """The controller, also keeping what is written to rest/networkconf."""
    nets = unifi.routes["rest/networkconf"]["data"]
    unifi.routes["rest/wlanconf"] = {"meta": {"rc": "ok"}, "data": []}
    unifi.sent = []
    unifi.ignore_networks = False
    unifi.view_only = False
    inner = unifi.handler

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        endpoint = path.split("/api/s/default/", 1)[-1]
        if method in ("POST", "PUT", "DELETE") and endpoint.startswith("rest/networkconf"):
            # The session and site checks, as for a read.
            check = inner(httpx.Request("GET", request.url, headers=request.headers))
            if check.status_code == 401:
                return check
            if unifi.view_only:
                return httpx.Response(
                    403, json={"meta": {"rc": "error", "msg": "api.err.NoPermission"}, "data": []}
                )
            body = json.loads(request.content) if request.content else {}
            unifi.sent.append((method, endpoint, body))
            if unifi.ignore_networks:
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": []})
            if method == "POST":
                made = {"_id": f"n{len(nets) + 10}", **body}
                nets.append(made)
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [made]})
            _id = endpoint.rsplit("/", 1)[1]
            n = next(n for n in nets if n["_id"] == _id)
            if method == "DELETE":
                nets.remove(n)
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": []})
            n.update(body)
            return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [n]})
        if endpoint.startswith("rest/networkconf/"):
            return httpx.Response(404, json={"meta": {"rc": "error"}, "data": []})
        return inner(request)

    monkeypatch.setattr(
        collect_module,
        "Client",
        lambda *a, **k: Client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    return unifi


def switch(unifi):
    return unifi.device(SWITCH_ID)


def override(unifi, idx):
    return next((o for o in switch(unifi)["port_overrides"] if o["port_idx"] == idx), None)


def port_vlans(devices, key=SWITCH):
    d = next(d for d in devices if d.key == key)
    return {
        i.name: (i.vlans.untagged, [t.root for t in i.vlans.tagged or []]) if i.vlans else None
        for i in d.interfaces or []
    }


def vlans_of(devices, key=SWITCH):
    return {v.id: v.name for v in next(d for d in devices if d.key == key).vlans}


def port_change(port, untagged, tagged=None):
    return Change(
        device=SWITCH, kind="port.vlans", port=port, value={"untagged": untagged, "tagged": tagged}
    )


# --- reading -------------------------------------------------------------------


def test_reads_each_switch_port_s_vlans(site, cfg):
    got = port_vlans(collect(cfg))
    # No override: the default, the LAN native and every network tagged.
    assert got["Port 1"] == (1, [20, 30])
    # The "All" profile says the same.
    assert got["Office AP"] == (1, [20, 30])
    # A native network the site does not have: unknown, left empty.
    assert got["Port 3"] is None
    # Gateways and access points report none: Omini changes switch ports only.
    assert all(v is None for v in port_vlans(collect(cfg), GATEWAY).values())
    assert all(v is None for v in port_vlans(collect(cfg), "78:45:58:00:00:20").values())


def test_reads_the_newer_override_fields(site, cfg):
    o = override(site, 3)
    o.update(native_networkconf_id="n2", tagged_vlan_mgmt="custom", excluded_networkconf_ids=["n3"])
    assert port_vlans(collect(cfg))["Port 3"] == (20, [1])
    o["tagged_vlan_mgmt"] = "block_all"
    assert port_vlans(collect(cfg))["Port 3"] == (20, [])
    o.update(tagged_vlan_mgmt="auto", native_networkconf_id="")
    assert port_vlans(collect(cfg))["Port 3"] == (None, [1, 20, 30])


def test_reads_an_older_port_profile(site, cfg):
    site.routes["rest/portconf"]["data"].append(
        {
            "_id": "p-cams",
            "name": "Cameras",
            "forward": "customize",
            "native_networkconf_id": "n2",
            "tagged_networkconf_ids": ["n3"],
        }
    )
    override(site, 2)["portconf_id"] = "p-cams"
    assert port_vlans(collect(cfg))["Office AP"] == (20, [30])
    site.routes["rest/portconf"]["data"][-1]["forward"] = "native"
    del site.routes["rest/portconf"]["data"][-1]["tagged_networkconf_ids"]
    assert port_vlans(collect(cfg))["Office AP"] == (20, [])


def test_the_port_table_when_there_is_no_override(site, cfg):
    switch(site)["port_table"][0].update(native_networkconf_id="n3", tagged_vlan_mgmt="block_all")
    assert port_vlans(collect(cfg))["Port 1"] == (30, [])


def test_aggregated_ports_report_none(site, cfg):
    switch(site)["port_table"][0].update(op_mode="aggregate", aggregated_by=17)
    assert port_vlans(collect(cfg))["Port 1"] is None


# --- port.vlans ------------------------------------------------------------------


def test_makes_a_port_an_access_port(site, cfg):
    others = copy.deepcopy(switch(site)["port_overrides"])
    devices = apply(cfg, port_change("Port 1", 20))
    endpoint, body = site.puts[0]
    assert endpoint == f"rest/device/{SWITCH_ID}"
    assert body["port_overrides"][:2] == others  # the other ports, as they were
    assert body["port_overrides"][2] == {
        "port_idx": 1,
        "native_networkconf_id": "n2",
        "tagged_vlan_mgmt": "block_all",
        "excluded_networkconf_ids": [],
        "forward": "customize",
    }
    assert port_vlans(devices)["Port 1"] == (20, [])


def test_makes_a_trunk_keeping_the_port_s_profile_and_settings(site, cfg):
    devices = apply(cfg, port_change("Office AP", 1, [20]))
    assert override(site, 2) == {
        "port_idx": 2,
        "name": "Office AP",
        "poe_mode": "auto",
        "portconf_id": ALL_PROFILE,
        "native_networkconf_id": "n1",
        "tagged_vlan_mgmt": "custom",
        "excluded_networkconf_ids": ["n3"],
        "forward": "customize",
    }
    assert port_vlans(devices)["Office AP"] == (1, [20])


def test_a_trunk_with_no_native_vlan(site, cfg):
    devices = apply(cfg, port_change("Port 3", None, [20, 30]))
    o = override(site, 3)
    assert (o["native_networkconf_id"], o["excluded_networkconf_ids"], o["autoneg"]) == (
        "",
        ["n1"],
        True,
    )
    assert port_vlans(devices)["Port 3"] == (None, [20, 30])


def test_a_disabled_port_stays_disabled(site, cfg):
    switch(site)["port_overrides"].append({"port_idx": 1, "forward": "disabled"})
    apply(cfg, port_change("Port 1", 30))
    assert override(site, 1)["forward"] == "disabled"


def test_an_older_profile_gets_the_tagged_list_too(site, cfg):
    site.routes["rest/portconf"]["data"].append(
        {"_id": "p-old", "name": "Old", "forward": "customize", "tagged_networkconf_ids": []}
    )
    override(site, 2)["portconf_id"] = "p-old"
    devices = apply(cfg, port_change("Office AP", 1, [30]))
    o = override(site, 2)
    assert (o["portconf_id"], o["tagged_networkconf_ids"]) == ("p-old", ["n3"])
    assert port_vlans(devices)["Office AP"] == (1, [30])


def test_nothing_is_sent_when_the_port_already_has_them(site, cfg):
    apply(cfg, port_change("Port 1", 1, [30, 20]))
    assert site.puts == []


def test_the_switch_reports_the_change(site, cfg):
    switch(site)["port_table"][0].update(native_networkconf_id="n1", tagged_vlan_mgmt="auto")
    apply(cfg, port_change("Port 1", 20))
    assert switch(site)["port_table"][0]["native_networkconf_id"] == "n2"


def test_undone_when_the_switch_does_not_report_it(site, cfg):
    switch(site)["port_table"][0].update(native_networkconf_id="n1", tagged_vlan_mgmt="auto")
    site.switch_lags = True
    before = copy.deepcopy(switch(site)["port_overrides"])
    with pytest.raises(PluginError, match=r"switch did not report it within 0 s \(undone\)"):
        apply(cfg, port_change("Port 1", 20))
    assert site.puts[-1][1] == {"port_overrides": before}


def test_undone_when_the_controller_does_not_keep_it(site, cfg):
    site.ignore_changes = True
    with pytest.raises(PluginError, match=r"did not keep it \(undone\)"):
        apply(cfg, port_change("Port 1", 20))
    assert len(site.puts) == 2


def test_port_vlans_refusals(site, cfg):
    with pytest.raises(PluginError, match="no network for VLAN 40"):
        apply(cfg, port_change("Port 1", 40))
    with pytest.raises(PluginError, match="no network for VLAN 2000"):
        apply(cfg, port_change("Port 1", 1, [2000]))  # the WAN's VLAN
    with pytest.raises(PluginError, match="untagged or a tagged VLAN"):
        apply(cfg, port_change("Port 1", None, []))
    with pytest.raises(PluginError, match="takes"):
        apply(cfg, Change(device=SWITCH, kind="port.vlans", port="Port 1", value=20))
    with pytest.raises(PluginError, match="has no port"):
        apply(cfg, port_change("Port 12", 20))
    with pytest.raises(PluginError, match="is not a switch"):
        apply(cfg, Change(device=GATEWAY, kind="port.vlans", port="Port 2", value={"untagged": 20}))
    with pytest.raises(PluginError, match="not a switch or gateway"):
        apply(
            cfg,
            Change(
                device="78:45:58:00:00:20", kind="port.vlans", port="Main", value={"untagged": 20}
            ),
        )
    switch(site)["port_table"][0].update(op_mode="aggregate", aggregated_by=17)
    with pytest.raises(PluginError, match="aggregate"):
        apply(cfg, port_change("Port 1", 20))
    assert site.puts == []


def test_a_view_only_account_cannot_change_a_port_s_vlans(site, cfg):
    site.forbidden = {"rest/device"}
    with pytest.raises(PluginError, match="Full Management or Site Admin"):
        apply(cfg, port_change("Port 1", 20))


# --- vlan.set / vlan.delete ------------------------------------------------------


def vlan_change(kind, **value):
    return Change(device=SWITCH, kind=kind, value=value)


def test_creates_a_vlan(site, cfg):
    devices = apply(cfg, vlan_change("vlan.set", id=40, name="Cameras"))
    assert site.sent == [
        (
            "POST",
            "rest/networkconf",
            {
                "name": "Cameras",
                "purpose": "vlan-only",
                "vlan_enabled": True,
                "vlan": 40,
                "enabled": True,
            },
        )
    ]
    assert vlans_of(devices)[40] == "Cameras"
    # Every port carrying every network (the default) now carries it too.
    assert port_vlans(devices)["Port 1"] == (1, [20, 30, 40])


def test_renames_a_vlan(site, cfg):
    devices = apply(cfg, vlan_change("vlan.set", id=20, name="Things"))
    assert site.sent == [("PUT", "rest/networkconf/n2", {"name": "Things"})]
    assert vlans_of(devices)[20] == "Things"
    apply(cfg, vlan_change("vlan.set", id=20, name="Things"))
    assert len(site.sent) == 1  # already so


def test_vlan_set_refusals(site, cfg):
    with pytest.raises(PluginError, match="used by a WAN"):
        apply(cfg, vlan_change("vlan.set", id=2000, name="Uplink"))
    with pytest.raises(PluginError, match='already has a network named "Guests"'):
        apply(cfg, vlan_change("vlan.set", id=40, name="guests"))
    with pytest.raises(PluginError, match="needs a name"):
        apply(cfg, vlan_change("vlan.set", id=40, name=" "))
    with pytest.raises(PluginError, match="from 1 to 4094"):
        apply(cfg, vlan_change("vlan.set", id=5000, name="Big"))
    assert site.sent == []


def test_vlan_set_is_checked(site, cfg):
    site.ignore_networks = True
    with pytest.raises(PluginError, match="did not apply the VLAN change"):
        apply(cfg, vlan_change("vlan.set", id=40, name="Cameras"))


def test_deletes_a_vlan_only_network(site, cfg):
    apply(cfg, vlan_change("vlan.set", id=40, name="Cameras"))
    devices = apply(cfg, vlan_change("vlan.delete", id=40))
    assert site.sent[-1][:2] == ("DELETE", "rest/networkconf/n14")
    assert 40 not in vlans_of(devices)


def test_vlan_delete_refusals(site, cfg):
    with pytest.raises(PluginError, match=r'"IOT" \(VLAN 20\) is a network with its own gateway'):
        apply(cfg, vlan_change("vlan.delete", id=20))
    with pytest.raises(PluginError, match="own gateway \\(guest\\)"):
        apply(cfg, vlan_change("vlan.delete", id=30))
    with pytest.raises(PluginError, match="is a WAN"):
        apply(cfg, vlan_change("vlan.delete", id=2000))
    with pytest.raises(PluginError, match="no VLAN 99"):
        apply(cfg, vlan_change("vlan.delete", id=99))
    with pytest.raises(PluginError, match="LAN"):
        apply(cfg, vlan_change("vlan.delete", id=1))
    site.routes["rest/networkconf"]["data"].append(
        {"_id": "n9", "name": "Cams", "purpose": "vlan-only", "vlan_enabled": True, "vlan": 50}
    )
    site.routes["rest/wlanconf"]["data"].append(
        {"_id": "w1", "name": "CamWiFi", "networkconf_id": "n9"}
    )
    with pytest.raises(PluginError, match="the Wi-Fi CamWiFi"):
        apply(cfg, vlan_change("vlan.delete", id=50))
    assert site.sent == []


def test_a_view_only_account_cannot_change_vlans(site, cfg):
    site.view_only = True
    with pytest.raises(PluginError, match="Full Management or Site Admin"):
        apply(cfg, vlan_change("vlan.set", id=40, name="Cameras"))


def test_vlan_set_check_rides_out_a_transient_error(site, cfg, monkeypatch):
    # The controller fails one read while it provisions: the check reads again.
    import omini_unifi.vlans as vlans_module
    from omini_unifi.client import ApiError

    monkeypatch.setattr(vlans_module, "VERIFY_S", 5)
    real_get = Client.get
    failed = []

    def get(self, endpoint):
        if endpoint == "rest/networkconf" and site.sent and not failed:
            failed.append(endpoint)
            raise ApiError(f"{endpoint}: HTTP 502")
        return real_get(self, endpoint)

    monkeypatch.setattr(Client, "get", get)
    devices = apply(cfg, vlan_change("vlan.set", id=40, name="Cameras"))
    assert failed and vlans_of(devices)[40] == "Cameras"
