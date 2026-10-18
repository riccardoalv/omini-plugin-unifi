import pytest
from omini_sdk import Change, PluginError

from omini_unifi.collect import apply, collect

SWITCH = "f4:92:bf:00:00:10"
SWITCH_ID = "6501a1b2c3d4e5f600000002"
DISABLED_PROFILE = "6501a1b2c3d4e5f6000000a0"

# The switch's overrides in the fixture, as the controller has them.
OFFICE_AP = {
    "port_idx": 2,
    "name": "Office AP",
    "poe_mode": "auto",
    "portconf_id": "6501a1b2c3d4e5f6000000a1",
}
PORT_3 = {
    "port_idx": 3,
    "forward": "customize",
    "native_networkconf_id": "6501a1b2c3d4e5f6000000b1",
    "tagged_vlan_mgmt": "custom",
    "excluded_networkconf_ids": ["6501a1b2c3d4e5f6000000b2"],
    "autoneg": True,
}


def change(port="Port 1", value=False, device=SWITCH):
    return Change(device=device, kind="port.enabled", port=port, value=value)


def ports(devices, key=SWITCH):
    d = next(d for d in devices if d.key == key)
    return {i.name: i for i in d.interfaces}


def overrides(unifi):
    return unifi.device(SWITCH_ID)["port_overrides"]


def test_reads_whether_each_port_is_enabled(unifi, cfg):
    unifi.device(SWITCH_ID)["port_table"][0]["enable"] = False
    unifi.device(SWITCH_ID)["port_overrides"][1]["forward"] = "disabled"
    sw = ports(collect(cfg))
    assert sw["Port 1"].enabled is False  # the switch reports it disabled
    assert sw["Office AP"].enabled is True
    assert sw["Port 3"].enabled is False  # its override disables it
    # The gateway's port table has no "enable": unknown, left empty.
    assert ports(collect(cfg), "74:ac:b9:00:00:01")["Port 1"].enabled is None


def test_disables_a_port_keeping_every_other_setting(unifi, cfg):
    devices = apply(cfg, change())
    assert unifi.puts == [
        (
            f"rest/device/{SWITCH_ID}",
            {"port_overrides": [OFFICE_AP, PORT_3, {"port_idx": 1, "forward": "disabled"}]},
        )
    ]
    assert ports(devices)["Port 1"].enabled is False
    # Enabling removes only what disabling added.
    devices = apply(cfg, change(value=True))
    assert unifi.puts[1][1] == {"port_overrides": [OFFICE_AP, PORT_3]}
    assert ports(devices)["Port 1"].enabled is True


def test_enabling_puts_back_the_port_s_own_forward(unifi, cfg):
    apply(cfg, change("Port 3"))
    assert unifi.puts[0][1]["port_overrides"][1] == dict(PORT_3, forward="disabled")
    apply(cfg, change("Port 3", True))
    assert unifi.puts[1][1]["port_overrides"] == [OFFICE_AP, PORT_3]


def test_a_port_disabled_elsewhere_is_enabled_with_forward_all(unifi, cfg):
    overrides(unifi)[1]["forward"] = "disabled"
    unifi.device(SWITCH_ID)["port_table"][2]["enable"] = False
    apply(cfg, change("Port 3", True))
    assert unifi.puts[0][1]["port_overrides"][1] == dict(PORT_3, forward="all")


def test_a_port_with_the_disabled_profile_is_enabled_by_removing_it(unifi, cfg):
    # Older Network versions disabled a port with the built-in "Disabled" profile.
    overrides(unifi)[0]["portconf_id"] = DISABLED_PROFILE
    unifi.device(SWITCH_ID)["port_table"][1]["enable"] = False
    assert ports(collect(cfg))["Office AP"].enabled is False
    devices = apply(cfg, change("Office AP", True))
    assert unifi.puts[0][1]["port_overrides"][0] == {
        "port_idx": 2,
        "name": "Office AP",
        "poe_mode": "auto",
    }
    assert ports(devices)["Office AP"].enabled is True


