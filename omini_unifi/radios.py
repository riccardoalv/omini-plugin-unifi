"""An access point's Wi-Fi radios and the channel each is on.

``stat/device`` has two lists per access point:

- ``radio_table``: the radios' settings in the controller (``name`` such as
  ``wifi0``, ``radio`` the band, ``channel`` a number or ``"auto"``, ``ht``
  the width in MHz, ``tx_power_mode`` and, when it is ``custom``,
  ``tx_power``);
- ``radio_table_stats``: what each radio reports now (``name``, the
  ``channel`` it is on, its ``tx_power``).

A radio's channel is the one it reports, else the configured one; whether
the controller picks it is the configured ``"auto"``."""

from __future__ import annotations

import re
import time
from typing import Any

from omini_sdk import PluginError
from omini_sdk.changes import Failed, Pending, apply_verify

BANDS = {"ng": "2.4ghz", "na": "5ghz", "6e": "6ghz"}
WIDTHS = {20, 40, 80, 160, 320}
AUTO = "auto"


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def channel_of(value: Any) -> int | None:
    """6, "6" → 6; "auto", 0 and anything else → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and 1 <= value <= 233 else None


def width_of(value: Any) -> int | None:
    """ "20", 40, "HT80" → MHz; anything else → None."""
    if isinstance(value, bool):
        return None
    digits = re.sub(r"\D", "", str(value)) if isinstance(value, (int, str)) else ""
    n = int(digits) if digits else None
    return n if n in WIDTHS else None


def power_of(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    return float(value) if isinstance(value, (int, float)) else None


def is_auto(r: dict[str, Any]) -> bool | None:
    """Whether the controller picks the radio's channel; None when unsaid."""
    ch = r.get("channel")
    if isinstance(ch, str) and ch.strip().lower() == AUTO:
        return True
    if channel_of(ch) is not None:
        return False
    if ch == 0:  # some versions write 0 for automatic
        return True
    return None


def radios(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Each radio of the access point, as Omini's Radio fields."""
    stats = {
        text(s.get("name")): s
        for s in d.get("radio_table_stats") or []
        if isinstance(s, dict) and text(s.get("name"))
    }
    out = []
    for r in d.get("radio_table") or []:
        if not isinstance(r, dict):
            continue
        name, band = text(r.get("name")), BANDS.get(text(r.get("radio")))
        if not name or not band:
            continue
        live = stats.get(name, {})
        power = power_of(live.get("tx_power"))
        if power is None and text(r.get("tx_power_mode")).lower() == "custom":
            power = power_of(r.get("tx_power"))
        radio = {
            "name": name,
            "band": band,
            "channel": channel_of(live.get("channel")) or channel_of(r.get("channel")),
            "width_mhz": width_of(r.get("ht")),
            "auto": is_auto(r),
            "tx_power_dbm": power,
        }
        out.append({k: v for k, v in radio.items() if v is not None})
    return out


def wireless_uplink(d: dict[str, Any]) -> bool | None:
    """A device that reaches the network over Wi-Fi (a mesh access point)."""
    kind = text((d.get("uplink") or {}).get("type"))
    if kind == "wireless":
        return True
    if kind in ("wire", "wired"):
        return False
    return None


# --- wifi.channel ------------------------------------------------------------

# How long the change waits for the access point to report the new channel
# (the controller provisions it, and the radio restarts on it).
VERIFY_S = 60
POLL_S = 3

# The channels each band may be set to (20 MHz primaries).
CHANNELS = {
    "2.4ghz": set(range(1, 15)),
    "5ghz": {*range(36, 65, 4), *range(100, 145, 4), *range(149, 178, 4)},
    "6ghz": set(range(1, 234, 4)),
}


def wanted(value: Any) -> tuple[str, int]:
    """The radio's name and the channel asked, or a PluginError."""
    name = text(value.get("radio")) if isinstance(value, dict) else ""
    ch = value.get("channel") if isinstance(value, dict) else None
    if not name:
        raise PluginError("wifi.channel takes {radio, channel}: the radio is missing")
    if isinstance(ch, bool) or not isinstance(ch, int):
        raise PluginError(f"the channel must be a number, not {ch!r}")
    return name, ch


def radio_entry(d: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (
            r
            for r in d.get("radio_table") or []
            if isinstance(r, dict) and text(r.get("name")) == name
        ),
        None,
    )


def changed_table(before: list[dict[str, Any]], name: str, channel: int) -> list[dict[str, Any]]:
    """The radio table with only that radio's channel changed: every other
    radio, and every other setting of this one, stay as they are."""
    out = [dict(r) for r in before]
    for r in out:
        if text(r.get("name")) == name:
            r["channel"] = channel
    return out


def live_channel(d: dict[str, Any], name: str) -> int | None:
    for s in d.get("radio_table_stats") or []:
        if isinstance(s, dict) and text(s.get("name")) == name:
            return channel_of(s.get("channel"))
    return None


def has_stats(d: dict[str, Any], name: str) -> bool:
    return any(
        isinstance(s, dict) and text(s.get("name")) == name
        for s in d.get("radio_table_stats") or []
    )


def check(c: Any, find: Any, _id: str, name: str, channel: int) -> bool:
    """Whether the controller keeps the channel and the radio reports it
    (when the access point reports its radios); Failed when it will not."""
    d = find(c.get("stat/device"), _id)
    if d is None:
        raise Failed("the controller no longer lists the access point")
    if channel_of((radio_entry(d, name) or {}).get("channel")) != channel:
        raise Failed("the controller did not keep it")
    live = live_channel(d, name)
    if d.get("state") not in (0, None) and (
        live == channel or (live is None and not has_stats(d, name))
    ):
        return True
    raise Pending(f"the radio did not report channel {channel} within {VERIFY_S} s")


def set_channel(c: Any, d: dict[str, Any], value: Any, find: Any) -> None:
    """Moves one radio of access point ``d`` to a channel (wifi.channel):
    ``PUT rest/device/<_id>`` with its radio table, only that radio's channel
    changed; the device is read again until the radio is on it, and the
    change is undone if it is not."""
    label = text(d.get("name")) or text(d.get("mac"))
    name, channel = wanted(value)
    if d.get("type") != "uap":
        raise PluginError(f"{label} is not an access point: it has no radios to change")
    if d.get("state") == 0:
        raise PluginError(f"{label} is disconnected from the controller: nothing was changed")
    if wireless_uplink(d):
        raise PluginError(
            f"{label} reaches the network over Wi-Fi (mesh): a new channel could cut it off, "
            "so Omini does not move it"
        )
    if not isinstance(d.get("_id"), str):
        raise PluginError(f"the controller did not give the id of {label}")
    entry = radio_entry(d, name)
    if entry is None:
        raise PluginError(f"{label} has no radio {name}")
    band = BANDS.get(text(entry.get("radio")))
    if band is None or channel not in CHANNELS[band]:
        raise PluginError(f"channel {channel} is not a channel of the {name} radio of {label}")
    if channel_of(entry.get("channel")) == channel:
        return
    before = [r for r in d.get("radio_table") or [] if isinstance(r, dict)]
    endpoint = f"rest/device/{d['_id']}"
    apply_verify(
        lambda: c.put(endpoint, {"radio_table": changed_table(before, name, channel)}),
        lambda: check(c, find, d["_id"], name, channel),
        lambda: c.put(endpoint, {"radio_table": before}),
        timeout=VERIFY_S,
        interval=POLL_S,
        message=f"could not move {name} of {label} to channel {channel}",
        check_in="in UniFi Network",
        undo_failed_apply=False,  # one PUT: all or nothing
        sleep=time.sleep,
    )
