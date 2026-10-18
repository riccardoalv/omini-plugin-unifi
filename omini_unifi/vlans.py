"""VLANs of the site's switches: read every round, changed by port.vlans,
vlan.set and vlan.delete.

A VLAN is a UniFi network (rest/networkconf): its ``vlan`` when
``vlan_enabled``; the untagged LAN (the "Default" network) is VLAN 1. Every
switch of the site carries the site's networks.

A switch port's VLANs are its settings in the controller, in this order: the
port's override (``port_overrides``), else its port profile (``portconf_id``,
rest/portconf), else what the switch reports in its port table, else the
default ("All": the LAN native, every network tagged):

- ``native_networkconf_id``: the untagged network ("" = none);
- newer versions: ``tagged_vlan_mgmt`` "auto" (every network tagged),
  "block_all" (none) or "custom" (every network but ``excluded_networkconf_ids``);
- older ones: ``forward`` "all" / "native" / "customize" with
  ``tagged_networkconf_ids``.

A change touches only that port's override entry (its other fields, the
profile included, stay), and the switch is read again until it shows it."""

from __future__ import annotations

import time
from typing import Any

from omini_sdk import PluginError

from omini_unifi.client import ApiError, Client, Forbidden, NotFound
from omini_unifi.wifi import text, vlan_of

# How long a change waits for the controller and the switch to show it.
VERIFY_S = 40
POLL_S = 3

VLAN_KEYS = (
    "native_networkconf_id",
    "tagged_vlan_mgmt",
    "excluded_networkconf_ids",
    "tagged_networkconf_ids",
)
# ``forward`` values that say something about VLANs (not "disabled").
FORWARD_VLANS = {"all", "native", "customize"}


def site_networks(networks: list[dict[str, Any]]) -> dict[str, int]:
    """networkconf _id → VLAN id, for every network a switch port can carry
    (never the WAN ones)."""
    out: dict[str, int] = {}
    for n in networks:
        if n.get("purpose") == "wan" or not isinstance(n.get("_id"), str):
            continue
        vid = vlan_of(n)
        if vid is not None and 1 <= vid <= 4094:
            out[n["_id"]] = vid
    return out


