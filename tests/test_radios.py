import pytest
from omini_sdk import Change, PluginError

from omini_unifi.collect import apply, collect
from omini_unifi.radios import radios

OFFICE_AP = "78:45:58:00:00:20"
GARDEN_AP = "78:45:58:00:00:30"


def by_key(devices):
    return {d.key: d for d in devices}


def test_reads_each_radio_of_an_access_point(unifi, cfg):
    ap = by_key(collect(cfg))[OFFICE_AP]
    assert [r.model_dump(exclude_none=True) for r in ap.radios] == [
        # A fixed channel, the power the radio reports.
        {
            "name": "wifi0",
            "band": "2.4ghz",
            "channel": 6,
            "width_mhz": 20,
            "auto": False,
            "tx_power_dbm": 22.0,
        },
        # On auto: the channel is the one the radio is on now.
        {
            "name": "wifi1",
            "band": "5ghz",
            "channel": 44,
            "width_mhz": 80,
            "auto": True,
            "tx_power_dbm": 20.0,
        },
    ]
    assert ap.wireless_uplink is False


def test_a_mesh_access_point_has_a_wireless_uplink(unifi, cfg):
    ap = by_key(collect(cfg))[GARDEN_AP]
    assert ap.wireless_uplink is True
    # No live readings: the configured channel, no power.
    assert [(r.name, r.channel, r.auto, r.tx_power_dbm) for r in ap.radios] == [
        ("wifi0", 11, False, None),
        ("wifi1", 44, False, None),
    ]


def test_switches_and_gateways_have_no_radios(unifi, cfg):
    devices = by_key(collect(cfg))
    assert devices["f4:92:bf:00:00:10"].radios is None
    assert devices["f4:92:bf:00:00:10"].wireless_uplink is False
    # A gateway's uplink is the internet: unknown, left empty.
    assert devices["74:ac:b9:00:00:01"].wireless_uplink is None


def test_unknown_values_stay_empty():
    d = {
        "radio_table": [
            {"name": "wifi0", "radio": "ng", "channel": "auto"},
            {"name": "wifi2", "radio": "6e", "channel": "37", "ht": "320"},
            {"name": "wifi9", "radio": "xx", "channel": 6},  # unknown band: left out
            {"radio": "na", "channel": 36},  # no name: left out
        ]
    }
    assert radios(d) == [
        {"name": "wifi0", "band": "2.4ghz", "auto": True},
        {"name": "wifi2", "band": "6ghz", "channel": 37, "width_mhz": 320, "auto": False},
    ]


AP_ID = "6501a1b2c3d4e5f600000003"


def channel(radio="wifi0", ch=11, device=OFFICE_AP):
    return Change(device=device, kind="wifi.channel", value={"radio": radio, "channel": ch})


def test_moves_one_radio_keeping_the_others(unifi, cfg):
    devices = apply(cfg, channel())
    endpoint, body = unifi.puts[0]
    assert endpoint == f"rest/device/{AP_ID}"
    assert body == {
        "radio_table": [
            {
                "name": "wifi0",
                "radio": "ng",
                "channel": 11,
                "ht": "20",
                "tx_power_mode": "auto",
                "min_rssi_enabled": False,
            },
            # The other radio is sent as it was, still on auto.
            {
                "name": "wifi1",
                "radio": "na",
                "channel": "auto",
                "ht": "80",
                "tx_power_mode": "custom",
                "tx_power": "20",
                "min_rssi_enabled": False,
            },
        ]
    }
    radio = by_key(devices)[OFFICE_AP].radios[0]
    assert (radio.name, radio.channel, radio.auto) == ("wifi0", 11, False)


def test_a_radio_already_on_the_channel_is_left_alone(unifi, cfg):
    apply(cfg, channel(ch=6))
    assert unifi.puts == []


def test_undone_when_the_radio_does_not_report_it(unifi, cfg):
    unifi.switch_lags = True  # the controller keeps it, the radio never moves
    with pytest.raises(PluginError, match=r"did not report channel 11.*\(undone\)$"):
        apply(cfg, channel())
    assert len(unifi.puts) == 2
    assert unifi.puts[1][1]["radio_table"][0]["channel"] == 6


def test_undone_when_the_controller_does_not_keep_it(unifi, cfg):
    unifi.ignore_changes = True
    with pytest.raises(PluginError, match=r"did not keep it.*undone"):
        apply(cfg, channel())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (channel(device=GARDEN_AP), "mesh"),
        (channel(device="f4:92:bf:00:00:10"), "not an access point"),
        (channel(radio="wifi7"), "has no radio wifi7"),
        (channel(ch=36), "not a channel of the wifi0 radio"),
        (channel(radio="wifi1", ch=165 + 1), "not a channel"),
        (Change(device=OFFICE_AP, kind="wifi.channel", value={"radio": "wifi0"}), "a number"),
        (channel(device="aa:aa:aa:aa:aa:aa"), "does not manage"),
    ],
)
def test_refused_changes(unifi, cfg, change, message):
    with pytest.raises(PluginError, match=message):
        apply(cfg, change)
    assert unifi.puts == []


def test_a_view_only_account_is_told_the_role_it_needs(unifi, cfg):
    unifi.forbidden.add("rest/device")
    with pytest.raises(PluginError, match="Full Management"):
        apply(cfg, channel())
