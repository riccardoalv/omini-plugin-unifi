import json

import pytest
from omini_sdk import PluginError

from omini_unifi.collect import backup, collect

SWITCH = "f4:92:bf:00:00:10"
UDM = "74:ac:b9:00:00:01"
AP = "78:45:58:00:00:20"


def ok(data):
    return {"meta": {"rc": "ok"}, "data": data}


def switch(fake):
    return next(d for d in fake.routes["stat/device"]["data"] if d["mac"].lower() == SWITCH)


def ports(devices, key=SWITCH):
    d = next(d for d in devices if d.key == key)
    return {i.name: i for i in d.interfaces}


def aggregate(fake, by_override=False):
    """Ports 2 and 3 of the switch aggregated (LAG 2-3), from its port table or an override."""
    table = {p["port_idx"]: p for p in switch(fake)["port_table"]}
    if by_override:
        switch(fake)["port_overrides"].append(
            {"port_idx": 2, "op_mode": "aggregate", "aggregate_num_ports": 2}
        )
        return
    table[2].update({"op_mode": "aggregate", "aggregated_by": False, "aggregate_num_ports": 2})
    table[3].update({"op_mode": "aggregate", "aggregated_by": 2})


# --- STP -----------------------------------------------------------------------


def test_stp_state_and_role_of_switch_ports(unifi, cfg):
    table = {p["port_idx"]: p for p in switch(unifi)["port_table"]}
    table[1]["stp_state"] = "forwarding"
    table[2].update({"stp_state": "discarding", "stp_role": "alternate"})
    table[3]["stp_state"] = "something new"
    p = ports(collect(cfg))
    assert (p["Port 1"].stp.state, p["Port 1"].stp.role) == ("forwarding", None)
    # RSTP's "discarding" is blocking.
    assert (p["Office AP"].stp.state, p["Office AP"].stp.role) == ("blocking", "alternate")
    assert p["Port 3"].stp is None  # an unknown state is not invented
    assert p["SFP 1"].stp is None  # not reported


def test_no_stp_on_access_points(unifi, cfg):
    ap = next(d for d in unifi.routes["stat/device"]["data"] if d["mac"].lower() == AP)
    ap["port_table"][0]["stp_state"] = "forwarding"
    assert ports(collect(cfg), AP)["Main"].stp is None


# --- LAG -----------------------------------------------------------------------


@pytest.mark.parametrize("by_override", [False, True])
def test_aggregated_ports_and_their_lag(unifi, cfg, by_override):
    aggregate(unifi, by_override)
    p = ports(collect(cfg))
    assert (p["Office AP"].lag, p["Port 3"].lag) == ("LAG 2-3", "LAG 2-3")
    assert p["Port 1"].lag is None and p["SFP 1"].lag is None
    lag = p["LAG 2-3"]
    assert lag.type == "lag" and lag.up is True
    assert lag.speed_mbps == 1000 + 100  # the members up
    assert lag.members == ["Office AP", "Port 3"]


def test_a_lag_with_every_member_down_is_down(unifi, cfg):
    aggregate(unifi)
    for p in switch(unifi)["port_table"]:
        if p["port_idx"] in (2, 3):
            p["up"] = False
    lag = ports(collect(cfg))["LAG 2-3"]
    assert lag.up is False and lag.speed_mbps is None


def test_no_lag_without_aggregates(unifi, cfg):
    assert not any(i.type == "lag" for d in collect(cfg) for i in d.interfaces or [])


# --- backup ----------------------------------------------------------------------


def site(fake):
    fake.routes["rest/wlanconf"] = ok(
        [
            {
                "_id": "w2",
                "name": "Guests",
                "x_passphrase": "guest-pass",
                "security": "wpapsk",
                "private_preshared_keys": [{"password": "ppsk"}],
            },
            {"_id": "w1", "name": "Home", "x_passphrase": "home-pass", "security": "wpapsk"},
        ]
    )
    fake.routes["get/setting"] = ok(
        [
            {"_id": "s2", "key": "snmp", "community": "private", "enabled": False},
            {"_id": "s1", "key": "mgmt", "x_ssh_password": "root-pass", "led_enabled": True},
            {"_id": "s3", "key": "radius", "x_secret": "radius-secret", "auth_port": 1812},
        ]
    )


