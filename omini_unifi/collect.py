"""Turns UniFi Network answers into Omini devices: one per adopted device."""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlparse

from omini_sdk import (
    Change,
    Config,
    ConfigBackup,
    Device,
    FdbEntry,
    Firmware,
    Gateway,
    Host,
    Interface,
    Neighbor,
    PluginError,
    PortStp,
    PortVlans,
    Storage,
    Temperature,
    Transceiver,
    Vlan,
    WirelessClient,
    log,
)

from omini_unifi import backup as bk
from omini_unifi import vlans as vl
from omini_unifi import wifi as wlan
from omini_unifi.client import ApiError, Client, Forbidden, NotFound

T = TypeVar("T")

PLUGIN_ID = "unifi"

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

ROLES = {
    "uap": "ap",
    "usw": "switch",
    "ugw": "router",
    "udm": "router",
    "uxg": "router",
}
GATEWAYS = {"ugw", "udm", "uxg"}
# Devices whose wired ports apply may enable or disable.
SWITCHING = {"usw", *GATEWAYS}
DISABLED = "disabled"

# Device states in stat/device: 0 is disconnected (its data is stale).
DISCONNECTED = 0

BANDS = {"ng": "2.4ghz", "na": "5ghz", "6e": "6ghz"}
BAND_NAMES = {"2.4ghz": "2.4 GHz", "5ghz": "5 GHz", "6ghz": "6 GHz"}

# port_table "stp_state" → Omini's STP state ("discarding" is RSTP's blocking).
STP_STATES = {
    "forwarding": "forwarding",
    "blocking": "blocking",
    "discarding": "blocking",
    "listening": "listening",
    "learning": "learning",
    "disabled": "disabled",
}
STP_ROLES = {"root", "designated", "alternate", "backup", "disabled"}

# port_table "media" values of copper ports.
COPPER = {"FE", "GE", "2.5GE", "5GE", "10GE"}

TEMPERATURE_KINDS = {"cpu": "cpu", "board": "board", "disk": "disk"}

# What each optional endpoint gives, for the connection test's message.
OPTIONAL = {
    "stat/sta": "clients",
    "rest/networkconf": "networks (VLANs)",
    "stat/health": "WAN status",
}


def client_from(cfg: Config) -> Client:
    url, user, password = cfg.str("url"), cfg.str("username"), cfg.str("password")
    if not url or not user or not password:
        raise PluginError("the address, username and password are required")
    return Client(
        url,
        user,
        password,
        site=cfg.str("site", "default"),
        verify_tls=cfg.bool("verify_tls", False),
        state_dir=cfg.state_dir,
    )


# --- parsing helpers -------------------------------------------------------


def mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("-", ":")
    if re.fullmatch(r"[0-9a-f]{12}", v):
        v = ":".join(v[i : i + 2] for i in range(0, 12, 2))
    return v if MAC.match(v) and v != "00:00:00:00:00:00" else None


def number(value: Any) -> float | None:
    """12, 12.5, "12.5" → float; anything else → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def count(value: Any) -> int | None:
    n = number(value)
    return int(n) if n is not None and n >= 0 else None


def pct(value: Any) -> float | None:
    n = number(value)
    return round(n, 1) if n is not None and 0 <= n <= 100 else None


def text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def optional(what: str, missing: list[str], fn: Callable[[], T], default: T) -> T:
    """Runs a read that may be unavailable (role, version): skipped, never fatal."""
    try:
        return fn()
    except (Forbidden, NotFound, ApiError) as e:
        log.info("%s unavailable: %s", what, type(e).__name__)
    except PluginError as e:
        log.warning("%s failed: %s", what, e)
    missing.append(what)
    return default


# --- ports -------------------------------------------------------------------


def port_names(d: dict[str, Any]) -> dict[int, str]:
    """port_idx → a unique readable name ("Port 1", or the name given in UniFi)."""
    names: dict[int, str] = {}
    used: set[str] = set()
    for p in d.get("port_table") or []:
        idx = p.get("port_idx")
        if not isinstance(idx, int):
            continue
        name = text(p.get("name")) or f"Port {idx}"
        if name in used:
            name = f"Port {idx}"
        used.add(name)
        names[idx] = name
    return names


def connector(p: dict[str, Any]) -> str | None:
    media = str(p.get("media") or "").upper()
    if media.startswith("QSFP"):
        return "qsfp"
    if p.get("sfp_found") or media.startswith("SFP"):
        return "sfp"
    if media in COPPER:
        return "rj45"
    return None


def transceiver(p: dict[str, Any]) -> Transceiver | None:
    if not p.get("sfp_found"):
        return None
    t = Transceiver(
        vendor=text(p.get("sfp_vendor")),
        part=text(p.get("sfp_part")),
        serial=text(p.get("sfp_serial")),
        type=text(p.get("sfp_compliance")),
        temperature_c=number(p.get("sfp_temperature")),
        voltage_v=number(p.get("sfp_voltage")),
        bias_ma=number(p.get("sfp_current")),
        tx_power_dbm=number(p.get("sfp_txpower")),
        rx_power_dbm=number(p.get("sfp_rxpower")),
    )
    return t if t.model_dump(exclude_none=True) else None


def overrides(d: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """port_idx → the port's override (its settings in the controller)."""
    return {
        o["port_idx"]: o
        for o in d.get("port_overrides") or []
        if isinstance(o, dict) and isinstance(o.get("port_idx"), int)
    }


