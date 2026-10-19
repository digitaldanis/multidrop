# Third-party notices

MultiDrop is licensed under GPL-3.0-or-later (see [LICENSE](LICENSE)). It
contains or builds on the following third-party work.

## Included in this repository

### omdrop-plugin (MIT) – AirDrop backend

The AirDrop receiver/sender in `src/multidrop/backends/airdrop/` is based on
[omdrop-plugin](https://github.com/brentkearney/omdrop-plugin).

```
MIT License

Copyright (c) 2026 Brent Kearney

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### NearDrop (Unlicense) and Google (Apache-2.0) – Quick Share protocol definitions

The `.proto` files in `src/multidrop/backends/quickshare/proto/` were collected by
[NearDrop](https://github.com/grishka/NearDrop) (Unlicense, see
`proto/UNLICENSE-NearDrop`). They originate from Google and carry
"Copyright 2020/2023 Google LLC" and the Apache License 2.0 notice in each file
(<https://www.apache.org/licenses/LICENSE-2.0>). The generated `pb/*_pb2.py`
files are derived from them.

### wireweave (MIT) – web edition transport

The web edition in `pages/` loads [wireweave](https://github.com/AnEntrypoint/wireweave)
from the `pages/vendor/wireweave` submodule (pinned to a commit) for nostr
presence, WebRTC signalling and encrypted relay messages. It also loads
nostr-tools (MIT), xstate (MIT) and qrcode-generator (MIT) from esm.sh at
pinned versions.

```
MIT License

Copyright (c) 2026 AnEntrypoint
```

## Used as reference only (no code copied)

[OpenDrop](https://github.com/seemoo-lab/opendrop) (GPL-3.0),
[rquickshare](https://github.com/Martichou/rquickshare) (GPL-3.0),
[LocalSend protocol](https://github.com/localsend/protocol),
[UxPlay](https://github.com/FDH2/UxPlay) (GPL-3.0).

## Separate programs (not included, run as their own processes)

- [omdrop-owl](https://github.com/t4t5/omdrop-owl) / [OWL](https://github.com/seemoo-lab/owl)
  (GPL-3.0-or-later; omdrop-awdl tools GPL-2.0-only) – AirDrop radio helper,
  installed by the user.
- [doubletake](https://github.com/omarroth/doubletake) (LGPL-3.0-or-later) –
  AirPlay mirroring, downloaded by `tools/fetch-doubletake.sh`.
- PipeWire (MIT) – AirPlay audio via `libpipewire-module-raop-discover`.

## Bundled in MultiDrop.exe (Windows build)

Python (PSF-2.0), aiohttp and its dependencies (Apache-2.0 / MIT),
cryptography (Apache-2.0 or BSD-3-Clause), protobuf (BSD-3-Clause),
python-zeroconf (LGPL-2.1-or-later), segno (BSD-3-Clause), pystray (LGPL-3.0),
Pillow (MIT-CMU), PyInstaller bootloader (GPL-2.0 with bootloader exception).
Their license texts are available from the respective projects; the complete
source of MultiDrop is in this repository.

## Bundled in MultiDrop-Setup.exe for screen mirroring (`{app}\mirror`)

- [doubletake](https://github.com/omarroth/doubletake) (LGPL-3.0-or-later),
  built from release v0.5.0 with MultiDrop's
  `packaging/windows/doubletake/windows-support.patch`; its license files are
  installed next to it. It runs as its own process.
- [GStreamer](https://gstreamer.freedesktop.org/) 1.28 runtime, a subset of the
  official MSVC build: GStreamer and its plugins (LGPL-2.1-or-later), GLib
  (LGPL-2.1-or-later), x264 (GPL-2.0-or-later), OpenH264 (BSD-2-Clause) and the
  libraries they link. Sources: <https://gstreamer.freedesktop.org/src/> and
  the [cerbero](https://gitlab.freedesktop.org/gstreamer/cerbero) build recipes.

## Bundled in the .deb package

Pure-Python copies in `/usr/lib/multidrop/vendor`: protobuf (BSD-3-Clause),
aiohttp, multidict, yarl, frozenlist, propcache, aiosignal, aiohappyeyeballs
(Apache-2.0); their license files are kept in the `*.dist-info` directories.
All other libraries come from the distribution.

## Trademarks

AirDrop and AirPlay are trademarks of Apple Inc., Quick Share of Google LLC /
Samsung Electronics. MultiDrop is not affiliated with or endorsed by them.
