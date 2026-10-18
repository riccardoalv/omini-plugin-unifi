"""What a UniFi gateway (UDM, UCG, USG, UXG) serves: its networks, fixed
addresses, DHCP clients, local DNS names, paused devices and port forwards,
read every round and changed by network.*, reservation.*, dns.* and pause.*.

- **Networks** are the site's "corporate" networks (``rest/networkconf``):
  ``vlan`` (VLAN 1 when not ``vlan_enabled``), ``ip_subnet`` (the gateway's
  address with the prefix, "192.168.30.1/24"), ``dhcpd_enabled``,
  ``dhcpd_start`` and ``dhcpd_stop``. Access is read from
  ``internet_access_enabled`` and ``network_isolation_enabled`` when the
  controller has both. Guest and "VLAN only" networks are not the router's.
- **Fixed addresses** are clients (``rest/user``) with ``use_fixedip`` and
  ``fixed_ip``.
- **DHCP clients** are the connected clients (``stat/sta``) whose address is
  in a network the gateway serves DHCP on; the controller gives no expiry.
- **Local DNS names** are the v2 static DNS records (``static-dns``, Network
  8.2 or later).
- **Pause** is a v2 traffic rule (``trafficrules``, Network 7 or later) that
  blocks the internet, and only the internet, for the client's MAC: the
  device keeps its own network. Never ``cmd/stamgr block-sta``, which also
  cuts it off the LAN.
- **Port forwards** are ``rest/portforward`` (read only).

Every change is read back until the controller shows it; one it does not
show in time is undone."""

from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Callable
from typing import Any

from omini_sdk import PluginError
from omini_sdk.changes import Pending, apply_verify

from omini_unifi.client import ApiError, Client, Forbidden, NotFound
from omini_unifi.wifi import text, vlan_of

# How long a change waits for the controller to show it (the SDK's window).
VERIFY_S = 30
POLL_S = 3

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# What Omini writes in its traffic rules' description, to find them again.
MARK = "Omini"

TRAFFIC_RULES = "trafficrules"
STATIC_DNS = "static-dns"

ACCESS = {
    # (internet_access_enabled, network_isolation_enabled) → Omini's access
    (True, True): "internet",
    (True, False): "all",
    (False, True): "local",
}
ACCESS_FIELDS = ("internet_access_enabled", "network_isolation_enabled")

PROTOCOLS = {"tcp_udp": "tcp/udp", "tcp": "tcp", "udp": "udp", "both": "tcp/udp"}


def norm_mac(value: Any) -> str:
    v = text(value).lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", v):
        v = ":".join(v[i : i + 2] for i in range(0, 12, 2))
    return v if MAC.match(v) and v != "00:00:00:00:00:00" else ""


def ipv4(value: Any) -> str | None:
    try:
        return str(ipaddress.IPv4Address(text(value)))
    except ValueError:
        return None


# --- networks ------------------------------------------------------------------


