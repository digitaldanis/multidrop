<p align="center"><img src="docs/banner.png" alt="MultiDrop – AirDrop, Quick Share, LocalSend and AirPlay for Windows and Linux" width="100%"></p>

<h1 align="center">MultiDrop</h1>

<h3 align="center">AirDrop, Quick Share, LocalSend and AirPlay in one app – for Windows and Linux.</h3>

<p align="center">
  Your PC in the share menu of every phone in the room. iPhone, Android, Samsung.<br>
  No cloud, no cable, no account – free and open source.
</p>

<p align="center">
  <a href="../../releases/latest"><img alt="Download" src="https://img.shields.io/github/v/release/digitaldanis/multidrop?label=download&color=1f6feb"></a>
  <a href="../../releases"><img alt="Downloads" src="https://img.shields.io/github/downloads/digitaldanis/multidrop/total?color=1a7f37"></a>
  <a href="../../stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/digitaldanis/multidrop?style=flat&color=yellow"></a>
  <a href="../../actions/workflows/build.yml"><img alt="Build" src="https://github.com/digitaldanis/multidrop/actions/workflows/build.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-blue"></a>
  <img alt="Windows | Linux" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-informational">
</p>

<p align="center">
  <a href="#download"><b>Download</b></a> ·
  <a href="#what-multidrop-does">Features</a> ·
  <a href="#how-to-use-it">How to use it</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="#multidrop-in-your-language">🌍 Your language</a> ·
  <a href="https://digitaldanis.github.io/multidrop/">Website</a>
</p>

---

**MultiDrop is a free, open-source app that brings AirDrop, Google and Samsung
Quick Share, LocalSend and AirPlay to Windows and Linux PCs – all in one app.**
iPhones speak AirDrop, Android and Samsung phones speak Quick Share, the rest
use LocalSend. MultiDrop speaks all of them at the same time, so it does not
matter which phone is in front of you: send a file, it arrives.

- 📲 **Every phone, one app** – iPhone, iPad, Mac, Android, Samsung Galaxy, Pixel, Xiaomi, and any phone with a browser
- ⚡ **Right in the phone's share menu** – no app on Android and Samsung (Quick Share), none on iPhones with AirDrop on Linux
- 📺 **AirPlay from your PC** – mirror your Windows or Linux screen to an Apple TV, play your PC's sound on HomePod, Sonos and AirPlay speakers
- 🔒 **Stays local and private** – straight from device to device over your Wi-Fi, encrypted, no server, no account, no tracking
- ✋ **Asks first** – nothing lands on your PC until you accept it

## What MultiDrop does

| | Windows | Linux | Works with |
|---|:---:|:---:|---|
| **Quick Share / Nearby Share** – send & receive, QR code | ✅ | ✅ | Android, Samsung Galaxy, Pixel – no extra app |
| **AirDrop** – send & receive | via LocalSend¹ | ✅² | iPhone, iPad, Mac |
| **LocalSend** – send & receive | ✅ | ✅ | iPhone, Android, other PCs |
| **Browser download** – QR code + PIN | ✅ | ✅ | any phone, no app at all⁵ |
| **AirPlay screen mirroring** | ✅³ | ✅ | Apple TV, Mac, AirPlay TVs (Samsung, LG, Roku …) |
| **AirPlay audio** | ✅⁴ | ✅ | Apple TV, HomePod, Sonos, AirPlay speakers |

