# MultiDrop: architecture

Goal: one tool for nearby sharing in both directions with **Quick Share**
(Android/Samsung), **LocalSend**, **AirDrop** and **AirPlay**, a common service
and CLI, and native-feeling apps on Linux and Windows. Research notes:
[RESEARCH.md](RESEARCH.md).

## Overview

```
                ┌──────────────── user space (no root) ───────────────────────┐
 Desktop app ──►│ multidropd  (asyncio; systemd --user or started by the app)  │
 (GTK, Linux)   │  ├─ core: config, accept policy, transfers, download folder, │
 Tray app ─────►│  │        history, desktop notifications (Linux)            │
 (Windows)      │  ├─ API: HTTP/JSON on a Unix socket (Linux) and the web UI   │
 Web UI, CLI ──►│  │       on 127.0.0.1 (token, Host and Origin checks)        │
                │  └─ backends (common interface multidrop.core.Backend)      │
                │      ├─ localsend   UDP multicast + HTTPS                   │
                │      ├─ quickshare  mDNS (zeroconf) + TCP/UKEY2, BLE wake-up │
                │      └─ airdrop     HTTPS on awdl0, BLE wake-up              │
                └───────────────────────────────┬─────────────────────────────┘
                                                │ pkexec / polkit, fixed argv
                ┌───────────────────────────────▼─────────────────────────────┐
                │ root: omdrop-owl's omdrop-discoverable start|stop|status|probe│
                │   OWL (AWDL in software) on a monitor interface → awdl0      │
                │   restores Wi-Fi itself after every session                  │
                └─────────────────────────────────────────────────────────────┘

 Linux desktop app, separate engines (own processes, user space):
   AirPlay screen mirroring → doubletake (-daemonize, JSON socket)
   AirPlay audio            → pipewire -c (libpipewire-module-raop-discover) + wpctl
```

### Backend interface (`src/multidrop/core.py`)

Every protocol implements `Backend`:

- `start(on_offer, on_event)`: become visible and accept transfers.
  `on_offer(IncomingOffer) -> set[file_id]` decides (dialog, auto-accept, PIN …).
- `discover(timeout) -> list[Device]`
- `send(device, paths, on_event)`
- `stop()`

Protocol-independent rules live in the core: `IncomingFileWriter` writes to
`*.part`, checks announced size and SHA-256, prevents path traversal
(`safe_relative_path`), never overwrites and renames only at the end – the same
safety rules for AirDrop, Quick Share and LocalSend.

### Service and front ends

- `multidropd` (`src/multidrop/daemon/`): `service.py` (offers, jobs, events,
  devices kept for 90 s across scans), `api.py` (HTTP API), `notify.py` (D-Bus
  notifications with Accept/Decline), `web/` (browser UI).
- The CLI talks to a running `multidropd` (`cli_daemon.py`) or runs the
  backends itself (`--direct`).
- **Linux desktop app** (`desktop.py`): GTK 4/libadwaita under the system Python
  (stdlib + gi only), a StatusNotifierItem with dbusmenu in the top bar, device
  bubbles with drag and drop, AirDrop sessions, AirPlay mirroring and audio.
  Fallbacks for GTK ≥ 4.6 / libadwaita ≥ 1.1.
- **Windows app** (`windows.py`): the service on a thread, a pystray tray icon,
  the web UI in the browser, Windows notifications for incoming offers. Built
  into `MultiDrop.exe` with PyInstaller by GitHub Actions.
- Configuration: `~/.config/multidrop/config.toml`; downloads to
  `~/Downloads/multidrop`; keys and identities under
  `~/.local/share/multidrop/<protocol>/` (mode 0600).

### Quick Share discovery details

- The service announces `_FC9F5ED42C8A._tcp` only on a real network address,
  waits while Wi-Fi reconnects and re-announces when the address changes.
