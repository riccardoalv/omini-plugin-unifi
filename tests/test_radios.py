from omini_unifi.collect import collect
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
