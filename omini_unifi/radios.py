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
from typing import Any

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
