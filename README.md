<p align="center"><img src="docs/banner.png" alt="MultiDrop" width="100%"></p>

<h1 align="center">MultiDrop – the AirDrop alternative for Linux and Windows</h1>

<h3 align="center">AirDrop, Quick Share, LocalSend and AirPlay in one free, open-source app.</h3>

<p align="center">
  Send files to any phone nearby, and get files from it. No cloud, no cable, no account.
</p>

<p align="center">
  <a href="../../releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/digitaldanis/multidrop"></a>
  <a href="../../actions/workflows/build.yml"><img alt="Build" src="https://github.com/digitaldanis/multidrop/actions/workflows/build.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-blue"></a>
  <img alt="Linux | Windows" src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows-informational">
</p>

<p align="center">
  <a href="#download"><b>Download</b></a> ·
  <a href="#how-to-use-it">How to use it</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="#command-line">Command line</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

iPhones speak AirDrop, Android and Samsung phones speak Quick Share, and the
rest use LocalSend. **MultiDrop speaks all of them**, so your PC finally shows
up in the share menu of every phone in the room. It is a free, open-source
**AirDrop alternative for Linux and Windows** that also replaces Google's
Quick Share for PC and the LocalSend desktop app, with no cloud and no account.

- 📲 **Every phone** – iPhone, Android, Samsung, and any phone with a browser
- 🔒 **Stays local** – straight from device to device, nothing goes through a server
- ✋ **Asks first** – nothing lands on your PC until you accept it
- 🔊 **More than files** – mirror your screen or play your PC's sound on AirPlay speakers

## Why MultiDrop? AirDrop alternatives compared

| | MultiDrop | Apple AirDrop | Google Quick Share (Windows) | LocalSend | Snapdrop / PairDrop |
|---|:---:|:---:|:---:|:---:|:---:|
| Works on **Linux** | ✅ | – | – | ✅ | browser only |
| Works on **Windows** | ✅ | – | ✅ | ✅ | browser only |
| Talks to **iPhone** natively (AirDrop) | ✅ Linux | ✅ | – | – | – |
| Talks to **Android / Samsung** natively (Quick Share) | ✅ | – | ✅ | – | – |
| Shows up in the phone's **share menu** | ✅ | ✅ | ✅ | needs the app | – |
| **AirPlay** screen mirroring and audio | ✅ Linux | ✅ | – | – | – |
| No account, no cloud | ✅ | ✅ | Google account optional | ✅ | server needed |
| Open source | ✅ GPL-3.0 | – | – | ✅ | ✅ |

## What works where

| | Linux | Windows | Works with |
|---|:---:|:---:|---|
| **Quick Share** – send & receive, QR code | ✅ | ✅ | Android, Samsung (no extra app) |
| **LocalSend** – send & receive | ✅ | ✅ | iPhone, Android, PCs with LocalSend |
| **Browser download** – QR code + PIN | ✅ | ✅ | any phone, no app at all |
| **AirDrop** – send & receive | ✅¹ | – | iPhone, iPad, Mac |
| **AirPlay** – mirror your screen | ✅ | – | Apple TV, Mac, AirPlay TVs |
| **AirPlay** – play audio | ✅ | ✅² | Apple TV, HomePod, Sonos, Mac |