def test_disabling_keeps_the_port_s_profile(unifi, cfg):
    apply(cfg, change("Office AP"))
    assert unifi.puts[0][1]["port_overrides"][0] == dict(OFFICE_AP, forward="disabled")
    apply(cfg, change("Office AP", True))
    assert unifi.puts[1][1]["port_overrides"][0] == OFFICE_AP


def test_nothing_is_sent_when_the_port_is_already_so(unifi, cfg):
    devices = apply(cfg, change(value=True))
    assert unifi.puts == []
    assert ports(devices)["Port 1"].enabled is True


def test_undone_when_the_controller_does_not_keep_it(unifi, cfg):
    unifi.ignore_changes = True
    with pytest.raises(PluginError, match="did not keep it; the change was undone"):
        apply(cfg, change())
    assert unifi.puts[-1][1] == {"port_overrides": [OFFICE_AP, PORT_3]}


def test_undone_when_the_switch_does_not_report_it(unifi, cfg):
    unifi.switch_lags = True
    with pytest.raises(PluginError, match="switch did not report it within 0 s"):
        apply(cfg, change())
    assert [body for _, body in unifi.puts] == [
        {"port_overrides": [OFFICE_AP, PORT_3, {"port_idx": 1, "forward": "disabled"}]},
        {"port_overrides": [OFFICE_AP, PORT_3]},
    ]


def test_unknown_ports_devices_and_values(unifi, cfg):
    with pytest.raises(PluginError, match="has no port Port 12"):
        apply(cfg, change("Port 12"))
    with pytest.raises(PluginError, match="does not manage"):
        apply(cfg, change(device="aa:bb:cc:dd:ee:ff"))
    with pytest.raises(PluginError, match="not a switch or gateway"):
        apply(cfg, change("Main", device="78:45:58:00:00:20"))
    with pytest.raises(PluginError, match="is a WAN port"):
        apply(cfg, change("Port 9", device="74:ac:b9:00:00:01"))
    with pytest.raises(PluginError, match="takes true or false"):
        apply(cfg, change(value="off"))
    assert unifi.puts == []


def test_a_disconnected_switch_is_not_changed(unifi, cfg):
    unifi.device(SWITCH_ID)["state"] = 0
    with pytest.raises(PluginError, match="disconnected"):
        apply(cfg, change())
    assert unifi.puts == []


def test_a_view_only_account_is_told_the_role_it_needs(unifi, cfg):
    unifi.forbidden = {"rest/device"}
    with pytest.raises(PluginError) as e:
        apply(cfg, change())
    assert "Full Management or Site Admin, not View Only" in str(e.value)


def test_the_gateway_s_lan_ports_too(classic, cfg):
    devices = apply(cfg, change("Port 2", device="74:ac:b9:00:00:01"))
    endpoint, body = classic.puts[0]
    assert endpoint == "rest/device/6501a1b2c3d4e5f600000001"
    assert body == {"port_overrides": [{"port_idx": 2, "forward": "disabled"}]}
    assert ports(devices, "74:ac:b9:00:00:01")["Port 2"].enabled is False
    assert ("PUT", "/api/s/default/rest/device/6501a1b2c3d4e5f600000001") in classic.calls


def test_access_points_report_no_port_state(unifi, cfg):
    # Omini changes ports of switches and gateways only: an access point's
    # ports report no state, so the map offers no button there.
    aps = [d for d in collect(cfg) if d.role == "ap"]
    assert aps and all(i.enabled is None for d in aps for i in d.interfaces or [])


def test_collect_reads_the_disabled_profile_like_apply(unifi, cfg):
    # The override gives the "Disabled" profile, though the switch still
    # reports the port enabled: collect and apply agree it is disabled.
    overrides(unifi)[0]["portconf_id"] = DISABLED_PROFILE
    unifi.device(SWITCH_ID)["port_table"][1]["enable"] = True
    assert ports(collect(cfg))["Office AP"].enabled is False
    apply(cfg, change("Office AP", False))
    assert unifi.puts == []  # already disabled: nothing to do
