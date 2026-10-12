"""The site's Wi-Fi networks (rest/wlanconf) and the VLAN each one puts its
clients in, read every round and changed by wifi.set and wifi.delete.

A UniFi network (rest/networkconf) carries the VLAN: a WLAN points at one
(``networkconf_id``). A VLAN with no network yet gets a "VLAN only" one, as
the controller's own form does. The password goes to the controller and is
never logged nor returned."""

from __future__ import annotations

import time
from typing import Any

from omini_sdk import PluginError

from omini_unifi.client import Client, NotFound


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def vlan_of(network: dict[str, Any] | None) -> int | None:
    """A UniFi network's VLAN: its id when tagged; the untagged LAN is VLAN 1."""
    if not network:
        return None
    if network.get("vlan_enabled") and isinstance(network.get("vlan"), int | str):
        try:
            return int(network["vlan"])
        except ValueError:
            return None
    return 1 if network.get("purpose") == "corporate" else None


def security_of(w: dict[str, Any]) -> str:
    if w.get("security") == "open":
        return "open"
    if w.get("wpa3_transition"):
        return "wpa2wpa3"
    if w.get("wpa3_support") or w.get("wpa_mode") == "wpa3":
        return "wpa3"
    return "wpa2"


def wifi_networks(
    wlans: list[dict[str, Any]], networks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each WLAN of the site: its name, VLAN, security and whether it is on."""
    by_id = {n.get("_id"): n for n in networks}
    out = []
    for w in wlans:
        name = text(w.get("name"))
        if not name:
            continue
        vid = vlan_of(by_id.get(w.get("networkconf_id")))
        if vid is None and w.get("vlan_enabled") and str(w.get("vlan") or "").isdigit():
            vid = int(w["vlan"])  # older controllers: the VLAN on the WLAN itself
        out.append(
            {
                "ssid": name,
                "vlan": vid if vid and vid != 1 else None,
                "security": security_of(w),
                "enabled": w.get("enabled") is not False,
            }
        )
    return out


def network_for(c: Client, networks: list[dict[str, Any]], vid: int) -> str:
    """The id of the UniFi network that carries the VLAN, made when missing."""
    for n in networks:
        if vlan_of(n) == vid:
            return str(n["_id"])
    if vid == 1:
        raise PluginError("the site has no untagged LAN network to put the Wi-Fi on")
    made = c.post(
        "rest/networkconf",
        {
            "name": f"VLAN {vid}",
            "purpose": "vlan-only",
            "vlan_enabled": True,
            "vlan": vid,
            "enabled": True,
        },
    )
    if made and made[0].get("_id"):
        return str(made[0]["_id"])
    for n in c.get("rest/networkconf"):
        if vlan_of(n) == vid:
            return str(n["_id"])
    raise PluginError(f"the controller did not create a network for VLAN {vid}")


def user_group(c: Client) -> str | None:
    try:
        groups = c.get("rest/usergroup")
    except NotFound:
        return None
    default = next((g for g in groups if text(g.get("name")).lower() == "default"), None)
    return str((default or (groups[0] if groups else {})).get("_id") or "") or None


def set_wifi(c: Client, want: dict[str, Any]) -> None:
    ssid = str(want["ssid"])
    wlans = c.get("rest/wlanconf")
    networks = c.get("rest/networkconf")
    current = next(
        (w for w in wlans if text(w.get("name")) in (str(want.get("previous") or ""), ssid)), None
    )
    body: dict[str, Any] = {"name": ssid}
    if want.get("vlan") is not None or current is None:
        body["networkconf_id"] = network_for(c, networks, int(want.get("vlan") or 1))
    if want.get("enabled") is not None:
        body["enabled"] = bool(want["enabled"])
    if want.get("password"):
        body["x_passphrase"] = str(want["password"])
    if current is not None:
        if current.get("security") == "open" and want.get("password"):
            body["security"], body["wpa_mode"] = "wpapsk", "wpa2"
        c.put(f"rest/wlanconf/{current['_id']}", body)
        return
    if not want.get("password"):
        raise PluginError("a new Wi-Fi network needs a password")
    body.update({"security": "wpapsk", "wpa_mode": "wpa2", "wlan_band": "both"})
    body.setdefault("enabled", True)
    group = user_group(c)
    if group:
        body["usergroup_id"] = group
    # The access point groups an existing WLAN is on (every AP by default).
    groups = next((w.get("ap_group_ids") for w in wlans if w.get("ap_group_ids")), None)
    if groups:
        body["ap_group_ids"] = groups
    c.post("rest/wlanconf", body)


def delete_wifi(c: Client, ssid: str) -> None:
    for w in c.get("rest/wlanconf"):
        if text(w.get("name")) == ssid:
            c.delete(f"rest/wlanconf/{w['_id']}")
            return
    raise PluginError(f'the site has no Wi-Fi network "{ssid}"')


def check(c: Client, want: dict[str, Any] | None, gone: str | None = None, tries: int = 5) -> None:
    """Reads the site again until it has the network as asked (or not at all)."""
    for i in range(tries):
        nets = wifi_networks(c.get("rest/wlanconf"), c.get("rest/networkconf"))
        if gone is not None and not any(n["ssid"] == gone for n in nets):
            return
        if want is not None:
            n = next((n for n in nets if n["ssid"] == want["ssid"]), None)
            if (
                n
                and (want.get("vlan") is None or (n["vlan"] or 1) == int(want["vlan"]))
                and (want.get("enabled") is None or n["enabled"] == bool(want["enabled"]))
            ):
                return
        if i < tries - 1:
            time.sleep(1)
    raise PluginError("the controller did not apply the Wi-Fi change")