def configured(o: dict[str, Any], disabled_profiles: set[str]) -> bool | None:
    """Whether the port's override enables it: its ``forward``, or the
    "Disabled" port profile (``portconf_id``); None when it says neither."""
    if o.get("forward") == DISABLED or o.get("portconf_id") in disabled_profiles:
        return False
    return True if isinstance(o.get("forward"), str) else None


def admin_enabled(
    p: dict[str, Any], o: dict[str, Any], disabled_profiles: set[str] | None = None
) -> bool | None:
    """The port's administrative state: its configuration first, else what
    the device reports (``enable``, or ``forward`` in its port table)."""
    state = configured(o, disabled_profiles or set())
    if state is not None:
        return state
    if isinstance(p.get("enable"), bool):
        return p["enable"]
    if isinstance(p.get("forward"), str):
        return p["forward"] != DISABLED
    return None


def ports(
    d: dict[str, Any],
    names: dict[int, str],
    networks: list[dict[str, Any]] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> list[Interface]:
    out: list[Interface] = []
    settings = overrides(d)
    # The state only where Omini can change it (switches and gateways): the
    # map offers enabling and disabling the ports that report it.
    switching = d.get("type") in SWITCHING
    # VLANs only on switches, and only when the site's networks were read.
    vlan_ports = d.get("type") == "usw" and bool(networks)
    # The "Disabled" port profiles: a port given one is disabled (as apply reads it).
    disabled = {i for i, x in (profiles or {}).items() if x.get("forward") == DISABLED}
    for i, p in enumerate(d.get("port_table") or []):
        idx = p.get("port_idx")
        name = names.get(idx) if isinstance(idx, int) else None
        name = name or text(p.get("name")) or text(p.get("ifname")) or f"port{i + 1}"
        up = p.get("up") if isinstance(p.get("up"), bool) else None
        speed = count(p.get("speed"))
        member = (
            vl.port_vlans(d, idx, networks or [], profiles or {})
            if vlan_ports and isinstance(idx, int)
            else None
        )
        out.append(
            Interface(
                name=name,
                type="ethernet",
                connector=connector(p),
                mac=mac(p.get("mac")),
                up=up,
                enabled=(admin_enabled(p, settings.get(idx, {}), disabled) if switching else None),
                speed_mbps=speed if up and speed else None,
                duplex=(
                    ("full" if p["full_duplex"] else "half")
                    if up and isinstance(p.get("full_duplex"), bool)
                    else None
                ),
                media=text(p.get("media")),
                rx_bytes=count(p.get("rx_bytes")),
                tx_bytes=count(p.get("tx_bytes")),
                rx_errors=count(p.get("rx_errors")),
                tx_errors=count(p.get("tx_errors")),
                transceiver=transceiver(p),
                vlans=PortVlans.model_validate(member) if member else None,
                stp=stp(p) if switching else None,
            )
        )
    return out


def stp(p: dict[str, Any]) -> PortStp | None:
    """The port's spanning tree state (``stp_state``) and, when the switch
    reports it, its role (``stp_role``)."""
    state = STP_STATES.get(str(p.get("stp_state") or "").lower())
    role = str(p.get("stp_role") or "").lower()
    if not state and role not in STP_ROLES:
        return None
    return PortStp(state=state, role=role if role in STP_ROLES else None)


def lag_groups(d: dict[str, Any]) -> list[list[int]]:
    """The device's aggregates (LAGs), each as its ports' port_idx, the first
    (the one the others are ``aggregated_by``) first. From the port table
    (``op_mode: "aggregate"``, ``aggregated_by``, ``aggregate_num_ports``)
    and the port overrides (``op_mode: "aggregate"`` with
    ``aggregate_num_ports`` or ``aggregate_members``) of the first port."""
    groups: dict[int, set[int]] = {}
    table = [p for p in d.get("port_table") or [] if isinstance(p.get("port_idx"), int)]
    for p in table:
        by = p.get("aggregated_by")
        if isinstance(by, int) and not isinstance(by, bool) and by > 0 and by != p["port_idx"]:
            groups.setdefault(by, {by}).add(p["port_idx"])
    for p in table:
        idx = p["port_idx"]
        if p.get("op_mode") != "aggregate" or any(idx in g for g in groups.values()):
            continue
        n = count(p.get("aggregate_num_ports")) or 1
        groups.setdefault(idx, {idx}).update(range(idx, idx + n))
    for idx, o in overrides(d).items():
        followers = {m for first, g in groups.items() for m in g if m != first}
        if o.get("op_mode") != "aggregate" or idx in followers:
            continue
        members = [m for m in o.get("aggregate_members") or [] if isinstance(m, int)]
        n = count(o.get("aggregate_num_ports"))
        if members or n:
            groups.setdefault(idx, {idx}).update(members or range(idx, idx + (n or 1)))
    return [[first, *sorted(g - {first})] for first, g in sorted(groups.items()) if len(g) > 1]


def lag_name(members: list[int]) -> str:
    """ "LAG 47-48": the ports it aggregates, as UniFi shows an aggregate."""
    lo, hi = min(members), max(members)
    return f"LAG {lo}" if lo == hi else f"LAG {lo}-{hi}"


def lags(d: dict[str, Any], names: dict[int, str], ifaces: list[Interface]) -> list[Interface]:
    """Marks each aggregated port with its LAG and returns the LAGs themselves
    (type "lag"): up when a member is, its speed the sum of the members up."""
    by_name = {i.name: i for i in ifaces}
    out = []
    for members in lag_groups(d):
        name = lag_name(members)
        ports_ = [by_name[n] for idx in members if (n := names.get(idx)) and n in by_name]
        if not ports_:
            continue
        for i in ports_:
            i.lag = name
        known = [i.up for i in ports_ if i.up is not None]
        speed = sum(i.speed_mbps or 0 for i in ports_ if i.up)
        out.append(
            Interface(
                name=name,
                type="lag",
                up=any(known) if known else None,
                speed_mbps=speed or None,
            )
        )
    return out


def wifi_label(ssid: str | None, band: str | None) -> str | None:
    """ "Home · 5 GHz"; the band or the name alone when the other is unknown."""
    return " · ".join(x for x in (ssid, BAND_NAMES.get(band or "")) if x) or None


def vaps(d: dict[str, Any]) -> list[Interface]:
    """Each Wi-Fi network the access point broadcasts, with its counters."""
    out: list[Interface] = []
    for v in d.get("vap_table") or []:
        name, ssid = text(v.get("name")), text(v.get("essid"))
        if not name or not ssid:
            continue
        out.append(
            Interface(
                name=name,
                description=wifi_label(ssid, BANDS.get(str(v.get("radio") or ""))),
                type="wireless",
                mac=mac(v.get("bssid")),
                up=v.get("up") if isinstance(v.get("up"), bool) else None,
                rx_bytes=count(v.get("rx_bytes")),
                tx_bytes=count(v.get("tx_bytes")),
            )
        )
    return out


# --- WAN ---------------------------------------------------------------------


def prefix_len(netmask: Any) -> int | None:
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except (ValueError, TypeError):
        return None


def wans(d: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """("WAN", wan1), ("WAN 2", wan2) of a gateway, as stat/device has them."""
    out = []
    for n in range(1, 5):
        w = d.get(f"wan{n}")
        if isinstance(w, dict) and (w.get("ifname") or w.get("ip")):
            out.append(("WAN" if n == 1 else f"WAN {n}", w))
    return out


def health_status(health: dict[str, dict[str, Any]]) -> str | None:
    return {"ok": "up", "warning": "degraded", "error": "down"}.get(
        str((health.get("wan") or {}).get("status") or "")
    )


def wan_interfaces(
    d: dict[str, Any],
    ifaces: list[Interface],
    health: dict[str, dict[str, Any]],
) -> list[Gateway]:
    """Flags the gateway's WAN ports (adding one when no port matches) and
    describes each uplink as a Gateway."""
    by_ifname = {
        text(p.get("ifname")): ifaces[i]
        for i, p in enumerate(d.get("port_table") or [])
        if text(p.get("ifname")) and i < len(ifaces)
    }
    uptime = (health.get("wan") or {}).get("uptime_stats") or {}
    out: list[Gateway] = []
    for n, (label, w) in enumerate(wans(d)):
        iface = by_ifname.get(text(w.get("ifname")))
        if iface is None:
            iface = Interface(
                name=text(w.get("ifname")) or label,
                type="ethernet",
                mac=mac(w.get("mac")),
                rx_bytes=count(w.get("rx_bytes")),
                tx_bytes=count(w.get("tx_bytes")),
            )
            ifaces.append(iface)
        up = w.get("up") if isinstance(w.get("up"), bool) else None
        iface.wan = True
        iface.description = iface.description or label
        if iface.up is None:
            iface.up = up
        speed = count(w.get("speed"))
        if iface.speed_mbps is None and up and speed:
            iface.speed_mbps = speed
        ip, plen = text(w.get("ip")), prefix_len(w.get("netmask"))
        if ip and not iface.ips:
            iface.ips = [f"{ip}/{plen}" if plen is not None else ip]
        if up is False:
            status = "down"
        elif n == 0:
            status = health_status(health) or ("up" if up else "unknown")
        else:
            status = "up" if up else "unknown"
        stats = uptime.get(label.replace(" ", "")) or {}
        rtt = number(stats.get("latency_average"))
        if rtt is None and n == 0:
            rtt = number((health.get("www") or {}).get("latency"))
        out.append(
            Gateway(
                name=label,
                interface=iface.name,
                address=text(w.get("gateway")),
                status=status,
                rtt_ms=rtt if rtt is not None and rtt >= 0 else None,
            )
        )
    return out


def vlans(networks: list[dict[str, Any]]) -> list[Vlan]:
    out: list[Vlan] = []
    for n in networks:
        if n.get("purpose") == "wan" or not n.get("vlan_enabled"):
            continue
        vid = count(n.get("vlan"))
        if not vid or vid > 4094:
            continue
        subnet = None
        try:
            if n.get("ip_subnet"):
                subnet = str(ipaddress.ip_interface(n["ip_subnet"]).network)
        except ValueError:
            pass
        out.append(Vlan(id=vid, name=text(n.get("name")), subnet=subnet))
    return sorted(out, key=lambda v: v.id)


def lan_ips(networks: list[dict[str, Any]]) -> list[str]:
    """The gateway's own address on each LAN (ip_subnet is "192.168.1.1/24")."""
    out = []
    for n in networks:
        if n.get("purpose") == "wan" or not n.get("ip_subnet"):
            continue
        try:
            out.append(str(ipaddress.ip_interface(n["ip_subnet"]).ip))
        except ValueError:
            continue
    return out


# --- device health ---------------------------------------------------------


def temperatures(d: dict[str, Any]) -> list[Temperature]:
    out = []
    for t in d.get("temperatures") or []:
        value = number(t.get("value"))
        if value is None:
            continue
        kind = TEMPERATURE_KINDS.get(str(t.get("type") or "").lower(), "other")
        out.append(Temperature(sensor=text(t.get("name")) or kind, kind=kind, celsius=value))
    if not out and d.get("has_temperature") and number(d.get("general_temperature")) is not None:
        out.append(Temperature(sensor="General", celsius=number(d["general_temperature"])))
    return out


def storage(d: dict[str, Any]) -> list[Storage]:
    out = []
    for s in d.get("storage") or []:
        mount = text(s.get("mount_point")) or text(s.get("name"))
        if not mount:
            continue
        out.append(
            Storage(
                mount=mount,
                fs_type=text(s.get("type")),
                total_bytes=count(s.get("size")),
                used_bytes=count(s.get("used")),
            )
        )
    return out


def firmware(d: dict[str, Any]) -> Firmware | None:
    """What the controller knows from its own last check; never starts one."""
    current = text(d.get("version"))
    upgradable = d.get("upgradable") if isinstance(d.get("upgradable"), bool) else None
    latest = text(d.get("upgrade_to_firmware")) if upgradable else None
    if not current and upgradable is None:
        return None
    return Firmware(
        current=current,
        latest=latest,
        update_available=upgradable,
        updates=(1 if upgradable else 0) if upgradable is not None else None,
    )


def load_avg(d: dict[str, Any]) -> list[float] | None:
    s = d.get("sys_stats") or {}
    values = [number(s.get(k)) for k in ("loadavg_1", "loadavg_5", "loadavg_15")]
    return [v for v in values if v is not None] if all(v is not None for v in values) else None


# --- neighbors ---------------------------------------------------------------


def lldp(d: dict[str, Any], names: dict[int, str]) -> list[Neighbor]:
    out = []
    for n in (d.get("lldp_table") or []) + (
        d.get("lldp_info") if isinstance(d.get("lldp_info"), list) else []
    ):
        if not isinstance(n, dict):
            continue
        idx = n.get("local_port_idx")
        local = (names.get(idx) if isinstance(idx, int) else None) or text(n.get("local_port_name"))
        remote = mac(n.get("chassis_id"))
        if not local or not (remote or text(n.get("chassis_id"))):
            continue
        out.append(
            Neighbor(
                local_port=local,
                protocol="lldp",
                remote_mac=remote,
                remote_name=text(n.get("system_name")) or (None if remote else n["chassis_id"]),
                remote_port=text(n.get("port_descr")) or text(n.get("port_id")),
            )
        )
    return out


def uplink(
    d: dict[str, Any],
    names: dict[int, str],
    known: dict[str, dict[str, Any]],
    lldp_macs: set[str],
) -> Neighbor | None:
    """The upstream UniFi device this one hangs from (wired or Wi-Fi mesh)."""
    if d.get("type") in GATEWAYS:
        return None  # its uplink is the internet (WAN)
    u = d.get("uplink") or {}
    parent_mac = mac(u.get("uplink_mac")) or mac(u.get("ap_mac"))
    if not parent_mac or parent_mac == mac(d.get("mac")) or parent_mac not in known:
        return None
    if parent_mac in lldp_macs:
        return None  # LLDP already describes this link
    parent = known[parent_mac]
    if u.get("type") == "wireless":
        band = BANDS.get(str(u.get("radio") or ""))
        return Neighbor(
            local_port="Wi-Fi uplink",
            protocol="other",
            remote_mac=parent_mac,
            remote_name=text(parent.get("name")),
            remote_port=BAND_NAMES.get(band or ""),
        )
    idx = u.get("port_idx")
    local = names.get(idx) if isinstance(idx, int) else None
    if not local:
        local = next(
            (
                names.get(p.get("port_idx"))
                for p in d.get("port_table") or []
                if p.get("is_uplink") and names.get(p.get("port_idx"))
            ),
            None,
        )
    remote_idx = u.get("uplink_remote_port")
    remote = port_names(parent).get(remote_idx) if isinstance(remote_idx, int) else None
    return Neighbor(
        local_port=local or "Uplink",
        protocol="other",
        remote_mac=parent_mac,
        remote_name=text(parent.get("name")),
        remote_port=remote,
    )


# --- clients -----------------------------------------------------------------


def bps(value: Any) -> int | None:
    """bytes/s (UniFi's "-r" rates) → bits/s."""
    n = number(value)
    return int(n * 8) if n is not None and n >= 0 else None


def mbps(kbps: Any) -> float | None:
    n = number(kbps)
    return round(n / 1000, 1) if n is not None and n > 0 else None


def signal(value: Any) -> int | None:
    n = number(value)
    return int(n) if n is not None and n < 0 else None


def clients(
    stations: list[dict[str, Any]], devices: dict[str, dict[str, Any]]
) -> tuple[dict[str, list[WirelessClient]], dict[str, list[FdbEntry]], list[Host]]:
    """Wi-Fi clients per AP, wired clients per switch port, and every client's name."""
    wifi: dict[str, list[WirelessClient]] = {}
    fdb: dict[str, list[FdbEntry]] = {}
    hosts: list[Host] = []
    names = {m: port_names(d) for m, d in devices.items()}
    for s in stations:
        cm = mac(s.get("mac"))
        if not cm:
            continue
        if s.get("is_wired") is False:
            ap = mac(s.get("ap_mac"))
            if ap in devices:
                band = BANDS.get(str(s.get("radio") or ""))
                ssid = text(s.get("essid"))
                wifi.setdefault(ap, []).append(
                    WirelessClient(
                        mac=cm,
                        interface=wifi_label(ssid, band),
                        ssid=ssid,
                        band=band,
                        signal_dbm=signal(s.get("signal")),
                        tx_rate_mbps=mbps(s.get("tx_rate")),
                        rx_rate_mbps=mbps(s.get("rx_rate")),
                        # The AP's counters: what it sends (tx) is what the
                        # client downloads (rx).
                        rx_bps=bps(s.get("tx_bytes-r")),
                        tx_bps=bps(s.get("rx_bytes-r")),
                    )
                )
        elif s.get("is_wired"):
            sw = mac(s.get("sw_mac"))
            idx = s.get("sw_port")
            port = names.get(sw, {}).get(idx) if sw and isinstance(idx, int) else None
            if sw in devices and port:
                vid = count(s.get("vlan"))
                fdb.setdefault(sw, []).append(
                    FdbEntry(mac=cm, port=port, vlan=vid if vid and vid <= 4095 else None)
                )
        ip = text(s.get("ip"))
        hostnames = list(
            dict.fromkeys(x for x in (text(s.get("name")), text(s.get("hostname"))) if x)
        )
        if ip:
            hosts.append(Host(ip=ip, mac=cm, hostnames=hostnames or None, sources=[PLUGIN_ID]))
    return wifi, fdb, hosts


# --- devices -----------------------------------------------------------------


def adopted(raw: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for d in raw:
        m = mac(d.get("mac"))
        if not m or d.get("adopted") is False or d.get("state") == DISCONNECTED:
            continue
        out[m] = d
    return out


def device(
    m: str,
    d: dict[str, Any],
    known: dict[str, dict[str, Any]],
    health: dict[str, dict[str, Any]],
    networks: list[dict[str, Any]],
    reached: str | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> Device:
    names = port_names(d)
    ifaces = ports(d, names, networks, profiles)
    gateway = d.get("type") in GATEWAYS
    gws = wan_interfaces(d, ifaces, health) if gateway else []
    if d.get("type") in SWITCHING:
        ifaces += lags(d, names, ifaces)
    ifaces += vaps(d)
    neighbors = lldp(d, names)
    up = uplink(d, names, known, {n.remote_mac for n in neighbors if n.remote_mac})
    if up:
        neighbors.append(up)
    macs = list(
        dict.fromkeys([m, *(x for e in d.get("ethernet_table") or [] if (x := mac(e.get("mac"))))])
    )
    stats = d.get("system-stats") or {}
    ip = text(d.get("ip"))
    ips = list(dict.fromkeys([*([ip] if ip else []), *(lan_ips(networks) if gateway else [])]))
    # A gateway's "ip" is its WAN address: the console's own address is better.
    host = reached if reached in ips else ip
    uptime = count(d.get("uptime"))
    return Device(
        key=m,
        name=text(d.get("name")) or text(d.get("model")) or m,
        host=host,
        role=ROLES.get(str(d.get("type") or ""), "unknown"),
        vendor="Ubiquiti",
        model=text(d.get("model")),
        os_version=text(d.get("version")),
        serial=text(d.get("serial")),
        uptime_s=uptime,
        cpu_pct=pct(stats.get("cpu")),
        mem_pct=pct(stats.get("mem")),
        load_avg=load_avg(d),
        temperatures=temperatures(d) or None,
        storage=storage(d) or None,
        firmware=firmware(d),
        macs=macs,
        ips=ips or None,
        interfaces=ifaces or None,
        neighbors=neighbors or None,
        gateways=gws or None,
        vlans=site_vlans(d, networks),
    )


def site_vlans(d: dict[str, Any], networks: list[dict[str, Any]]) -> list[Vlan] | None:
    """A gateway's VLANs (with their subnets); a switch carries every network
    of the site, the untagged LAN as VLAN 1."""
    if d.get("type") in GATEWAYS:
        return vlans(networks) or None
    if d.get("type") == "usw":
        return [Vlan.model_validate(v) for v in vl.site_vlans(networks)] or None
    return None


# --- plugin entry points -----------------------------------------------------


def read_all(c: Client, missing: list[str]) -> tuple[list[dict], list[dict], list[dict], dict]:
    raw = c.get("stat/device")
    stations = optional("stat/sta", missing, lambda: c.get("stat/sta"), [])
    networks = optional("rest/networkconf", missing, lambda: c.get("rest/networkconf"), [])
    health_rows = optional("stat/health", missing, lambda: c.get("stat/health"), [])
    health = {str(h.get("subsystem")): h for h in health_rows}
    return raw, stations, networks, health


def site_wifi(c: Client, networks: list[dict], missing: list[str]) -> list[Any]:
    """The site's Wi-Fi networks, which every access point of it broadcasts."""
    if "wifi_networks" not in Device.model_fields:
        return []
    from omini_sdk.models import WifiNetwork

    wlans = optional("rest/wlanconf", missing, lambda: c.get("rest/wlanconf"), [])
    return [WifiNetwork.model_validate(w) for w in wlan.wifi_networks(wlans, networks)]


def port_profiles(
    c: Client, known: dict[str, dict[str, Any]], missing: list[str]
) -> dict[str, dict[str, Any]]:
    """The port profiles (rest/portconf), read only when a port of a switch or
    gateway uses one."""
    used = any(
        x.get("portconf_id")
        for d in known.values()
        if d.get("type") in SWITCHING
        for x in [*(d.get("port_overrides") or []), *(d.get("port_table") or [])]
        if isinstance(x, dict)
    )
    if not used:
        return {}
    return vl.profiles_by_id(optional("rest/portconf", missing, lambda: c.get("rest/portconf"), []))


def collect(cfg: Config) -> list[Device]:
    c = client_from(cfg)
    try:
        missing: list[str] = []
        raw, stations, networks, health = read_all(c, missing)
        known = adopted(raw)
        profiles = port_profiles(c, known, missing)
        out = [
            device(m, d, known, health, networks, urlparse(c.base).hostname, profiles)
            for m, d in known.items()
        ]
        wifi, fdb, hosts = clients(stations, known)
        ssids = site_wifi(c, networks, missing)
        for dev in out:
            dev.wireless_clients = wifi.get(dev.key) or None
            dev.fdb = fdb.get(dev.key) or None
            if dev.role == "ap" and ssids:
                dev.wifi_networks = ssids
        # Client names go with the gateway (else the first device).
        home = next((d for d in out if d.role == "router"), out[0] if out else None)
        if home and hosts:
            home.hosts = hosts
        if missing:
            log.warning("could not read: %s", ", ".join(missing))
        return out
    finally:
        c.close()


def test(cfg: Config) -> str:
    c = client_from(cfg)
    try:
        missing: list[str] = []
        raw, *_ = read_all(c, missing)
        n = len(adopted(raw))
        msg = f'Connected to {c.kind}, site "{c.site}": {n} device{"" if n == 1 else "s"}'
        if missing:
            msg += ". Could not read: " + ", ".join(OPTIONAL[m] for m in missing)
        return msg
    finally:
        c.close()


# --- changes -----------------------------------------------------------------

# How long apply waits for the switch to report the change (the controller
# provisions it, and the switch reports its ports every few seconds).
VERIFY_S = 40
POLL_S = 3


def find(raw: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    m = mac(key)
    return next((d for d in raw if m and mac(d.get("mac")) == m), None)


def target(raw: list[dict[str, Any]], key: str, port: str | None) -> tuple[dict[str, Any], int]:
    """The device and the port_idx of the change, or a PluginError saying why not."""
    d = find(raw, key)
    if d is None or d.get("adopted") is False:
        raise PluginError(f"the controller does not manage the device {key}")
    name = text(d.get("name")) or key
    if d.get("type") not in SWITCHING:
        raise PluginError(f"{name} is not a switch or gateway: its ports cannot be changed")
    if d.get("state") == DISCONNECTED:
        raise PluginError(f"{name} is disconnected from the controller: nothing was changed")
    if not isinstance(d.get("_id"), str):
        raise PluginError(f"the controller did not give the id of {name}")
    idx = next((i for i, n in port_names(d).items() if n == port), None)
    if idx is None:
        raise PluginError(f"{name} has no port {port}")
    wan = {text(w.get("ifname")) for _, w in wans(d)}
    if any(p.get("port_idx") == idx and text(p.get("ifname")) in wan for p in port_entries(d)):
        raise PluginError(f"{port} of {name} is a WAN port: change it in UniFi Network")
    return d, idx


def port_entries(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in d.get("port_table") or [] if isinstance(p, dict)]


def port_entry(d: dict[str, Any], idx: int) -> dict[str, Any]:
    return next((p for p in port_entries(d) if p.get("port_idx") == idx), {})


def disabled_profiles(c: Client) -> set[str]:
    """The ids of the port profiles that disable a port (the built-in
    "Disabled" one): a port given one of them is disabled too."""
    missing: list[str] = []
    profiles = optional("rest/portconf", missing, lambda: c.get("rest/portconf"), [])
    return {
        p["_id"] for p in profiles if p.get("forward") == DISABLED and isinstance(p.get("_id"), str)
    }


class Memory:
    """What a port's override had before Omini disabled it, so enabling puts
    it back (kept in the state folder; without one, enabling uses defaults)."""

    def __init__(self, cfg: Config):
        self.file = cfg.state_dir / "disabled-ports.json" if cfg.state_dir else None
        self.data: dict[str, Any] = {}
        try:
            if self.file and self.file.exists():
                self.data = json.loads(self.file.read_text())
        except (OSError, ValueError):
            log.debug("could not read the ports' previous settings")

    def save(self) -> None:
        if not self.file:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(self.data))
        except OSError:
            log.debug("could not keep the ports' previous settings")


def changed_overrides(
    before: list[dict[str, Any]],
    idx: int,
    enabled: bool,
    profiles: set[str],
    remembered: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The device's overrides with only port ``idx`` changed: its ``forward``
    (and, to enable a port given the "Disabled" profile, that profile). Every
    other override, and every other field of this one, stay as they are.
    Also returns what to remember to undo it."""
    out = [dict(o) for o in before]
    o = next((x for x in out if x.get("port_idx") == idx), None)
    if o is None:
        o = {"port_idx": idx}
        out.append(o)
    memo = None
    if not enabled:
        memo = {"had": "forward" in o, "forward": o.get("forward")}
        o["forward"] = DISABLED
    else:
        if remembered and remembered.get("forward") != DISABLED:
            if remembered.get("had"):
                o["forward"] = remembered.get("forward")
            else:
                o.pop("forward", None)
        elif o.get("forward") == DISABLED:
            o["forward"] = "all"
        if o.get("portconf_id") in profiles:
            o.pop("portconf_id")  # back to the default profile
    if set(o) == {"port_idx"}:
        out.remove(o)  # nothing left to override
    return out, memo


def verify(
    c: Client, key: str, idx: int, enabled: bool, profiles: set[str]
) -> tuple[bool, str | None]:
    """Reads the device until the controller shows the override and the switch
    reports the port so (up to VERIFY_S): (done, why not)."""
    deadline = time.monotonic() + VERIFY_S
    while True:
        try:
            d = find(c.get("stat/device"), key)
        except (PluginError, Forbidden, NotFound, ApiError) as e:
            why = f"the controller did not answer ({e or type(e).__name__})"
        else:
            if d is None:
                return False, "the controller no longer lists the device"
            stored = configured(overrides(d).get(idx, {}), profiles)
            if (stored is False) != (not enabled):
                return False, "the controller did not keep it"
            reported = port_entry(d, idx).get("enable")
            if d.get("state") != DISCONNECTED and reported in (enabled, None):
                return True, None
            why = f"the switch did not report it within {VERIFY_S} s"
        if time.monotonic() >= deadline:
            return False, why
        time.sleep(POLL_S)


def apply(cfg: Config, change: Change) -> list[Device]:
    """Enables or disables a switch port (port.enabled) through the device's
    port overrides: only that port's ``forward`` changes (and, to enable a
    port given the "Disabled" profile, that profile is removed). The
    controller saves it and provisions the switch; the device is read again
    until the switch reports the port so, and the change is undone on the
    controller if it does not. Returns the devices as read after the change."""
    if change.kind in ("wifi.set", "wifi.delete"):
        return apply_wifi(cfg, change)
    if change.kind in ("port.vlans", "vlan.set", "vlan.delete"):
        return apply_vlans(cfg, change)
    if change.kind != "port.enabled":
        raise PluginError(f"this plugin cannot apply {change.kind}")
    if not isinstance(change.value, bool):
        raise PluginError(f"port.enabled takes true or false, not {change.value!r}")
    c = client_from(cfg)
    try:
        d, idx = target(c.get("stat/device"), change.device, change.port)
        profiles = disabled_profiles(c)
        before = [o for o in d.get("port_overrides") or [] if isinstance(o, dict)]
        current = admin_enabled(port_entry(d, idx), overrides(d).get(idx, {}), profiles)
        if current is not change.value:
            memory = Memory(cfg)
            slot = f"{mac(d.get('mac'))}/{idx}"
            after, memo = changed_overrides(
                before, idx, change.value, profiles, memory.data.get(slot)
            )
            endpoint = f"rest/device/{d['_id']}"
            c.put(endpoint, {"port_overrides": after})
            done, why = verify(c, change.device, idx, change.value, profiles)
            if not done:
                undone = "undone on the controller"
                try:
                    c.put(endpoint, {"port_overrides": before})
                except PluginError as e:
                    undone = f"not undone ({e}): check the port in UniFi Network"
                verb = "enable" if change.value else "disable"
                raise PluginError(f"could not {verb} {change.port}: {why}; the change was {undone}")
            if memo:
                memory.data[slot] = memo
            else:
                memory.data.pop(slot, None)
            memory.save()
    finally:
        c.close()
    return collect(cfg)


def apply_wifi(cfg: Config, change: Change) -> list[Device]:
    """wifi.set: a Wi-Fi network of the site (its name, VLAN, password, on or
    off); wifi.delete removes it. The controller saves it and provisions every
    access point; the site is read again until it has it as asked."""
    value = change.value if isinstance(change.value, dict) else {}
    c = client_from(cfg)
    try:
        if change.kind == "wifi.set":
            wlan.set_wifi(c, value)
            wlan.check(c, value)
        else:
            wlan.delete_wifi(c, str(value.get("ssid") or ""))
            wlan.check(c, None, gone=str(value.get("ssid") or ""))
    finally:
        c.close()
    return collect(cfg)


def apply_vlans(cfg: Config, change: Change) -> list[Device]:
    """port.vlans: a switch port's untagged and tagged VLANs (its override's
    native and tagged networks); vlan.set creates a "VLAN only" network or
    renames one; vlan.delete deletes a "VLAN only" network. The controller
    saves it and provisions the switches; the site is read again until it
    shows the change (a port's change is undone if it does not)."""
    c = client_from(cfg)
    try:
        if change.kind == "port.vlans":
            d, idx = target(c.get("stat/device"), change.device, change.port)
            vl.set_port(c, d, idx, str(change.port), change.value)
        elif change.kind == "vlan.set":
            vl.set_vlan(c, change.value)
        else:
            vl.delete_vlan(c, change.value)
    finally:
        c.close()
    return collect(cfg)


def backup(cfg: Config) -> list[ConfigBackup]:
    """Each adopted device's settings as sorted JSON; the site's settings
    (networks, Wi-Fi networks, port profiles, site settings) go with the
    gateway, else the switch with the lowest MAC (see backup.py). Secrets are
    left out."""
    c = client_from(cfg)
    try:
        return bk.backups(c, adopted(c.get("stat/device")))
    finally:
        c.close()