def site_vlans(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The site's VLANs as a switch has them: [{id, name}], VLAN 1 included."""
    seen: dict[int, dict[str, Any]] = {}
    for n in networks:
        if n.get("purpose") == "wan" or not isinstance(n.get("_id"), str):
            continue
        vid = vlan_of(n)
        if vid is not None and 1 <= vid <= 4094 and vid not in seen:
            seen[vid] = {"id": vid, "name": text(n.get("name")) or None}
    return [seen[v] for v in sorted(seen)]


def port_settings(
    override: dict[str, Any], entry: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The port's VLAN settings, each key from the first that has it: the
    override, its profile (the override's, else the port's), the port table."""
    profile = profiles.get(override.get("portconf_id") or entry.get("portconf_id") or "", {})
    out: dict[str, Any] = {}
    for layer in (entry, profile, override):
        for k in VLAN_KEYS:
            if k in layer:
                out[k] = layer[k]
        if layer.get("forward") in FORWARD_VLANS:
            out["forward"] = layer["forward"]
    return out


def membership(settings: dict[str, Any], ids: dict[str, int]) -> dict[str, Any] | None:
    """{untagged, tagged} from a port's settings; None when they name a
    network the site does not have (unknown, left empty)."""
    lan = next((i for i, v in ids.items() if v == 1), None)
    native_id = settings.get("native_networkconf_id", lan)
    if native_id in (None, ""):
        untagged = None
    elif native_id in ids:
        untagged = ids[native_id]
    else:
        return None
    mode = settings.get("tagged_vlan_mgmt")
    forward = settings.get("forward")
    every = set(ids.values())
    if mode == "block_all" or (mode is None and forward == "native"):
        tagged: set[int] = set()
    elif mode == "custom":
        excluded = settings.get("excluded_networkconf_ids") or []
        tagged = {v for i, v in ids.items() if i not in excluded}
    elif mode is None and forward == "customize" and "tagged_networkconf_ids" in settings:
        tagged = {ids[i] for i in settings.get("tagged_networkconf_ids") or [] if i in ids}
    else:  # "auto", forward "all", or nothing said: every network
        tagged = every
    tagged.discard(untagged or 0)
    return {"untagged": untagged, "tagged": sorted(tagged)}


def changeable(entry: dict[str, Any]) -> bool:
    """A switch port whose VLANs Omini may change: not a member of an
    aggregate (LAG) nor a mirror port."""
    return entry.get("op_mode") in (None, "switch") and not entry.get("aggregated_by")


def port_vlans(
    d: dict[str, Any],
    idx: int,
    networks: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    entry = next((p for p in d.get("port_table") or [] if p.get("port_idx") == idx), {})
    override = next(
        (
            o
            for o in d.get("port_overrides") or []
            if isinstance(o, dict) and o.get("port_idx") == idx
        ),
        {},
    )
    if not changeable(entry):
        return None
    return membership(port_settings(override, entry, profiles), site_networks(networks))


def profiles_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {p["_id"]: p for p in rows if isinstance(p.get("_id"), str)}


# --- port.vlans ----------------------------------------------------------------


def wanted(value: Any, ids: dict[str, int]) -> dict[str, Any]:
    """The asked membership, checked against the site's networks."""
    if not isinstance(value, dict):
        raise PluginError(f"port.vlans takes {{untagged, tagged}}, not {value!r}")
    untagged = value.get("untagged")
    tagged = value.get("tagged") or []
    if untagged is not None and (isinstance(untagged, bool) or not isinstance(untagged, int)):
        raise PluginError(f"the untagged VLAN must be a number, not {untagged!r}")
    if not isinstance(tagged, list) or not all(
        isinstance(t, int) and not isinstance(t, bool) for t in tagged
    ):
        raise PluginError("the tagged VLANs must be a list of numbers")
    have = set(ids.values())
    missing = sorted({v for v in [untagged, *tagged] if v is not None} - have)
    if missing:
        listed = ", ".join(str(v) for v in missing)
        raise PluginError(f"the site has no network for VLAN {listed}: create it first")
    tagged_set = set(tagged) - {untagged}
    if untagged is None and not tagged_set:
        raise PluginError("a port needs an untagged or a tagged VLAN")
    return {"untagged": untagged, "tagged": sorted(tagged_set)}


def changed_overrides(
    before: list[dict[str, Any]],
    idx: int,
    want: dict[str, Any],
    ids: dict[str, int],
    old_style: bool,
) -> list[dict[str, Any]]:
    """The device's overrides with only port ``idx``'s VLAN fields changed:
    every other override, and every other field of this one (name, PoE,
    profile, ``forward: disabled``...), stay as they are."""
    out = [dict(o) for o in before]
    o = next((x for x in out if x.get("port_idx") == idx), None)
    if o is None:
        o = {"port_idx": idx}
        out.append(o)
    by_vlan = {v: i for i, v in ids.items()}
    o["native_networkconf_id"] = by_vlan[want["untagged"]] if want["untagged"] else ""
    if want["tagged"]:
        o["tagged_vlan_mgmt"] = "custom"
        o["excluded_networkconf_ids"] = sorted(
            i for i, v in ids.items() if v not in want["tagged"] and v != want["untagged"]
        )
    else:
        o["tagged_vlan_mgmt"] = "block_all"
        o["excluded_networkconf_ids"] = []
    if old_style:
        o["tagged_networkconf_ids"] = sorted(by_vlan[v] for v in want["tagged"])
    if o.get("forward") != "disabled":
        o["forward"] = "customize"
    return out


def reported(entry: dict[str, Any], ids: dict[str, int]) -> dict[str, Any] | None:
    """What the switch itself reports in its port table, when it does."""
    if not any(k in entry for k in VLAN_KEYS):
        return None
    return membership({k: entry[k] for k in (*VLAN_KEYS, "forward") if k in entry}, ids)


def find(raw: list[dict[str, Any]], _id: str) -> dict[str, Any] | None:
    return next((d for d in raw if d.get("_id") == _id), None)


def verify_port(
    c: Client, _id: str, idx: int, want: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> tuple[bool, str | None]:
    """Reads the device until the controller keeps the port's VLANs and the
    switch reports them (when its port table has them): (done, why not)."""
    deadline = time.monotonic() + VERIFY_S
    while True:
        try:
            d = find(c.get("stat/device"), _id)
            networks = c.get("rest/networkconf")
        except (PluginError, Forbidden, NotFound, ApiError) as e:
            why = f"the controller did not answer ({e or type(e).__name__})"
        else:
            if d is None:
                return False, "the controller no longer lists the device"
            if port_vlans(d, idx, networks, profiles) != want:
                return False, "the controller did not keep it"
            entry = next((p for p in d.get("port_table") or [] if p.get("port_idx") == idx), {})
            seen = reported(entry, site_networks(networks))
            if d.get("state") != 0 and seen in (want, None):
                return True, None
            why = f"the switch did not report it within {VERIFY_S} s"
        if time.monotonic() >= deadline:
            return False, why
        time.sleep(POLL_S)


def set_port(c: Client, d: dict[str, Any], idx: int, port: str, value: Any) -> None:
    """port.vlans on port ``idx`` of switch ``d``: undone if it does not take."""
    name = text(d.get("name")) or str(d.get("mac"))
    if d.get("type") != "usw":
        raise PluginError(f"{name} is not a switch: change its ports' VLANs in UniFi Network")
    entry = next((p for p in d.get("port_table") or [] if p.get("port_idx") == idx), {})
    if not changeable(entry):
        raise PluginError(
            f"{port} of {name} is part of an aggregate or a mirror: change it in UniFi Network"
        )
    networks = c.get("rest/networkconf")
    ids = site_networks(networks)
    want = wanted(value, ids)
    try:
        profiles = profiles_by_id(c.get("rest/portconf"))
    except (Forbidden, NotFound, ApiError):
        profiles = {}
    if port_vlans(d, idx, networks, profiles) == want:
        return
    before = [o for o in d.get("port_overrides") or [] if isinstance(o, dict)]
    o = next((x for x in before if x.get("port_idx") == idx), {})
    profile = profiles.get(o.get("portconf_id") or entry.get("portconf_id") or "", {})
    old_style = any(
        "tagged_networkconf_ids" in x and "tagged_vlan_mgmt" not in x for x in (o, profile, entry)
    )
    after = changed_overrides(before, idx, want, ids, old_style)
    endpoint = f"rest/device/{d['_id']}"
    c.put(endpoint, {"port_overrides": after})
    done, why = verify_port(c, d["_id"], idx, want, profiles)
    if not done:
        undone = "undone on the controller"
        try:
            c.put(endpoint, {"port_overrides": before})
        except PluginError as e:
            undone = f"not undone ({e}): check the port in UniFi Network"
        raise PluginError(f"could not change the VLANs of {port}: {why}; the change was {undone}")


# --- vlan.set / vlan.delete -----------------------------------------------------


def vlan_id(value: Any) -> int:
    vid = value.get("id") if isinstance(value, dict) else None
    if isinstance(vid, bool) or not isinstance(vid, int) or not 1 <= vid <= 4094:
        raise PluginError(f"a VLAN id is a number from 1 to 4094, not {vid!r}")
    return vid


def network_of(networks: list[dict[str, Any]], vid: int) -> dict[str, Any] | None:
    return next(
        (n for n in networks if n.get("purpose") != "wan" and vlan_of(n) == vid),
        None,
    )


def set_vlan(c: Client, value: Any) -> None:
    """Creates a "VLAN only" network for the VLAN, or renames its network."""
    vid = vlan_id(value)
    name = text(value.get("name"))
    if not name:
        raise PluginError("a VLAN needs a name")
    networks = c.get("rest/networkconf")
    if any(n.get("purpose") == "wan" and vlan_of(n) == vid for n in networks):
        raise PluginError(f"VLAN {vid} is used by a WAN of the site: pick another")
    current = network_of(networks, vid)
    taken = next(
        (n for n in networks if text(n.get("name")).lower() == name.lower() and n is not current),
        None,
    )
    if taken is not None:
        raise PluginError(f'the site already has a network named "{text(taken.get("name"))}"')
    if current is not None:
        if text(current.get("name")) != name:
            c.put(f"rest/networkconf/{current['_id']}", {"name": name})
    elif vid == 1:
        raise PluginError("the site has no untagged LAN network to rename")
    else:
        c.post(
            "rest/networkconf",
            {
                "name": name,
                "purpose": "vlan-only",
                "vlan_enabled": True,
                "vlan": vid,
                "enabled": True,
            },
        )
    check(c, lambda nets: (n := network_of(nets, vid)) is not None and text(n.get("name")) == name)


def delete_vlan(c: Client, value: Any) -> None:
    """Deletes a "VLAN only" network; a network with a gateway (corporate,
    guest) or the WAN's is refused: it is the router's."""
    vid = vlan_id(value)
    if vid == 1:
        raise PluginError("VLAN 1 is the site's LAN: it cannot be deleted")
    networks = c.get("rest/networkconf")
    n = network_of(networks, vid)
    if n is None:
        if any(x.get("purpose") == "wan" and vlan_of(x) == vid for x in networks):
            raise PluginError(f"VLAN {vid} is a WAN of the site: Omini does not delete it")
        raise PluginError(f"the site has no VLAN {vid}")
    label = text(n.get("name")) or f"VLAN {vid}"
    if n.get("purpose") != "vlan-only":
        raise PluginError(
            f'"{label}" (VLAN {vid}) is a network with its own gateway '
            f"({n.get('purpose') or 'unknown'}), not a VLAN only: delete it in UniFi Network"
        )
    try:
        wlans = c.get("rest/wlanconf")
    except (Forbidden, NotFound, ApiError):
        wlans = []
    used = [text(w.get("name")) for w in wlans if w.get("networkconf_id") == n["_id"]]
    if used:
        raise PluginError(
            f'"{label}" (VLAN {vid}) is the network of the Wi-Fi {", ".join(used)}: '
            "move or delete the Wi-Fi first"
        )
    c.delete(f"rest/networkconf/{n['_id']}")
    check(c, lambda nets: network_of(nets, vid) is None)


def check(c: Client, done: Any) -> None:
    """Reads the site's networks until ``done(networks)`` (up to VERIFY_S); a
    read that fails meanwhile (the controller busy provisioning) is retried."""
    deadline = time.monotonic() + VERIFY_S
    while True:
        why = ""
        try:
            if done(c.get("rest/networkconf")):
                return
        except (PluginError, Forbidden, NotFound, ApiError) as e:
            why = f" (it did not answer: {e or type(e).__name__})"
        if time.monotonic() >= deadline:
            raise PluginError(f"the controller did not apply the VLAN change{why}")
        time.sleep(POLL_S)