def corporate(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The networks the gateway routes (with an address and DHCP of its own)."""
    return [
        n for n in networks if n.get("purpose") == "corporate" and isinstance(n.get("_id"), str)
    ]


def access_of(n: dict[str, Any]) -> str | None:
    """Omini's access choice from the network's two switches; "custom" for
    the one pair that is none of them; unknown when the controller lacks them."""
    internet, isolated = (n.get(k) for k in ACCESS_FIELDS)
    if not isinstance(internet, bool) or not isinstance(isolated, bool):
        return None
    return ACCESS.get((internet, isolated), "custom")


def interface_of(n: dict[str, Any]) -> str | None:
    try:
        return str(ipaddress.ip_interface(text(n.get("ip_subnet"))))
    except ValueError:
        return None


def network(n: dict[str, Any]) -> dict[str, Any] | None:
    vid = vlan_of(n)
    if vid is None or not 1 <= vid <= 4094:
        return None
    out: dict[str, Any] = {"vlan": vid, "name": text(n.get("name")) or None}
    out["gateway"] = interface_of(n)
    if isinstance(n.get("dhcpd_enabled"), bool):
        dhcp: dict[str, Any] = {"enabled": n["dhcpd_enabled"]}
        if n["dhcpd_enabled"]:
            dhcp["from"] = ipv4(n.get("dhcpd_start"))
            dhcp["to"] = ipv4(n.get("dhcpd_stop"))
        out["dhcp"] = {k: v for k, v in dhcp.items() if v is not None}
    out["access"] = access_of(n)
    return {k: v for k, v in out.items() if v is not None}


def networks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    out = []
    for n in corporate(rows):
        x = network(n)
        if x and x["vlan"] not in seen:
            seen.add(x["vlan"])
            out.append(x)
    return sorted(out, key=lambda x: x["vlan"])


def domain(rows: list[dict[str, Any]]) -> str | None:
    """The domain the gateway hands out: the LAN's (VLAN 1), else the first
    network's that has one."""
    nets = sorted(corporate(rows), key=lambda n: vlan_of(n) or 9999)
    return next((d.lower() for n in nets if (d := text(n.get("domain_name")))), None)


def served(rows: list[dict[str, Any]]) -> list[ipaddress.IPv4Network]:
    """The subnets the gateway serves DHCP on."""
    out = []
    for n in corporate(rows):
        if n.get("dhcpd_enabled") is True:
            try:
                out.append(ipaddress.IPv4Interface(text(n.get("ip_subnet"))).network)
            except ValueError:
                continue
    return out


# --- DHCP: fixed addresses and clients -------------------------------------------


def reservations(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for u in users:
        m, ip = norm_mac(u.get("mac")), ipv4(u.get("fixed_ip"))
        if u.get("use_fixedip") is True and m and ip:
            name = text(u.get("name")) or text(u.get("hostname")) or None
            out.append({k: v for k, v in {"mac": m, "ip": ip, "name": name}.items() if v})
    return sorted(out, key=lambda r: r["mac"])


def leases(stations: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Connected clients with an address in a network the gateway serves
    DHCP on (the controller says nothing of the lease's expiry)."""
    subnets = served(rows)
    out = []
    for s in stations:
        m, ip = norm_mac(s.get("mac")), ipv4(s.get("ip"))
        if not m or not ip or not any(ipaddress.IPv4Address(ip) in n for n in subnets):
            continue
        lease = {"ip": ip, "mac": m, "hostname": text(s.get("hostname")) or None}
        out.append({k: v for k, v in lease.items() if v})
    return out


# --- DNS -----------------------------------------------------------------------


def dns_records(rows: Any) -> list[dict[str, str]]:
    """The enabled A and AAAA records of the static DNS."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("enabled") is False:
            continue
        if text(r.get("record_type")).upper() not in ("A", "AAAA"):
            continue
        name, ip = text(r.get("key")).lower().rstrip("."), text(r.get("value"))
        if name and ip:
            out.append({"name": name, "ip": ip})
    return out


# --- pause ---------------------------------------------------------------------


def targets(rule: dict[str, Any]) -> list[str]:
    """The client MACs a traffic rule applies to; empty when it applies to
    anything else too (a network, every device...)."""
    devices = rule.get("target_devices")
    if not isinstance(devices, list) or not devices:
        return []
    macs = []
    for t in devices:
        m = norm_mac(t.get("client_mac")) if isinstance(t, dict) else ""
        if not m or text(t.get("type")).upper() != "CLIENT":
            return []
        macs.append(m)
    return macs


def ours(rule: dict[str, Any]) -> bool:
    return text(rule.get("description")).startswith(MARK)


def blocks_internet(rule: dict[str, Any]) -> bool:
    """An enabled rule that blocks the whole internet, always, for clients."""
    schedule = rule.get("schedule") if isinstance(rule.get("schedule"), dict) else {}
    return (
        rule.get("enabled") is True
        and text(rule.get("action")).upper() == "BLOCK"
        and text(rule.get("matching_target")).upper() == "INTERNET"
        and text(schedule.get("mode") or "ALWAYS").upper() == "ALWAYS"
        and bool(targets(rule))
    )


def paused(rules: Any, stations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each client an internet block applies to: Omini's rules, and simple
    ones set by hand (named by their description)."""
    ips: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for s in stations:
        m = norm_mac(s.get("mac"))
        if m and text(s.get("ip")):
            ips.setdefault(m, []).append(text(s.get("ip")))
        if m and (n := text(s.get("name")) or text(s.get("hostname"))):
            names[m] = n
    out: dict[str, dict[str, Any]] = {}
    for r in rules if isinstance(rules, list) else []:
        if not isinstance(r, dict) or not blocks_internet(r):
            continue
        for m in targets(r):
            if m in out and out[m].get("rule") is None:
                continue  # Omini's rule wins over one set by hand
            p: dict[str, Any] = {"mac": m, "ips": ips.get(m), "name": names.get(m)}
            if not ours(r):
                p["rule"] = text(r.get("description")) or "Traffic rule"
            out[m] = {k: v for k, v in p.items() if v}
    return [out[m] for m in sorted(out)]


# --- port forwards ---------------------------------------------------------------


def ports_of(value: Any) -> list[tuple[int, int | None]]:
    """ "443" → [(443, None)], "8000-8010" → [(8000, 8010)], "80,443" → both."""
    out: list[tuple[int, int | None]] = []
    for part in str(value if value is not None else "").split(","):
        lo, _, hi = part.strip().partition("-")
        if not lo.strip().isdigit() or (hi and not hi.strip().isdigit()):
            return []
        a, b = int(lo), int(hi) if hi else None
        if not 1 <= a <= 65535 or (b is not None and not a <= b <= 65535):
            return []
        out.append((a, b if b != a else None))
    return out


def port_forwards(rows: list[dict[str, Any]], wan_names: dict[str, str]) -> list[dict[str, Any]]:
    """rest/portforward: proto, dst_port (the internet side), fwd and
    fwd_port (the device), src, pfwd_interface, name, enabled."""
    out = []
    for f in rows:
        proto = PROTOCOLS.get(text(f.get("proto")).lower())
        external = ports_of(f.get("dst_port"))
        if not proto or not external:
            continue
        internal = ports_of(f.get("fwd_port"))
        if len(internal) != len(external):
            internal = internal[:1] * len(external) if len(internal) == 1 else []
        src = text(f.get("src"))
        for i, (port, end) in enumerate(external):
            pf = {
                "protocol": proto,
                "external_port": port,
                "external_port_end": end,
                "internal_ip": text(f.get("fwd")) or None,
                "internal_port": internal[i][0] if internal else None,
                "source": src if src and src.lower() != "any" else None,
                "interface": wan_names.get(text(f.get("pfwd_interface")).lower()),
                "description": text(f.get("name")) or None,
                "enabled": f.get("enabled") if isinstance(f.get("enabled"), bool) else None,
                "upnp": False,
            }
            out.append({k: v for k, v in pf.items() if v is not None})
    return out


# --- changes -------------------------------------------------------------------


def settle(c: Client, done: Callable[[], bool], what: str, undo: Callable[[], None] | None) -> None:
    """Reads until ``done()`` (up to VERIFY_S; a read that fails meanwhile is
    read again); a change not shown in time is undone (when it can be) and
    reported."""

    def shown() -> bool:
        if done():
            return True
        raise Pending(f"the controller did not show it within {VERIFY_S} s")

    apply_verify(
        lambda: None,
        shown,
        undo,
        timeout=VERIFY_S,
        interval=POLL_S,
        message=f"could not apply {what}",
        check_in="in UniFi Network",
        sleep=time.sleep,
    )


def network_for_ip(rows: list[dict[str, Any]], ip: str) -> dict[str, Any]:
    addr = ipaddress.IPv4Address(ip)
    for n in corporate(rows):
        try:
            if addr in ipaddress.IPv4Interface(text(n.get("ip_subnet"))).network:
                return n
        except ValueError:
            continue
    raise PluginError(f"no network of the gateway holds {ip}")


def user_of(c: Client, mac: str) -> dict[str, Any] | None:
    return next((u for u in c.get("rest/user") if norm_mac(u.get("mac")) == mac), None)


def fixed(c: Client, mac: str) -> str | None:
    u = user_of(c, mac)
    return ipv4(u.get("fixed_ip")) if u and u.get("use_fixedip") is True else None


def set_reservation(c: Client, value: dict[str, Any]) -> None:
    """reservation.set {mac, ip, name}: the client's fixed address
    (``use_fixedip``, ``fixed_ip`` and the ``network_id`` that holds it);
    a client the controller has never seen is created with that name."""
    mac, ip = norm_mac(value.get("mac")), ipv4(value.get("ip"))
    if not mac or not ip:
        raise PluginError("a fixed address needs a MAC and an IPv4 address")
    net = network_for_ip(c.get("rest/networkconf"), ip)
    user = user_of(c, mac)
    body = {"use_fixedip": True, "fixed_ip": ip, "network_id": net["_id"]}
    undo: Callable[[], None] | None
    if user is None:
        name = text(value.get("name"))
        c.post("rest/user", {"mac": mac, **({"name": name} if name else {}), **body})

        def undo() -> None:
            u = user_of(c, mac)
            if u and isinstance(u.get("_id"), str):
                c.put(f"rest/user/{u['_id']}", {"use_fixedip": False})

    else:
        if fixed(c, mac) == ip and user.get("network_id") == net["_id"]:
            return
        endpoint = f"rest/user/{user['_id']}"
        previous = {k: user[k] for k in body if k in user}
        previous.setdefault("use_fixedip", False)
        c.put(endpoint, body)

        def undo() -> None:
            c.put(endpoint, previous)

    settle(c, lambda: fixed(c, mac) == ip, f"the fixed address {ip}", undo)


def delete_reservation(c: Client, value: dict[str, Any]) -> None:
    mac = norm_mac(value.get("mac"))
    if not mac:
        raise PluginError("a MAC address is needed to free a fixed address")
    user = user_of(c, mac)
    if user is None or user.get("use_fixedip") is not True:
        return  # no fixed address: nothing to free
    c.put(f"rest/user/{user['_id']}", {"use_fixedip": False})
    settle(c, lambda: fixed(c, mac) is None, "the freed address", None)


def wanted_network(value: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    """The VLAN, the gateway (address/prefix) and the DHCP asked, checked."""
    vid = value.get("vlan")
    if isinstance(vid, bool) or not isinstance(vid, int) or not 1 <= vid <= 4094:
        raise PluginError(f"a VLAN id is a number from 1 to 4094, not {vid!r}")
    try:
        gw = ipaddress.IPv4Interface(text(value.get("gateway")))
    except ValueError as e:
        raise PluginError("the network needs its gateway as an IPv4 address/prefix") from e
    if gw.ip in (gw.network.network_address, gw.network.broadcast_address):
        raise PluginError(f"{gw} is not a host address of its network")
    dhcp = value.get("dhcp") if isinstance(value.get("dhcp"), dict) else {"enabled": False}
    if dhcp.get("enabled"):
        lo, hi = ipv4(dhcp.get("from")), ipv4(dhcp.get("to"))
        if not lo or not hi:
            raise PluginError("DHCP needs the first and last address of its range")
        if not all(ipaddress.IPv4Address(x) in gw.network for x in (lo, hi)):
            raise PluginError(f"the DHCP range must be inside {gw.network}")
        if ipaddress.IPv4Address(lo) > ipaddress.IPv4Address(hi):
            raise PluginError("the DHCP range ends before it starts")
    if value.get("mdns"):
        raise PluginError("discovery (mDNS) between networks is set in UniFi Network")
    return vid, str(gw), dhcp


def network_body(
    value: dict[str, Any], gw: str, dhcp: dict[str, Any], expressible: bool
) -> dict[str, Any]:
    body: dict[str, Any] = {"ip_subnet": gw, "dhcpd_enabled": bool(dhcp.get("enabled"))}
    if dhcp.get("enabled"):
        body["dhcpd_start"], body["dhcpd_stop"] = ipv4(dhcp["from"]), ipv4(dhcp["to"])
    access = value.get("access")
    if access in ("internet", "all", "local"):
        if not expressible:
            raise PluginError(
                "this controller has no network isolation or internet access settings: "
                "leave the access as it is and set it in UniFi Network"
            )
        internet, isolated = next(k for k, v in ACCESS.items() if v == access)
        body["internet_access_enabled"], body["network_isolation_enabled"] = internet, isolated
    elif access not in (None, "custom"):
        raise PluginError(f"unknown access {access!r}")
    if text(value.get("name")) and value.get("vlan") != 1:  # the LAN keeps its name
        body["name"] = text(value.get("name"))
    return body


def matches(n: dict[str, Any] | None, body: dict[str, Any]) -> bool:
    if n is None:
        return False
    return all(
        (interface_of(n) if k == "ip_subnet" else n.get(k)) == v
        for k, v in body.items()
        if k not in ("purpose", "networkgroup", "enabled", "vlan_enabled", "vlan")
    )


def by_vlan(rows: list[dict[str, Any]], vid: int) -> dict[str, Any] | None:
    return next((n for n in rows if n.get("purpose") != "wan" and vlan_of(n) == vid), None)


def set_network(c: Client, value: dict[str, Any]) -> None:
    """network.set: the VLAN's network, a corporate one with the gateway's
    address, its DHCP range and, where the controller has them, its access
    switches. A "VLAN only" network of that VLAN is given the gateway (its
    ports and Wi-Fi stay); a guest network is left to UniFi Network."""
    vid, gw, dhcp = wanted_network(value)
    rows = c.get("rest/networkconf")
    expressible = any(all(k in n for k in ACCESS_FIELDS) for n in corporate(rows))
    body = network_body(value, gw, dhcp, expressible)
    if any(n.get("purpose") == "wan" and vlan_of(n) == vid for n in rows):
        raise PluginError(f"VLAN {vid} is used by a WAN of the site: pick another")
    current = by_vlan(rows, vid)
    if current is not None and current.get("purpose") not in ("corporate", "vlan-only"):
        label = text(current.get("name")) or f"VLAN {vid}"
        raise PluginError(
            f'"{label}" (VLAN {vid}) is a {current.get("purpose") or "special"} network: '
            "change it in UniFi Network"
        )
    undo: Callable[[], None]
    if current is None:
        if vid == 1:
            raise PluginError("the site has no LAN network to change")
        body = {
            "name": f"VLAN {vid}",
            **body,
            "purpose": "corporate",
            "networkgroup": "LAN",
            "vlan_enabled": True,
            "vlan": vid,
            "enabled": True,
        }
        made = c.post("rest/networkconf", body)
        made_id = made[0].get("_id") if made else None

        def undo() -> None:
            n = by_vlan(c.get("rest/networkconf"), vid)
            _id = made_id or (n or {}).get("_id")
            if _id:
                c.delete(f"rest/networkconf/{_id}")

    else:
        if current.get("purpose") == "vlan-only":
            body.update(purpose="corporate", networkgroup="LAN")
        if matches(current, body) and current.get("purpose") == "corporate":
            return
        endpoint = f"rest/networkconf/{current['_id']}"
        previous = {k: current[k] for k in body if k in current}
        c.put(endpoint, body)

        def undo() -> None:
            c.put(endpoint, previous)

    settle(
        c,
        lambda: matches(by_vlan(c.get("rest/networkconf"), vid), body),
        f"the network of VLAN {vid}",
        undo,
    )


def delete_network(c: Client, value: dict[str, Any]) -> None:
    vid = value.get("vlan")
    if isinstance(vid, bool) or not isinstance(vid, int):
        raise PluginError(f"network.delete takes {{vlan}}, not {value!r}")
    if vid == 1:
        raise PluginError("the LAN cannot be removed from Omini")
    n = by_vlan(c.get("rest/networkconf"), vid)
    if n is None or n.get("purpose") != "corporate":
        raise PluginError(f"the gateway serves no network on VLAN {vid}")
    label = text(n.get("name")) or f"VLAN {vid}"
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
    settle(
        c,
        lambda: by_vlan(c.get("rest/networkconf"), vid) is None,
        f"the removal of VLAN {vid}",
        None,
    )


def static_dns(c: Client) -> list[dict[str, Any]]:
    try:
        rows = c.v2("GET", STATIC_DNS)
    except NotFound as e:
        raise PluginError(
            "this controller has no static DNS records (UniFi Network 8.2 or later): "
            "set the name in UniFi Network or on the DNS server"
        ) from e
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def a_record(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next(
        (
            r
            for r in rows
            if text(r.get("key")).lower().rstrip(".") == name
            and text(r.get("record_type")).upper() == "A"
        ),
        None,
    )


def set_dns(c: Client, value: dict[str, Any]) -> None:
    """dns.set {name, ip}: an A record of the static DNS."""
    name, ip = text(value.get("name")).lower().rstrip("."), ipv4(value.get("ip"))
    if not name or not ip:
        raise PluginError("a DNS name needs a name and an IPv4 address")
    current = a_record(static_dns(c), name)
    undo: Callable[[], None]
    if current is None:
        made = c.v2(
            "POST",
            STATIC_DNS,
            {
                "key": name,
                "value": ip,
                "record_type": "A",
                "enabled": True,
                "ttl": 0,
                "port": 0,
                "priority": 0,
                "weight": 0,
            },
        )
        made_id = made.get("_id") if isinstance(made, dict) else None

        def undo() -> None:
            r = a_record(static_dns(c), name)
            _id = made_id or (r or {}).get("_id")
            if _id:
                c.v2("DELETE", f"{STATIC_DNS}/{_id}")

    else:
        if text(current.get("value")) == ip and current.get("enabled") is not False:
            return
        endpoint = f"{STATIC_DNS}/{current['_id']}"
        c.v2("PUT", endpoint, {**current, "value": ip, "enabled": True})

        def undo() -> None:
            c.v2("PUT", endpoint, current)

    def done() -> bool:
        r = a_record(static_dns(c), name)
        return r is not None and text(r.get("value")) == ip and r.get("enabled") is not False

    settle(c, done, f"the name {name}", undo)


def delete_dns(c: Client, value: dict[str, Any]) -> None:
    """dns.delete {name}: its A records (AAAA records others made are kept)."""
    name = text(value.get("name")).lower().rstrip(".")
    if not name:
        raise PluginError("dns.delete takes {name}")
    found = [
        r
        for r in static_dns(c)
        if text(r.get("key")).lower().rstrip(".") == name
        and text(r.get("record_type")).upper() == "A"
    ]
    for r in found:
        c.v2("DELETE", f"{STATIC_DNS}/{r['_id']}")
    if found:
        settle(c, lambda: a_record(static_dns(c), name) is None, f"the removal of {name}", None)


def traffic_rules(c: Client) -> list[dict[str, Any]]:
    try:
        rows = c.v2("GET", TRAFFIC_RULES)
    except NotFound as e:
        raise PluginError(
            "this controller has no traffic rules (UniFi Network 7 or later): Omini pauses a "
            "device's internet only through them, never by blocking the client"
        ) from e
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def pause_rule(mac: str, name: str) -> dict[str, Any]:
    return {
        "description": f"{MARK}: pause {name or mac}"[:128],
        "action": "BLOCK",
        "enabled": True,
        "matching_target": "INTERNET",
        "target_devices": [{"client_mac": mac, "type": "CLIENT"}],
        "schedule": {"mode": "ALWAYS"},
        "app_category_ids": [],
        "app_ids": [],
        "domains": [],
        "ip_addresses": [],
        "ip_ranges": [],
        "network_ids": [],
        "regions": [],
        "bandwidth_limit": {
            "enabled": False,
            "download_limit_kbps": 1024,
            "upload_limit_kbps": 1024,
        },
    }


def is_paused(c: Client, mac: str) -> bool:
    return any(blocks_internet(r) and mac in targets(r) for r in traffic_rules(c))


def set_pause(c: Client, value: dict[str, Any], own: set[str]) -> None:
    """pause.set {mac, ips, name}: a traffic rule of Omini's that blocks the
    internet for the client's MAC (its own network stays reachable)."""
    mac = norm_mac(value.get("mac"))
    if not mac:
        raise PluginError("a MAC address is needed to pause a device")
    if mac in own:
        raise PluginError("a UniFi device of the site cannot be paused")
    rules = traffic_rules(c)
    if any(blocks_internet(r) and mac in targets(r) for r in rules):
        return  # already paused
    mine = next((r for r in rules if ours(r) and targets(r) == [mac]), None)
    undo: Callable[[], None]
    if mine is not None and isinstance(mine.get("_id"), str):
        endpoint = f"{TRAFFIC_RULES}/{mine['_id']}"
        c.v2("PUT", endpoint, {**mine, "enabled": True})

        def undo() -> None:
            c.v2("PUT", endpoint, mine)

    else:
        made = c.v2("POST", TRAFFIC_RULES, pause_rule(mac, text(value.get("name"))))
        made_id = made.get("_id") if isinstance(made, dict) else None

        def undo() -> None:
            ids = [made_id] if made_id else []
            ids = ids or [
                r["_id"]
                for r in traffic_rules(c)
                if ours(r) and targets(r) == [mac] and r.get("_id")
            ]
            for _id in ids:
                c.v2("DELETE", f"{TRAFFIC_RULES}/{_id}")

    settle(c, lambda: is_paused(c, mac), "the pause", undo)


def delete_pause(c: Client, value: dict[str, Any]) -> None:
    """pause.delete {mac}: Omini's rule for the device is deleted; a block set
    by hand is turned off when it blocks that device alone."""
    mac = norm_mac(value.get("mac"))
    if not mac:
        raise PluginError("a MAC address is needed to resume a device")
    changed = False
    for r in traffic_rules(c):
        if mac not in targets(r) or not isinstance(r.get("_id"), str):
            continue
        endpoint = f"{TRAFFIC_RULES}/{r['_id']}"
        if ours(r) and targets(r) == [mac]:
            c.v2("DELETE", endpoint)
        elif ours(r):
            rest = [t for t in r["target_devices"] if norm_mac(t.get("client_mac")) != mac]
            c.v2("PUT", endpoint, {**r, "target_devices": rest})
        elif not blocks_internet(r):
            continue
        elif targets(r) == [mac]:
            c.v2("PUT", endpoint, {**r, "enabled": False})
        else:
            label = text(r.get("description")) or "a traffic rule"
            raise PluginError(
                f'the device is blocked by "{label}", set by hand for several devices: '
                "change it in UniFi Network"
            )
        changed = True
    if changed:
        settle(c, lambda: not is_paused(c, mac), "the end of the pause", None)
