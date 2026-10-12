# omini-plugin-unifi

[Omini](https://github.com/riccardoalv/omini) plugin for **UniFi Network**: UniFi OS consoles (Dream Machine, Dream Router, Cloud Gateway, Cloud Key Gen2+...) and the self-hosted UniFi Network Application. Collecting only reads; the one change it makes is enabling or disabling a switch port, when you ask for it in Omini (see [What it changes](#what-it-changes)).

> **Experimental.** Built from the API's documentation and the answers its web interface gets; not yet tested on real hardware. Reports of what works (and what does not) are welcome.

## What it reads

One Omini device per **adopted** UniFi device (gateways, switches, access points), keyed by its MAC. Disconnected devices are left out (their last data would be stale).

| Data | From | Used for |
|---|---|---|
| Name, model, serial, firmware, uptime, CPU, memory, load | `stat/device` | Device details and health |
| Temperatures, storage (consoles with a disk) | `stat/device` | Health |
| Pending firmware (`upgradable`, `upgrade_to_firmware`) | `stat/device` | "Update available" — what the controller knows from its own checks; Omini never starts one |
| Ports: whether each is enabled (`port_overrides` → `forward`, else the port table's `enable`) | `stat/device` | "Disabled" ports and the Enable / Disable button |
| Ports: name, up/down, link speed, duplex, media, RJ45/SFP, traffic and error counters, SFP module and its readings | `stat/device` → `port_table` | Ports of each device on the map, front view, link speed, traffic |
| LLDP neighbors | `stat/device` → `lldp_table` | Links between devices |
| Uplink (the upstream UniFi device and its port, or the access point of a mesh link) | `stat/device` → `uplink` | Each device hangs under the one it is connected to |
| Wi-Fi networks broadcast by each AP, with counters | `stat/device` → `vap_table` | Wireless interfaces of the AP |
| WAN ports, their addresses and gateways | `stat/device` → `wan1`, `wan2` | WAN nodes |
| WAN status and latency | `stat/health` | WAN status dot and latency |
| Wi-Fi clients: AP, network name, band, signal, link rates, current traffic | `stat/sta` | Clients under their AP ("Home · 5 GHz") |
| Wired clients: switch and port | `stat/sta` (`sw_mac`, `sw_port`) | Clients under their switch port |
| Client names and IPs | `stat/sta` | Device names |
| Networks with a VLAN: id, name, subnet | `rest/networkconf` | VLANs of the gateway |

Clients, networks and WAN status are optional: if the account cannot read them (or the controller version lacks them), the rest is still collected, and **Test connection** says what is missing.

## What it changes

**Enable or disable a switch port** (`port.enabled`), from the port's card in Omini: ports of the switches and gateways (their LAN ports) the controller manages. Access points' ports and a gateway's WAN ports are refused with a message, and so is a device disconnected from the controller.

The port is changed through the device's **port overrides**, the same settings as the port in UniFi Network's Port Manager:

1. `GET stat/device` and `GET rest/portconf` (the port profiles, to recognize the built-in "Disabled" one).
2. `PUT rest/device/<_id>` (the device's `_id` from `stat/device`) with the body `{"port_overrides": [...]}`: the device's overrides as they are, with only that port's `forward` changed. Disabling sets `"forward": "disabled"` (creating the port's override, `{"port_idx": n, "forward": "disabled"}`, when it has none). Enabling puts back the `forward` the port had before Omini disabled it (or removes the field when it had none; an override left with nothing else is removed), else sets `"forward": "all"`; a port given the "Disabled" port profile (`portconf_id`, how older Network versions disabled ports) gets that profile removed, back to the default. Every other override, and every other field of the port's override (name, PoE mode, VLANs, profile...), is sent as it was.
3. The controller saves the change itself and provisions the switch. The plugin reads `stat/device` again (every 3 seconds, up to 40) until the override shows the change and the switch reports the port (`port_table` → `enable`) so. If the controller did not keep it or the switch does not report it in time, the plugin sends the overrides as they were before (the change is undone) and reports the error.

What the port's override had before it was disabled is kept in the plugin's state folder (`disabled-ports.json`), so enabling restores it. UniFi has no separate running and saved configuration: what the controller keeps is saved and survives reboots.

Paths are under `/proxy/network/api/s/<site>/` on UniFi OS and `/api/s/<site>/` on the self-hosted application. Changes need an account that may change the site (see below); with a **View Only** account, collecting works and a change fails with a message naming the role it needs.

### Wi-Fi per VLAN

`wifi.set` and `wifi.delete` manage the site's Wi-Fi networks (`rest/wlanconf`) from the VLANs tab of Omini: a network's name, the VLAN its clients go to, its password (WPA2 personal for a new one) and whether it is on. The VLAN is a UniFi network (`rest/networkconf`): a VLAN with none yet gets a "VLAN only" network, as the controller's own form does. A new WLAN takes the site's default user group and the access point groups of an existing WLAN. The controller saves the change and provisions every access point; the plugin reads the site again until it has it. The password goes to the controller only (never logged, never kept by Omini). Every access point reports the site's Wi-Fi networks.

## Requirements

- UniFi Network **7.x to 9.x** — on a UniFi OS console, or the self-hosted UniFi Network Application.
- A **local** account (see below). UI.com / SSO accounts and accounts with two-factor authentication cannot sign in through this API.
- An Omini version whose plugin SDK has VLANs and SFP readings (`Vlan`, `Transceiver`).

## Create the account

Use a dedicated **local** admin for Omini whose role in the **Network** application can change the site's devices: **Full Management** on a UniFi OS console, **Site Admin** on the self-hosted application (called *Administrator* on older versions). See Ubiquiti's [UniFi roles explained](https://help.ui.com/hc/en-us/articles/1500011491541-UniFi-Roles-Explained-Admins-and-Users).

A **View Only** account also works if you only want the map: everything is read, but enabling or disabling a port fails with a message saying which role it needs.

**UniFi OS console** (UniFi OS 3.x and later):

1. Open the console (e.g. `https://192.168.1.1`) → **Admins & Users** → **Create New** (or **Add Admin**).
2. Choose **Restrict to local access only** (no UI.com account), username `omini` and a strong password.
3. Under the role, pick **Full Management** for the **Network** application (or **View Only**, for the map alone). No access to the other applications is needed.
4. Save.

**Self-hosted UniFi Network Application:**

1. **Settings → Admins & Users → Add Admin** (older versions: *Settings → Admins → Add Admin*).
2. Turn off *Remote Access* / pick a **local** admin, username `omini`, set a password.
3. Role: **Site Admin** (or **View Only**, for the map alone). Save.

Then, in Omini, fill in:

| Field | Value |
|---|---|
| Address | The console (`https://192.168.1.1`) or the application (`https://controller:8443`) |
| Username / Password | The account above |
| Site | The site's internal name: the part after `/manage/` in the Network application's address (`default` for the first site; other sites have short ids like `x8k2m1ab`) |
| Verify the TLS certificate | Off by default: UniFi uses a self-signed certificate |

## How it works

UniFi's documented API is the official [UniFi Network API](https://developer.ui.com/) (an API key, `/proxy/network/integration/v1/...`). It does not yet expose ports with counters, LLDP, uplinks or client rates, so this plugin uses the **classic API**: the same endpoints the Network application's own web interface reads, stable across versions 7 to 9 and documented by the community ([ubntwiki](https://ubntwiki.com/products/software/unifi-controller/api), [Art-of-WiFi UniFi API client](https://github.com/Art-of-WiFi/UniFi-API-client)).

1. **Which flavor:** `GET /` answers 200 on a UniFi OS console and redirects (to `/manage`) on the self-hosted application. The other flavor is tried if the first login path does not exist.
2. **Login** (the only POST; the one PUT is the change above): `POST /api/auth/login` on UniFi OS (keeps the `TOKEN` cookie and the `x-csrf-token` header), `POST /api/login` on the self-hosted application (`unifises` and `csrf_token` cookies), with `{"username", "password", "remember": true}`.
3. **Session:** kept in the plugin's state folder (`session.json`, readable only by Omini; a hash of the credentials, never the password) and reused until the controller answers 401, so there is no login on every collection. After the controller refuses the credentials, the plugin waits 10 minutes before trying again (UniFi OS locks accounts after repeated failures); changing them in Omini retries at once.
4. **Reads** (GET), under `/proxy/network/api/s/<site>/` on UniFi OS or `/api/s/<site>/` on the self-hosted application:
   - `stat/device` — adopted devices (required)
   - `stat/sta` — connected clients
   - `rest/networkconf` — networks (read only, never written)
   - `stat/health` — health of each subsystem (WAN status, latency)
   - `rest/portconf` — port profiles, only when applying a change

Mapping notes:

- Ports are named as in UniFi (`Port 1`, or the name given to the port); a client's `sw_port` (a port index) is translated into that name.
- Connector: SFP when the port has a module (`sfp_found`) or its media is `SFP`/`SFP+`/`SFP28`, QSFP for `QSFP*`, RJ45 for copper media (`FE`, `GE`, `2.5GE`, `5GE`, `10GE`); otherwise unknown.
- Wi-Fi bands: `ng` = 2.4 GHz, `na` = 5 GHz, `6e` = 6 GHz. Link rates come in kbit/s and are reported in Mbit/s.
- A client's current traffic (`tx_bytes-r` / `rx_bytes-r`, bytes/s) is measured by the AP: what the AP sends is what the client downloads.
- A gateway's `ip` in `stat/device` is its WAN address; its LAN addresses come from the networks, and the address Omini reaches it at is used as its host.

## Known limitations

- **Not tested on hardware.** Field names come from the controller's answers as documented by the community; a firmware may name or scale something differently. In particular, unverified: the direction of client traffic rates (`tx_bytes-r` as the client's download), SFP readings units (`sfp_txpower`/`sfp_rxpower` taken as dBm, `sfp_current` as mA), the `storage` list of consoles, the LLDP fields beyond `chassis_id`/`port_id`/`local_port_idx`, and the uplink of mesh APs. For changes: the `forward` field of port overrides, the built-in "Disabled" profile of older versions, the port table's `enable` and how long a switch takes to report a change were checked only against Omini's emulated controller, and the role names (Full Management, Site Admin) follow Ubiquiti's documentation.
- PoE (power per port) is read by UniFi but Omini's data model has no place for it yet.
- Port VLAN membership (port profiles) is not read.
- Only one site per integration: add the integration again for another site.
- Clients without an IP are not given a name; Wi-Fi clients of an AP that is not adopted on this site are left out.

## Development

The plugin uses [uv](https://docs.astral.sh/uv/) and expects the Omini repository next to it (the SDK is in `../omini/sdk/python`):

```bash
uv run pytest          # tests, against fixtures shaped like the controller's answers
uv run ruff check .    # lint
uv run ruff format .   # format
```

Run it from a local Omini without installing it: `OMINI_PLUGIN_DIRS=../omini-plugin-unifi make run` in the Omini repository.

## License

MIT