def contents(cfg):
    return {b.device: json.loads(b.content) for b in backup(cfg)}


def test_one_backup_per_adopted_device(unifi, cfg):
    site(unifi)
    out = backup(cfg)
    assert [b.device for b in out] == sorted([UDM, SWITCH, AP, "78:45:58:00:00:30"])
    assert {b.format for b in out} == {"json"}
    keys = {d.key for d in collect(cfg)}
    assert {b.device for b in out} == keys  # the keys collect reports


def test_device_settings_without_live_status(unifi, cfg):
    site(unifi)
    sw = contents(cfg)[SWITCH]["device"]
    assert sw["name"] == "Rack Switch"
    assert [o["port_idx"] for o in sw["port_overrides"]] == [2, 3]
    for live in ("uptime", "port_table", "system-stats", "state", "version", "upgradable"):
        assert live not in sw
    ap = contents(cfg)[AP]["device"]
    assert "radio_table" in ap and "vap_table" not in ap


def test_site_settings_go_with_the_gateway_without_secrets(unifi, cfg):
    site(unifi)
    out = contents(cfg)
    assert [k for k, v in out.items() if "site" in v] == [UDM]
    s = out[UDM]["site"]
    assert [w["name"] for w in s["wifi_networks"]] == ["Guests", "Home"]  # sorted
    assert all(
        "x_passphrase" not in w and "private_preshared_keys" not in w for w in s["wifi_networks"]
    )
    assert list(s["settings"]) == ["mgmt", "radius", "snmp"]
    assert s["settings"]["mgmt"] == {"_id": "s1", "key": "mgmt", "led_enabled": True}
    assert "community" not in s["settings"]["snmp"]
    assert s["settings"]["radius"]["auth_port"] == 1812
    assert "networks" in s and "port_profiles" in s
    text = "".join(b.content for b in backup(cfg))
    for secret in ("guest-pass", "home-pass", "ppsk", "private", "root-pass", "radius-secret"):
        assert secret not in text


def test_without_a_gateway_the_switch_holds_the_site(unifi, cfg):
    site(unifi)
    unifi.routes["stat/device"]["data"] = [
        d for d in unifi.routes["stat/device"]["data"] if d["type"] != "udm"
    ]
    out = contents(cfg)
    assert [k for k, v in out.items() if "site" in v] == [SWITCH]


def test_backup_is_deterministic(unifi, cfg):
    site(unifi)
    first = [b.content for b in backup(cfg)]
    # Live status changes and the controller lists things in another order.
    for d in unifi.routes["stat/device"]["data"]:
        d["uptime"] = (d.get("uptime") or 0) + 300
        for p in d.get("port_table") or []:
            p["rx_bytes"] = (p.get("rx_bytes") or 0) + 12345
    unifi.routes["rest/wlanconf"]["data"].reverse()
    unifi.routes["get/setting"]["data"].reverse()
    unifi.routes["stat/device"]["data"].reverse()
    assert [b.content for b in backup(cfg)] == first


def test_backup_changes_with_the_configuration(unifi, cfg):
    site(unifi)
    before = contents(cfg)
    switch(unifi)["port_overrides"].append({"port_idx": 1, "forward": "disabled"})
    after = contents(cfg)
    assert after[SWITCH] != before[SWITCH]
    assert after[AP] == before[AP]


def test_unreadable_site_settings_are_left_out(unifi, cfg):
    site(unifi)
    unifi.forbidden.add("get/setting")
    s = contents(cfg)[UDM]["site"]
    assert "settings" not in s and "wifi_networks" in s


def test_backup_without_devices(unifi, cfg):
    unifi.routes["stat/device"]["data"] = []
    with pytest.raises(PluginError, match="no adopted device"):
        backup(cfg)