<sub>¹ Windows Wi-Fi drivers cannot speak Apple's AWDL radio, so iPhones send
to and receive from Windows with the free LocalSend app.
² needs a supported Wi-Fi card, see [AirDrop on Linux](#airdrop-on-linux).
³ with MultiDrop-Setup.exe; the portable MultiDrop.exe has no mirroring.
⁴ receivers that accept AirPlay 1 streams without a password; others are
marked as not supported.
⁵ Safari on many iPhones blocks downloads from local addresses
(“HTTPS-Only”, or “network connection lost” with the secure link); use
LocalSend there.</sub>

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

## Download

### Windows 10 / 11

1. Download **[MultiDrop-Setup.exe](../../releases/latest)**.
2. Run it. It installs just for you; only the firewall step below asks for
   admin rights.
3. Click **Yes** when Windows asks once, so phones on your Wi-Fi can reach your
   PC (a firewall rule for devices on your local network only).

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

### Fedora, Arch, openSUSE and other distributions

```bash
git clone https://github.com/digitaldanis/multidrop.git ~/multidrop
~/multidrop/tools/install-desktop.sh --pin
```

Needs Python ≥ 3.11, GTK 4 and libadwaita. The installer needs no root and
tells you if a system package is missing. Uninstall with
`tools/install-desktop.sh --remove`.

## How to use it

**Receive:** just send from your phone. MultiDrop shows up like any other
nearby device and asks you before it accepts anything. Files land in
`Downloads/multidrop` in your home folder.

**Send:** drop files on a device in the MultiDrop window, or click the device
and pick them.

- **Windows** – MultiDrop opens in its own window and explains on first start
  how phones send to it. Closing the window keeps it receiving in the
  notification area. Turn on *Send to* in *Settings*, then right-click files in
  Explorer → *Send to* → *MultiDrop*. *Show this PC's screen on a TV* mirrors
  the screen to an Apple TV or AirPlay TV (enter the code the TV shows the
  first time); *Play this PC's sound on a speaker* sends your audio to an
  AirPlay speaker.
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
<summary><b>Is there AirDrop for Windows?</b></summary>

Not Apple's radio itself – Windows Wi-Fi drivers cannot speak AWDL. MultiDrop
covers everything else on Windows: iPhones send to the PC with the free
LocalSend app, the PC sends to iPhones with LocalSend or – where Safari allows
it – the browser download, and Android and Samsung phones use their built-in
Quick Share. On top you get AirPlay screen mirroring and audio.
</details>

<details>
<summary><b>Is there AirDrop for Linux?</b></summary>

Yes. MultiDrop sends to and receives from iPhones, iPads and Macs with real
AirDrop on Linux, using a supported Wi-Fi card (MediaTek MT7925, Intel in
exclusive mode) – see [AirDrop on Linux](#airdrop-on-linux). Without one,
iPhones use LocalSend.
</details>

<details>
<summary><b>How do I transfer files from an iPhone to a Windows PC wirelessly?</b></summary>

Start MultiDrop on the PC. On the iPhone, install the free LocalSend app and
pick the PC. No cable, no iTunes, no cloud.
</details>

<details>
<summary><b>Can I use Quick Share (Nearby Share) on Linux or Windows?</b></summary>

Yes. MultiDrop appears in Android's and Samsung's Quick Share menu on both
Windows and Linux – no Google account, no app on the phone. Samsung phones
sometimes hide PCs; then scan the **QR code** tile.
</details>

<details>
<summary><b>How do I mirror my Windows or Linux screen to an Apple TV?</b></summary>

Pick the TV under *Show this PC's screen on a TV* (Windows) or *Mirror screen*
(Linux) and enter the code the TV shows the first time. Works with Apple TV,
Macs and AirPlay TVs from Samsung, LG, Roku and others.
</details>

<details>
<summary><b>Is MultiDrop free? Does anything leave my network?</b></summary>

Free and open source (GPL-3.0). There is no server and no account; transfers go
directly between the devices on your network, encrypted where the protocol
supports it.
</details>

<details>
<summary><b>Nothing arrives on Windows.</b></summary>

Windows Firewall probably blocks phones. If MultiDrop's window shows
*Phones can't reach this PC yet*, click **Let phones connect** and choose
**Yes**. This adds one rule for devices on your local network (any Wi-Fi,
nothing from the internet); the portable `MultiDrop.exe` asks for it on first
start, and uninstalling removes it.
</details>

<details>
<summary><b>Where do received files go?</b></summary>

Into `Downloads/multidrop` in your home folder; change it with `download_dir`
in the config. On Windows, *Open received files* in the window or *Received
files* in the tray menu opens it.
</details>

## Guides

[AirDrop for Linux](https://digitaldanis.github.io/multidrop/airdrop-for-linux/) ·
[AirDrop for Windows](https://digitaldanis.github.io/multidrop/airdrop-for-windows/) ·
[Quick Share for Linux](https://digitaldanis.github.io/multidrop/quick-share-for-linux/) ·
[Quick Share for Windows](https://digitaldanis.github.io/multidrop/quick-share-for-windows/) ·
[iPhone to PC without a cable](https://digitaldanis.github.io/multidrop/iphone-to-pc/) ·
[Mirror a PC to Apple TV](https://digitaldanis.github.io/multidrop/mirror-pc-to-apple-tv/) ·
[MultiDrop vs LocalSend, NearDrop, rquickshare](https://digitaldanis.github.io/multidrop/alternatives/)

## MultiDrop in your language

<!-- generated by tools/build_site.py --readme; edit site/strings.toml -->
- 🇩🇪 **Deutsch** – AirDrop, Quick Share, LocalSend und AirPlay in einer App für Windows und Linux: Dateien zwischen PC und iPhone, Android oder Samsung senden – ohne Kabel, Cloud oder Konto. [Mehr](https://digitaldanis.github.io/multidrop/de/)
- 🇪🇸 **Español** – AirDrop, Quick Share, LocalSend y AirPlay en una sola app para Windows y Linux: envía archivos entre tu PC y tu iPhone, Android o Samsung, sin cables, sin nube y sin cuenta. [Más](https://digitaldanis.github.io/multidrop/es/)
- 🇫🇷 **Français** – AirDrop, Quick Share, LocalSend et AirPlay dans une seule appli pour Windows et Linux : envoyez des fichiers entre votre PC et un iPhone, Android ou Samsung, sans câble, sans cloud, sans compte. [Plus](https://digitaldanis.github.io/multidrop/fr/)
- 🇧🇷 **Português** – AirDrop, Quick Share, LocalSend e AirPlay em um só app para Windows e Linux: envie arquivos entre o PC e iPhone, Android ou Samsung, sem cabo, sem nuvem e sem conta. [Mais](https://digitaldanis.github.io/multidrop/pt/)
- 🇮🇹 **Italiano** – AirDrop, Quick Share, LocalSend e AirPlay in un'unica app per Windows e Linux: invia file tra PC e iPhone, Android o Samsung, senza cavi, cloud o account. [Altro](https://digitaldanis.github.io/multidrop/it/)
- 🇳🇱 **Nederlands** – AirDrop, Quick Share, LocalSend en AirPlay in één app voor Windows en Linux: stuur bestanden tussen pc en iPhone, Android of Samsung, zonder kabel, cloud of account. [Meer](https://digitaldanis.github.io/multidrop/nl/)
- 🇵🇱 **Polski** – AirDrop, Quick Share, LocalSend i AirPlay w jednej aplikacji dla Windows i Linux: przesyłaj pliki między PC a iPhonem, Androidem lub Samsungiem, bez kabla, chmury i konta. [Więcej](https://digitaldanis.github.io/multidrop/pl/)
- 🇹🇷 **Türkçe** – AirDrop, Quick Share, LocalSend ve AirPlay tek uygulamada, Windows ve Linux için: PC ile iPhone, Android veya Samsung arasında kablosuz, bulutsuz ve hesapsız dosya gönderin. [Daha fazla](https://digitaldanis.github.io/multidrop/tr/)
- 🇷🇺 **Русский** – AirDrop, Quick Share, LocalSend и AirPlay в одном приложении для Windows и Linux: передавайте файлы между ПК и iPhone, Android или Samsung без кабеля, облака и аккаунта. [Подробнее](https://digitaldanis.github.io/multidrop/ru/)
- 🇺🇦 **Українська** – AirDrop, Quick Share, LocalSend і AirPlay в одному застосунку для Windows і Linux: надсилайте файли між ПК та iPhone, Android чи Samsung без кабелю, хмари й облікового запису. [Докладніше](https://digitaldanis.github.io/multidrop/uk/)
- 🇨🇳 **中文** – 一个应用集成 AirDrop、Quick Share、LocalSend 和 AirPlay，支持 Windows 和 Linux：在电脑与 iPhone、安卓或三星手机之间传文件，无需数据线、云端或账号。 [了解更多](https://digitaldanis.github.io/multidrop/zh/)
- 🇯🇵 **日本語** – AirDrop、Quick Share、LocalSend、AirPlay をひとつにまとめた Windows・Linux 用アプリ。PC と iPhone、Android、Samsung の間でケーブルもクラウドもアカウントも不要でファイルを送れます。 [詳しく](https://digitaldanis.github.io/multidrop/ja/)
- 🇰🇷 **한국어** – AirDrop, Quick Share, LocalSend, AirPlay를 하나로 담은 Windows·Linux 앱: 케이블, 클라우드, 계정 없이 PC와 iPhone, 안드로이드, 삼성 폰 사이에서 파일을 주고받으세요. [더 보기](https://digitaldanis.github.io/multidrop/ko/)
- 🇮🇳 **हिन्दी** – Windows और Linux के लिए एक ही ऐप में AirDrop, Quick Share, LocalSend और AirPlay: PC और iPhone, Android या Samsung के बीच बिना केबल, क्लाउड या अकाउंट के फ़ाइलें भेजें। [और जानें](https://digitaldanis.github.io/multidrop/hi/)
- 🇸🇦 **العربية** – ‏AirDrop وQuick Share وLocalSend وAirPlay في تطبيق واحد لنظامي Windows وLinux: أرسل الملفات بين الكمبيوتر وiPhone أو Android أو Samsung بلا كابل ولا سحابة ولا حساب. [المزيد](https://digitaldanis.github.io/multidrop/ar/)
- 🇮🇩 **Bahasa Indonesia** – AirDrop, Quick Share, LocalSend, dan AirPlay dalam satu aplikasi untuk Windows dan Linux: kirim file antara PC dan iPhone, Android, atau Samsung tanpa kabel, cloud, atau akun. [Selengkapnya](https://digitaldanis.github.io/multidrop/id/)
- 🇻🇳 **Tiếng Việt** – AirDrop, Quick Share, LocalSend và AirPlay trong một ứng dụng cho Windows và Linux: gửi tệp giữa PC và iPhone, Android hoặc Samsung, không cần cáp, đám mây hay tài khoản. [Xem thêm](https://digitaldanis.github.io/multidrop/vi/)
<!-- end of generated block -->

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

**Tested with real devices:** Quick Share with Android and Samsung Galaxy
phones; AirPlay screen mirroring with Apple TVs; AirDrop Linux → Mac.

**Tested in CI and against other implementations:** LocalSend with the official
LocalSend app; the Windows build, installer and app window; Windows AirPlay
audio against shairport-sync; screen mirroring against doubletake's test
receiver.

**Not verified yet:** AirPlay audio on HomePod and Sonos, Windows on more
hardware. [Device
reports](../../issues/new/choose) are very welcome – "Galaxy S24 → Windows 11
works" helps a lot.

⭐ **If MultiDrop saved you a cable, star the repo** – it helps other people
find it.

## Web version

`pages/` is a browser edition at
[digitaldanis.github.io/multidrop/web](https://digitaldanis.github.io/multidrop/web/):
nothing to install, browser to browser only. AirDrop, Quick Share, LocalSend,
AirPlay and the desktop integration need the app.

Open the page on two devices on the same Wi-Fi and they see each other – no
code, and the receiver accepts every transfer. Only devices that reach each
other directly on the local network are shown. To reach your own devices on
other networks, *Remember a device* pairs them once with a 6-digit code or QR
code.

Devices find each other through public nostr relays
([wireweave](https://github.com/AnEntrypoint/wireweave)); the relays see a
neutral name, real device names and files go directly between the browsers
over WebRTC. If a remembered device can't be reached directly, the page asks
before sending through the relays (encrypted). Receiving needs Chrome, Edge or
Opera, which write straight into a folder you choose.

Work on it with `git clone --recurse-submodules …` or
`git submodule update --init`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]" && .venv/bin/pytest -q
```

```
android/           native Android app, build scripts, protocol and emulator tests
src/multidrop/     service, protocols, Linux desktop app, Windows app
tests/             unit, loopback and interop tests
tools/             install-desktop.sh, fetch-doubletake.sh, build-omdrop-owl.sh, build_site.py, dev/
site/              website texts in every language and guide pages (GitHub Pages)
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
