import json

import httpx
import pytest
from omini_sdk import Change, PluginError

import omini_unifi.collect as collect_module
from omini_unifi.client import Client


@pytest.fixture
def site(unifi, monkeypatch):
    """The controller with two WLANs and the writes Wi-Fi needs."""
    unifi.routes["rest/wlanconf"] = {
        "meta": {"rc": "ok"},
        "data": [
            {
                "_id": "w1",
                "name": "Home",
                "enabled": True,
                "security": "wpapsk",
                "wpa_mode": "wpa2",
                "networkconf_id": "n1",
                "ap_group_ids": ["g1"],
            },
            {
                "_id": "w2",
                "name": "Things",
                "enabled": True,
                "security": "wpapsk",
                "wpa_mode": "wpa2",
                "wpa3_transition": True,
                "networkconf_id": "n2",
            },
        ],
    }
    unifi.routes["rest/usergroup"] = {
        "meta": {"rc": "ok"},
        "data": [{"_id": "u1", "name": "Default"}],
    }
    unifi.sent = []
    unifi.wifi_drops = set()  # WLAN fields the controller answers ok to but drops
    wlans = unifi.routes["rest/wlanconf"]["data"]
    nets = unifi.routes["rest/networkconf"]["data"]
    inner = unifi.handler

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        endpoint = path.split("/api/s/default/", 1)[-1]
        if method in ("POST", "PUT", "DELETE") and endpoint.startswith(
            ("rest/wlanconf", "rest/networkconf")
        ):
            inner(
                httpx.Request(
                    "GET",
                    request.url.copy_with(path=path.rsplit("/", 1)[0]),
                    headers=request.headers,
                )
            )
            body = json.loads(request.content) if request.content else {}
            unifi.sent.append((method, endpoint, body))
            if endpoint.startswith("rest/wlanconf"):
                body = {k: v for k, v in body.items() if k not in unifi.wifi_drops}
            _kind, _, _id = endpoint.partition("/")[2].partition("/")
            if endpoint.startswith("rest/networkconf") and method == "POST":
                made = {"_id": f"n{len(nets) + 1}", **body}
                nets.append(made)
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [made]})
            if method == "POST":
                made = {"_id": f"w{len(wlans) + 1}", **body}
                made.pop("x_passphrase", None)  # the controller never shows it back
                wlans.append(made)
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [made]})
            w = next(w for w in wlans if w["_id"] == _id)
            if method == "DELETE":
                wlans.remove(w)
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": []})
            w.update({k: v for k, v in body.items() if k != "x_passphrase"})
            return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [w]})
        return inner(request)

    monkeypatch.setattr(
        collect_module,
        "Client",
        lambda *a, **k: Client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    return unifi


def aps_wifi(devices):
    aps = [d for d in devices if d.role == "ap"]
    assert aps
    return {w.ssid: w for w in aps[0].wifi_networks}


def test_every_ap_reports_the_sites_wifi(site, cfg):
    got = aps_wifi(collect_module.collect(cfg))
    assert (got["Home"].vlan, got["Home"].security, got["Home"].enabled) == (None, "wpa2", True)
    assert (got["Things"].vlan, got["Things"].security) == (20, "wpa2wpa3")


def test_creates_a_wifi_on_a_vlan_with_no_network_yet(site, cfg):
    value = {"ssid": "Cameras", "vlan": 40, "password": "correct horse", "enabled": True}
    devices = collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    net = next(b for m, e, b in site.sent if e == "rest/networkconf")
    assert (net["purpose"], net["vlan_enabled"], net["vlan"]) == ("vlan-only", True, 40)
    wlan = next(b for m, e, b in site.sent if e == "rest/wlanconf")
    assert wlan["x_passphrase"] == "correct horse"
    assert (wlan["security"], wlan["usergroup_id"], wlan["ap_group_ids"]) == (
        "wpapsk",
        "u1",
        ["g1"],
    )
    assert aps_wifi(devices)["Cameras"].vlan == 40


def test_moves_renames_and_turns_off_a_wifi(site, cfg):
    value = {"ssid": "IoT", "previous": "Things", "vlan": 30, "enabled": False}
    devices = collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    put = next(b for m, e, b in site.sent if m == "PUT")
    assert put == {"name": "IoT", "networkconf_id": "n3", "enabled": False}  # no password: kept
    got = aps_wifi(devices)
    assert "Things" not in got and (got["IoT"].vlan, got["IoT"].enabled) == (30, False)


def test_deletes_a_wifi(site, cfg):
    devices = collect_module.apply(
        cfg, Change(device="x", kind="wifi.delete", value={"ssid": "Things"})
    )
    assert "Things" not in aps_wifi(devices)
    with pytest.raises(PluginError, match="no Wi-Fi network"):
        collect_module.apply(cfg, Change(device="x", kind="wifi.delete", value={"ssid": "Nope"}))


def test_a_change_the_controller_does_not_show_is_undone(site, cfg):
    site.wifi_drops = {"enabled"}
    value = {"ssid": "IoT", "previous": "Things", "vlan": 30, "enabled": False}
    with pytest.raises(
        PluginError, match=r"did not apply the Wi-Fi change.*undone on the controller"
    ):
        collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    assert site.sent[-1] == (
        "PUT",
        "rest/wlanconf/w2",
        {"name": "Things", "networkconf_id": "n2", "enabled": True},
    )
    got = aps_wifi(collect_module.collect(cfg))
    assert "IoT" not in got and (got["Things"].vlan, got["Things"].enabled) == (20, True)


def test_a_new_wifi_the_controller_does_not_show_is_deleted(site, cfg):
    site.wifi_drops = {"networkconf_id"}
    value = {"ssid": "Cameras", "vlan": 40, "password": "correct horse"}
    with pytest.raises(PluginError, match="undone on the controller"):
        collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    assert site.sent[-1] == ("DELETE", "rest/wlanconf/w3", {})
    assert "Cameras" not in aps_wifi(collect_module.collect(cfg))


def test_the_wifi_check_rides_out_a_transient_error(site, cfg, monkeypatch):
    import omini_unifi.wifi as wifi_module
    from omini_unifi.client import ApiError

    monkeypatch.setattr(wifi_module, "VERIFY_S", 5)
    real_get = Client.get
    failed = []

    def get(self, endpoint):
        if endpoint == "rest/wlanconf" and site.sent and not failed:
            failed.append(endpoint)
            raise ApiError(f"{endpoint}: HTTP 502")
        return real_get(self, endpoint)

    monkeypatch.setattr(Client, "get", get)
    value = {"ssid": "Things", "enabled": False}
    devices = collect_module.apply(cfg, Change(device="x", kind="wifi.set", value=value))
    assert failed and aps_wifi(devices)["Things"].enabled is False