- A long-lived browser on the announcement socket hears phones between scans
  (some phones answer only when a router's mDNS gateway asks).
- Queries are additionally answered by unicast, and one-shot queries from
  mDNS gateways get RFC 6762 §6.7 legacy-unicast answers (same id, question
  repeated, TTL ≤ 10 s); the service re-announces when a phone starts searching.
- Samsung phones ignore the BLE wake-up, so sending to them uses the QR-code
  flow (the phone scans, then advertises a token from the code).

## Language: Python 3.11+ (asyncio)

- Templates and specs are Python or language-neutral: opendrop (GPL-3),
  omdrop-plugin (MIT), NearDrop's Quick Share spec (Unlicense).
- Everything installs without root as wheels: `aiohttp`, `cryptography`,
  `protobuf`, `zeroconf`, `dbus-fast` (Linux only).
- Python is fast enough here: AirDrop over OWL ≈ 0.5–1.3 MB/s, LAN transfers
  tens of MB/s.
- OWL stays C and runs as a separate program from the omdrop-owl package.

License: **GPL-3.0-or-later** – code from opendrop (GPL-3) and omdrop-plugin
(MIT) can be used; GPL-2.0-only tools are only ever run as separate programs.

## Upstream

| Upstream | Use |
|---|---|
| LocalSend protocol | own implementation from the specification |
| NearDrop `PROTOCOL.md` + `.proto` | `.proto` files (Unlicense), generated `_pb2.py`, own implementation |
| rquickshare | reference (BLE wake-up, re-announce, edge cases) |
| omdrop-plugin | basis of the AirDrop backend (MIT, attributed) |
| opendrop | reference for sending via AirDrop |
| omdrop-owl | radio backend contract `omdrop-discoverable start/stop/status/peers/probe`, used as installed |
| doubletake | AirPlay mirroring engine, driven over its daemon socket |

## Privileged part (AWDL), kept separate

- The **only** root component is omdrop-owl's helper at a fixed path, reached
  through its polkit action; it takes fixed commands only.
- The service speaks AirDrop itself (HTTPS on `awdl0`) as a normal user.
- AWDL shares the station's channel (6, 44 or 149). With `--switch-channel`,
  `wifi_channel.py` moves to the same network's access point on such a channel
  for the session (temporary NetworkManager profile with band/channel/BSSID
  pinned) after starting a `systemd-run --user` safety timer that restores the
  original profile even if MultiDrop dies.

## Code layout

```
src/multidrop/
  core.py                 Device, IncomingOffer, Backend ABC, safe file writer
  config.py               TOML configuration
  cli.py, cli_daemon.py   multidrop devices|send|receive|share|info|ui|airdrop
  daemon/                 multidropd: service.py, api.py, client.py, notify.py, web/
  desktop.py              Linux desktop app (GTK 4, tray, AirDrop, AirPlay)
  windows.py              Windows tray app
  airdrop_radio.py        timed AirDrop sessions with omdrop-owl
  wifi_channel.py         temporary Wi-Fi channel move for AirDrop
  airplay_scan.py         AirPlay receiver capabilities from mDNS (features bits)
  backends/localsend/     identity, protocol, discovery, server, client, webshare
  backends/quickshare/    proto/pb, crypto (UKEY2, SecureMessage), frames,
                          connection, inbound, outbound, mdns, ble, qr
  backends/airdrop/       server, client, archive, http, mdns, ble
tools/
  install-desktop.sh      Linux install (venv, launcher, autostart, file managers)
  fetch-doubletake.sh     pinned doubletake download (SHA-256) or source build
  build-omdrop-owl.sh     omdrop-owl .deb without root
  dev/                    gen_proto.sh, rquickshare/LocalSend interop, UI smoke test
data/                     icons (SVG, PNG, ICO), systemd user unit
packaging/windows/        PyInstaller entry point
tests/                    unit, loopback, interop and CLI tests
```


### Android

`android/` is a native Java application with an Android Studio/Gradle entry point
and an SDK-only build script. `TransferEngine` and `LocalHttp` are JVM-testable
LocalSend/browser components; `TransferService` supplies Android KeyStore TLS,
MediaStore pending writes, explicit receive consent, and foreground notifications.
`AudioService` captures approved app playback and feeds the classic RAOP sender.
The app delegates Quick Share to Android's system sharing provider. It does not
embed Python, PipeWire, OWL, or doubletake. See `android/README.md` for the precise
support matrix and platform restrictions.
