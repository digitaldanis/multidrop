## MultiDrop

Share files, your screen and audio with devices nearby – Linux and Windows.

### New in 0.2.1

- **Windows: no more empty white window** – when the app window (WebView2)
  fails to start, MultiDrop closes it and opens in the browser instead; the
  notification area icon keeps working. When it starts with Windows, nothing
  opens.
- **winget** – manifest in `packaging/winget/`; `winget install multidrop`
  works once it is accepted into the winget catalog.

### New in 0.2.0

- **Windows installer** – `MultiDrop-Setup.exe` installs just for you, no admin
  rights, with Start menu entry, *Send to* and *Start with Windows*.
- **Windows app window** – MultiDrop opens in its own window, explains on first
  start how phones send to it and keeps receiving in the notification area.
- **Explorer "Send to"** – right-click files → *Send to* → *MultiDrop*, then
  click a device.
- **AirPlay on Windows** – mirror the screen to Apple TV, Macs and AirPlay
  TVs (with `MultiDrop-Setup.exe`), and play the PC's sound on AirPlay
  speakers.
- **Phones reach the PC without setup** – Windows asks once for a firewall
  rule that lets in devices on your local network only (any Wi-Fi, nothing
  from the internet). Uninstalling removes it.
- **Browser download follows the network** – the link and QR code move along
  when the PC switches Wi-Fi; optional https link.
- **Command line on Windows** – `multidrop send/devices/receive/share` talk to
  the running app; the test suite now runs on Windows in CI.
- **Security:** received file names could point outside the download folder
  (names starting with `//`, and drive letters or network paths on Windows).
  They now always stay inside it. Please update.
- Fixes: AirPlay calls that time out are cancelled cleanly; the browser
  download stops reliably after it expires or after too many wrong PINs;
  file names Windows doesn't allow (`CON`, `a?.txt`) no longer fail; the
  local API answers bad input with 400 instead of a server error.
- **iPhones:** use the free LocalSend app. Safari on many iPhones refuses the
  browser download on a home network.

**Windows:** run `MultiDrop-Setup.exe` (per-user install, no admin rights) or
the portable `MultiDrop.exe` (Quick Share, LocalSend, browser download,
Explorer "Send to", AirPlay audio to speakers; no screen mirroring). MultiDrop
opens in its own window and keeps receiving in the notification area when you
close it. Click **Yes** when Windows asks once, so phones on your Wi-Fi can
reach the PC.

**Ubuntu/Debian:** `sudo apt install multidrop` after adding the APT repository
(see README), or `sudo apt install ./multidrop_*_all.deb`.

**Other Linux:** unpack `MultiDrop-linux-*.tar.gz` (or clone the repository) and run
`tools/install-desktop.sh --pin` – desktop app with Quick Share, LocalSend,
AirDrop (supported Wi-Fi card + omdrop-owl), AirPlay mirroring and audio.
The Python package (`.whl`/`.tar.gz`) contains the CLI and the background service.

Test status and limitations: see the README.
