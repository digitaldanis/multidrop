<p align="center"><img src="docs/banner.png" alt="MultiDrop" width="100%"></p>

<p align="center">
  <b>AirDrop, Quick Share, LocalSend and AirPlay for Linux and Windows.</b><br>
  Share files, your screen and audio with phones, Macs and TVs nearby.
</p>

<p align="center">
  <a href="../../releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/digitaldanis/multidrop"></a>
  <a href="../../actions/workflows/build.yml"><img alt="Build" src="https://github.com/digitaldanis/multidrop/actions/workflows/build.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-blue"></a>
</p>

## Features

| | Linux | Windows | Works with |
|---|:---:|:---:|---|
| **Quick Share** – send & receive, QR code | ✅ | ✅ | Android, Samsung (no app) |
| **LocalSend** – send & receive | ✅ | ✅ | iPhone, Android, PC with LocalSend |
| **Browser download** – QR code + PIN | ✅ | ✅ | any phone, no app |
| **AirDrop** – send & receive | ✅* | – | iPhone, iPad, Mac |
| **AirPlay** – mirror your screen | ✅ | – | Apple TV, Mac, AirPlay TVs |
| **AirPlay** – play audio | ✅ | – | Apple TV, HomePod, Sonos, Mac |

\* needs a supported Wi-Fi card, see [AirDrop setup](#airdrop-setup-linux).

## Android preview

A native Android 10+ app lives in [`android/`](android/README.md): LocalSend file
transfers with receive approval, browser downloads with QR codes, Android's system
share sheet (including Quick Share where installed), classic AirPlay audio, and a
local transfer history. Download the preview APK from the **Android** Actions artifacts,
or build it with Android Studio / `android/build.sh`.

This is a platform-specific preview, not full desktop protocol parity. Direct AirDrop,
AirPlay screen mirroring, and AirPlay 2 pairing are not implemented. Quick Share uses
the Android provider rather than the desktop backend. See the [support matrix and
build/test instructions](android/README.md).

## Install

### Windows

Download **MultiDrop.exe** from [Releases](../../releases/latest) and run it.
MultiDrop sits in the notification area and opens in your browser. Allow
*Private networks* when Windows Firewall asks.

### Ubuntu / Debian (apt)

Ubuntu 24.04+ and Debian 13+. Add the repository once, then install and update
with apt:

```bash
curl -fsSL https://digitaldanis.github.io/multidrop/multidrop-archive-keyring.gpg \
  | sudo tee /usr/share/keyrings/multidrop-archive-keyring.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/multidrop-archive-keyring.gpg] https://digitaldanis.github.io/multidrop stable main" \
  | sudo tee /etc/apt/sources.list.d/multidrop.list
sudo apt update && sudo apt install multidrop
```

Or download `multidrop_*_all.deb` from [Releases](../../releases/latest) and run
`sudo apt install ./multidrop_*_all.deb`. For AirPlay mirroring run
`multidrop-fetch-doubletake` once.

### Other Linux distributions

```bash
git clone https://github.com/digitaldanis/multidrop.git ~/multidrop
~/multidrop/tools/install-desktop.sh --pin
```

Fedora, Arch, openSUSE and others (Python ≥ 3.11, GTK 4, libadwaita). The
installer needs no root and tells you if a system package is missing. Uninstall
with `tools/install-desktop.sh --remove`.

## Using it

- **Top bar menu** – nearby devices, AirDrop on/off, *Mirror screen*, *Play audio on*.
- **Window** – drop files on a device bubble, or click it. Samsung phones that
  don't show up: use the **QR code** tile.
- **File manager** – right-click → *Send with MultiDrop*, or drop files on the dock icon.
- **Receiving** – just send from your phone; MultiDrop asks before accepting.
  Files land in `~/Downloads/multidrop`.

On GNOME the top bar icon needs the *AppIndicator and KStatusNotifierItem
Support* extension (preinstalled on Ubuntu).

## AirDrop setup (Linux)

AirDrop uses [omdrop-owl](https://github.com/t4t5/omdrop-owl), which recreates
Apple's AWDL radio link in software (tested with MediaTek MT7925; Intel cards in
exclusive mode). Install it once, then turn AirDrop on in the app:

```bash
tools/build-omdrop-owl.sh
sudo apt install ./build/airdrop/omdrop-owl_0.1.0-1_amd64.deb
```

If your Wi-Fi isn't on channel 6, 44 or 149, MultiDrop temporarily switches to
the same network on one of those channels and switches back afterwards – no
router changes needed. Details: [docs/INSTALL-ROOT.md](docs/INSTALL-ROOT.md).

## Command line

```bash
multidrop devices                          # find devices
multidrop send photo.jpg --to "Galaxy S24" # send (protocol chosen automatically)
multidrop send photo.jpg --qr              # send via Quick Share QR code
multidrop receive                          # receive, confirm each transfer
multidrop share photos/                    # browser download with QR code + PIN
```

`multidropd` is the background service behind the apps (systemd unit in
`data/systemd/`). Config: `~/.config/multidrop/config.toml`.

## Status

Tested: AirDrop Linux → Mac, Quick Share and LocalSend against other
implementations and the official LocalSend app, the Linux desktop app, the
Windows build in CI. Not fully verified yet on real devices: Quick Share with
Android/Samsung phones, AirPlay mirroring and audio, Windows on real hardware.
Bug reports welcome.

## Project layout

```
android/          native Android app, build scripts, protocol and emulator tests
src/multidrop/     service, protocols, Linux desktop app, Windows tray app
tests/             unit, loopback and interop tests
tools/             install-desktop.sh, fetch-doubletake.sh, build-omdrop-owl.sh, dev/
data/              icons, systemd unit
packaging/         .deb and APT repository, Windows build, release notes
docs/              architecture, protocol research, root setup
```

Development: `python3 -m venv .venv && .venv/bin/pip install -e ".[test]" && .venv/bin/pytest -q`.
Design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/RESEARCH.md](docs/RESEARCH.md).

## Credits & license

Built on the work of [NearDrop](https://github.com/grishka/NearDrop),
[rquickshare](https://github.com/Martichou/rquickshare),
[LocalSend](https://github.com/localsend/localsend),
[omdrop-owl](https://github.com/t4t5/omdrop-owl), [OWL](https://github.com/seemoo-lab/owl),
[OpenDrop](https://github.com/seemoo-lab/opendrop),
[omdrop-plugin](https://github.com/brentkearney/omdrop-plugin),
[doubletake](https://github.com/omarroth/doubletake) and
[UxPlay](https://github.com/FDH2/UxPlay) – see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

MultiDrop is licensed under [GPL-3.0-or-later](LICENSE). Not affiliated with
Apple, Google or Samsung; AirDrop, AirPlay and Quick Share are their trademarks.
