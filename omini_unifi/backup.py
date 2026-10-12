"""Configuration backups: each adopted device's settings as sorted JSON.

UniFi has no text export of a device, so the backup is built from what the
controller keeps: the device's own settings from ``stat/device`` (only the
configuration keys below, never its live status) and, on one device of the
site, the site's settings (networks, Wi-Fi networks, port profiles and
``get/setting``). Secrets are left out: every ``x_``-prefixed field (UniFi's
convention for secrets: Wi-Fi passphrases, RADIUS and VPN keys, SSH
passwords...) and any field named like a password, passphrase, secret, key
or SNMP community. Two backups of an unchanged site are equal.
"""

from __future__ import annotations

import json
from typing import Any

from omini_sdk import ConfigBackup, PluginError, log

from omini_unifi.client import ApiError, Client, Forbidden, NotFound

# The configuration a device keeps in the controller (stat/device); the rest
# of its entry is live status (counters, uptime, clients, temperatures...).
DEVICE_KEYS = (
    "mac",
    "model",
    "type",
    "name",
    "config_network",
    "port_overrides",
    "radio_table",
    "ethernet_overrides",
    "wlan_overrides",
    "wlangroup_id_ng",
    "wlangroup_id_na",
    "mgmt_network_id",
    "led_override",
    "led_override_color",
    "led_override_color_brightness",
    "outdoor_mode_override",
    "mesh_sta_vap_enabled",
    "stp_version",
    "stp_priority",
    "jumboframe_enabled",
    "flowctrl_enabled",
    "dot1x_portctrl_enabled",
    "dot1x_fallback_networkconf_id",
    "snmp_contact",
    "snmp_location",
    "switch_vlan_enabled",
    "lcm_brightness_override",
    "lcm_idle_timeout_override",
    "volume",
)
# Fields of radio_table that are live readings, not settings.
RADIO_LIVE = {"current_antenna_gain", "radio_caps", "radio_caps2"}

# The site's settings, each in the anchor device's backup.
SITE_READS = (
    ("networks", "rest/networkconf"),
    ("wifi_networks", "rest/wlanconf"),
    ("port_profiles", "rest/portconf"),
    ("settings", "get/setting"),
)
# Live or per-boot fields of the site's settings.
SITE_LIVE = {"last_seen", "update_time", "time", "timestamp"}

SECRET_WORDS = ("password", "passphrase", "secret", "preshared", "psk", "private_key", "auth_key")
SECRET_KEYS = {"community"}

GATEWAYS = {"ugw", "udm", "uxg"}


def secret(key: str) -> bool:
    k = key.lower()
    return k.startswith("x_") or k in SECRET_KEYS or any(w in k for w in SECRET_WORDS)


def clean(value: Any, live: frozenset[str] | set[str] = frozenset()) -> Any:
    """``value`` without secrets nor live fields, at every depth."""
    if isinstance(value, dict):
        return {
            k: clean(v, live)
            for k, v in value.items()
            if isinstance(k, str) and not secret(k) and k not in live
        }
    if isinstance(value, list):
        return [clean(v, live) for v in value]
    return value


def ordered(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    """A list of the controller's records in a stable order (it may answer
    them in any order)."""
    return sorted(rows, key=lambda r: tuple(str(r.get(k) or "") for k in (*keys, "_id")))


def device_settings(d: dict[str, Any]) -> dict[str, Any]:
    out = {k: d[k] for k in DEVICE_KEYS if k in d and not secret(k)}
    if "radio_table" in out:
        out["radio_table"] = ordered(
            [r for r in out["radio_table"] if isinstance(r, dict)], "radio", "name"
        )
        out["radio_table"] = [
            {k: v for k, v in r.items() if k not in RADIO_LIVE} for r in out["radio_table"]
        ]
    if "port_overrides" in out:
        out["port_overrides"] = sorted(
            (o for o in out["port_overrides"] if isinstance(o, dict)),
            key=lambda o: o.get("port_idx") if isinstance(o.get("port_idx"), int) else 0,
        )
    return clean(out)


def site_settings(c: Client) -> dict[str, Any]:
    """The site's networks, Wi-Fi networks, port profiles and settings; a read
    the account or version cannot do is left out (the same every time)."""
    out: dict[str, Any] = {}
    for name, endpoint in SITE_READS:
        try:
            rows = c.get(endpoint)
        except (Forbidden, NotFound, ApiError) as e:
            log.info("%s left out of the backup: %s", endpoint, type(e).__name__)
            continue
        if name == "settings":
            out[name] = {
                str(r.get("key")): clean(r, SITE_LIVE) for r in ordered(rows, "key") if r.get("key")
            }
        else:
            out[name] = [clean(r, SITE_LIVE) for r in ordered(rows, "name")]
    return out


def anchor(devices: dict[str, dict[str, Any]]) -> str | None:
    """The device whose backup holds the site's settings: the gateway, else
    the switch, else the device with the lowest MAC (stable from one backup
    to the next)."""
    for kinds in (GATEWAYS, {"usw"}, None):
        keys = sorted(m for m, d in devices.items() if kinds is None or d.get("type") in kinds)
        if keys:
            return keys[0]
    return None


def backups(c: Client, devices: dict[str, dict[str, Any]]) -> list[ConfigBackup]:
    """One backup per adopted device (keyed by MAC, as collect reports it)."""
    if not devices:
        raise PluginError("the controller has no adopted device to back up")
    home = anchor(devices)
    site = site_settings(c)
    out = []
    for m in sorted(devices):
        content: dict[str, Any] = {"device": device_settings(devices[m])}
        if m == home:
            content["site"] = site
        out.append(
            ConfigBackup(
                device=m, format="json", content=json.dumps(content, sort_keys=True, indent=2)
            )
        )
    return out
