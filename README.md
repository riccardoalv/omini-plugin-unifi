# omini-plugin-unifi

[Omini](https://github.com/riccardoalv/omini) plugin for **UniFi Network**: UniFi OS consoles (Dream Machine, Dream Router, Cloud Gateway, Cloud Key Gen2+...) and the self-hosted UniFi Network Application. Read-only: it never changes the controller's or the devices' configuration.

> **Experimental.** Built from the API's documentation and the answers its web interface gets; not yet tested on real hardware. Reports of what works (and what does not) are welcome.

## What it reads

One Omini device per **adopted** UniFi device (gateways, switches, access points), keyed by its MAC. Disconnected devices are left out (their last data would be stale).

| Data | From | Used for |
|---|---|---|
| Name, model, serial, firmware, uptime, CPU, memory, load | `stat/device` | Device details and health |
| Temperatures, storage (consoles with a disk) | `stat/device` | Health |
| Pending firmware (`upgradable`, `upgrade_to_firmware`) | `stat/device` | "Update available" — what the controller knows from its own checks; Omini never starts one |
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

## Requirements

- UniFi Network **7.x to 9.x** — on a UniFi OS console, or the self-hosted UniFi Network Application.
- A **local** account (see below). UI.com / SSO accounts and accounts with two-factor authentication cannot sign in through this API.
- An Omini version whose plugin SDK has VLANs and SFP readings (`Vlan`, `Transceiver`).

## Create the account (View Only)

Use a dedicated local account with the **View Only** role: it can read everything Omini needs and change nothing. See Ubiquiti's [UniFi roles explained](https://help.ui.com/hc/en-us/articles/1500011491541-UniFi-Roles-Explained-Admins-and-Users).

**UniFi OS console** (UniFi OS 3.x and later):

1. Open the console (e.g. `https://192.168.1.1`) → **Admins & Users** → **Create New** (or **Add Admin**).
2. Choose **Restrict to local access only** (no UI.com account), username `omini` and a strong password.
3. Under the role, pick **View Only** for the **Network** application (no access to the other applications is needed).
4. Save.

**Self-hosted UniFi Network Application:**

1. **Settings → Admins & Users → Add Admin** (older versions: *Settings → Admins → Add Admin*).
2. Turn off *Remote Access* / pick a **local** admin, username `omini`, set a password.
3. Role: **View Only**. Save.

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
2. **Login** (the only POST): `POST /api/auth/login` on UniFi OS (keeps the `TOKEN` cookie and the `x-csrf-token` header), `POST /api/login` on the self-hosted application (`unifises` and `csrf_token` cookies), with `{"username", "password", "remember": true}`.
3. **Session:** kept in the plugin's state folder (`session.json`, readable only by Omini; a hash of the credentials, never the password) and reused until the controller answers 401, so there is no login on every collection. After the controller refuses the credentials, the plugin waits 10 minutes before trying again (UniFi OS locks accounts after repeated failures); changing them in Omini retries at once.
4. **Reads** (GET), under `/proxy/network/api/s/<site>/` on UniFi OS or `/api/s/<site>/` on the self-hosted application:
   - `stat/device` — adopted devices (required)
   - `stat/sta` — connected clients
   - `rest/networkconf` — networks (read only, never written)
   - `stat/health` — health of each subsystem (WAN status, latency)

Mapping notes:

- Ports are named as in UniFi (`Port 1`, or the name given to the port); a client's `sw_port` (a port index) is translated into that name.
- Connector: SFP when the port has a module (`sfp_found`) or its media is `SFP`/`SFP+`/`SFP28`, QSFP for `QSFP*`, RJ45 for copper media (`FE`, `GE`, `2.5GE`, `5GE`, `10GE`); otherwise unknown.
- Wi-Fi bands: `ng` = 2.4 GHz, `na` = 5 GHz, `6e` = 6 GHz. Link rates come in kbit/s and are reported in Mbit/s.
- A client's current traffic (`tx_bytes-r` / `rx_bytes-r`, bytes/s) is measured by the AP: what the AP sends is what the client downloads.
- A gateway's `ip` in `stat/device` is its WAN address; its LAN addresses come from the networks, and the address Omini reaches it at is used as its host.

## Known limitations

- **Not tested on hardware.** Field names come from the controller's answers as documented by the community; a firmware may name or scale something differently. In particular, unverified: the direction of client traffic rates (`tx_bytes-r` as the client's download), SFP readings units (`sfp_txpower`/`sfp_rxpower` taken as dBm, `sfp_current` as mA), the `storage` list of consoles, the LLDP fields beyond `chassis_id`/`port_id`/`local_port_idx`, and the uplink of mesh APs.
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