<sub>¹ needs a supported Wi-Fi card, see [AirDrop on Linux](#airdrop-on-linux).
² receivers that accept unencrypted or RSA AirPlay 1 streams without a password;
others are listed as not supported.</sub>

## Download

### Windows 10 / 11

1. Download **[MultiDrop-Setup.exe](../../releases/latest)**.
2. Run it. It installs just for you – no admin rights needed.
3. Click **Allow** when Windows Firewall asks, so phones can reach your PC.

Don't want to install anything? Take the portable **MultiDrop.exe** from the
same page. Uninstall any time in *Settings → Apps*.

### Ubuntu / Debian

Ubuntu 24.04+ and Debian 13+. Add the repository once, then MultiDrop updates
with the rest of your system:

```bash
curl -fsSL https://digitaldanis.github.io/multidrop/multidrop-archive-keyring.gpg \
  | sudo tee /usr/share/keyrings/multidrop-archive-keyring.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/multidrop-archive-keyring.gpg] https://digitaldanis.github.io/multidrop stable main" \
  | sudo tee /etc/apt/sources.list.d/multidrop.list
sudo apt update && sudo apt install multidrop
```

Or grab `multidrop_*_all.deb` from [Releases](../../releases/latest) and run
`sudo apt install ./multidrop_*_all.deb`. For AirPlay mirroring, run
`multidrop-fetch-doubletake` once.

<details>
<summary><b>Fedora, Arch, openSUSE and other distributions</b></summary>

```bash
git clone https://github.com/digitaldanis/multidrop.git ~/multidrop
~/multidrop/tools/install-desktop.sh --pin
```

Needs Python ≥ 3.11, GTK 4 and libadwaita. The installer needs no root and
tells you if a system package is missing. Uninstall with
`tools/install-desktop.sh --remove`.

</details>

## How to use it

**Receive:** just send from your phone. MultiDrop shows up like any other
nearby device and asks you before it accepts anything. Files land in
`Downloads/multidrop` in your home folder.

**Send:** drop files on a device in the MultiDrop window, or click the device
and pick them.

- **Windows** – MultiDrop opens in its own window and explains on first start
  how phones send to it. Closing the window keeps it receiving in the
  notification area. Turn on *Send to* in *Settings*, then right-click files in
  Explorer → *Send to* → *MultiDrop*. *Play this PC's sound on a speaker* sends
  your audio to an AirPlay speaker.
- **Linux** – the top bar menu lists nearby devices and has AirDrop on/off,
  *Mirror screen* and *Play audio on*. In the file manager, right-click →
  *Send with MultiDrop*. On GNOME the top bar icon needs the *AppIndicator and
  KStatusNotifierItem Support* extension (preinstalled on Ubuntu).
- **Phone doesn't show up?** Scan the **QR code** tile with its camera
  (Android/Samsung), or use the **browser download** tile: scan, enter the PIN,
  download. That works on every phone.

## AirDrop on Linux

AirDrop needs Apple's AWDL radio link, which
[omdrop-owl](https://github.com/t4t5/omdrop-owl) recreates in software (tested
with MediaTek MT7925; Intel cards in exclusive mode). Install it once, then
turn AirDrop on in the app:

```bash
tools/build-omdrop-owl.sh
sudo apt install ./build/airdrop/omdrop-owl_0.1.0-1_amd64.deb
```

If your Wi-Fi isn't on channel 6, 44 or 149, MultiDrop briefly moves to the
same network on one of those channels and moves back afterwards – no router
changes needed. Details: [docs/INSTALL-ROOT.md](docs/INSTALL-ROOT.md).

## FAQ

<details>
<summary><b>Is there an AirDrop for Linux?</b></summary>

Yes. MultiDrop sends and receives files with iPhones, iPads and Macs over the
real AirDrop protocol on Linux. It needs a supported Wi-Fi card, see
[AirDrop on Linux](#airdrop-on-linux).
</details>

<details>
<summary><b>Is there an AirDrop for Windows?</b></summary>

Not the real one: AirDrop needs Apple's radio link, which Windows can't do.
MultiDrop for Windows uses Quick Share, LocalSend and a browser download
(QR code + PIN) instead, which together cover every iPhone and Android phone.
</details>

<details>
<summary><b>Is MultiDrop a Quick Share or Nearby Share alternative for PC?</b></summary>

Yes. It speaks Google's Quick Share (formerly Nearby Share) protocol on Linux
and Windows, so Android and Samsung phones see your PC without any extra app.
</details>

<details>
<summary><b>Can I use it instead of LocalSend?</b></summary>

MultiDrop is compatible with the LocalSend protocol, so it talks to the official
LocalSend apps. On top of that it adds AirDrop and Quick Share.
</details>

<details>
<summary><b>How do I send from an iPhone to Windows?</b></summary>

AirDrop needs Apple's radio link, which Windows can't do. Use the **browser
download** (scan the QR code, enter the PIN) or the free LocalSend app on the
iPhone.
</details>

<details>
<summary><b>Nothing arrives on Windows.</b></summary>

Windows Firewall probably blocked MultiDrop on first start. Allow it for
private networks in *Windows Security → Firewall → Allow an app*, and make
sure your Wi-Fi is set to *Private*.
</details>

<details>
<summary><b>My Samsung or Android phone doesn't show up.</b></summary>

Scan the **QR code** tile in MultiDrop with the phone's camera. That works
even when the phone can't see the PC on the network.
</details>

<details>
<summary><b>Where do received files go?</b></summary>

Into `Downloads/multidrop` in your home folder; change it with `download_dir`
in the config. On Windows, *Open received files* in the window or *Received
files* in the tray menu opens it.
</details>

<details>
<summary><b>Does anything leave my network?</b></summary>

No. There is no server and no account; transfers are local and encrypted
where the protocol supports it.
</details>

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

**Tested:** AirDrop Linux → Mac; Quick Share and LocalSend against other
implementations and the official LocalSend app; the Linux desktop app; the
Windows build, installer and app window in CI; Windows AirPlay audio against
shairport-sync.

**Not verified on real devices yet:** Quick Share with Android/Samsung phones,
AirPlay mirroring and audio (no real Apple TV, HomePod or Sonos yet), Windows
on real hardware. [Device reports](../../issues/new/choose) are very welcome.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]" && .venv/bin/pytest -q
```

```
src/multidrop/     service, protocols, Linux desktop app, Windows app
tests/             unit, loopback and interop tests
tools/             install-desktop.sh, fetch-doubletake.sh, build-omdrop-owl.sh, dev/
data/              icons, systemd unit
packaging/         .deb and APT repository, Windows build and installer, release notes
docs/              architecture, protocol research, root setup
```

How it fits together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
protocol notes: [docs/RESEARCH.md](docs/RESEARCH.md) ·
how to help: [CONTRIBUTING.md](CONTRIBUTING.md)

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
