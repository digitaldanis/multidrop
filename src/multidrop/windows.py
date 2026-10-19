"""MultiDrop for Windows: background service, tray icon, UI in the browser.

Quick Share and LocalSend work as on Linux; the tray menu plays the PC's
audio on AirPlay speakers (win_audio.py) and the window mirrors the screen to
an Apple TV (win_mirror.py, doubletake's Windows build). AirDrop needs Apple's
AWDL radio link, which Windows Wi-Fi drivers cannot provide, so it is not part
of the Windows build.

Built into MultiDrop.exe by GitHub Actions (.github/workflows/build.yml).
`MultiDrop.exe FILE...` (Explorer's "Send to") hands the files to the running
instance and shows them in the UI to pick a device.
`MultiDrop.exe --selftest` starts the service, checks its API and exits.
`MultiDrop.exe --allow-network` (run elevated by the installer, else asked for
once on first start) lets phones on the local network through the Windows
Firewall, see winshell.py.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from multidrop import daemon, winshell
from multidrop.config import Config, runtime_dir
from multidrop.daemon.api import TOKEN_HEADER

APP_NAME = "MultiDrop"
log = logging.getLogger("multidrop.windows")


def _resource(name: str) -> Path:
    """Bundled file (PyInstaller unpacks to sys._MEIPASS) or the source tree."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "data" / "icons" / name


def api(url: str, path: str, body: dict | None = None, method: str | None = None):
    """Call the service's API with the token from its UI URL."""
    base, _, token = url.partition("/#t=")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={TOKEN_HEADER: token,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class Service:
    """multidropd's run() on its own event loop thread."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.stop_event: asyncio.Event | None = None
        self.url = ""
        self.error: BaseException | None = None
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="multidropd", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def main():
            self.stop_event = asyncio.Event()
            started = self.loop.create_future()
            started.add_done_callback(lambda f: self._started(f))
            await daemon.run(self.cfg, started=started, stop=self.stop_event,
                             notifications=False)
        try:
            self.loop.run_until_complete(main())
        except BaseException as e:      # reported to the user by start()
            self.error = e
        finally:
            self._ready.set()

    def _started(self, fut: asyncio.Future) -> None:
        if not fut.cancelled():
            self.url = fut.result()
        self._ready.set()

    def start(self, timeout: float = 30) -> str:
        self.thread.start()
        self._ready.wait(timeout)
        if self.error is not None:
            raise self.error
        if not self.url:
            raise TimeoutError("MultiDrop service did not start")
        return self.url

    def stop(self) -> None:
        if self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(10)

    def api(self, path: str, body: dict | None = None, method: str | None = None):
        return api(self.url, path, body, method)


def _watch_events(service: Service, notify, show) -> None:
    """Bring MultiDrop to the front when someone wants to send (with a Windows
    notification; Accept/Decline notifications exist only on Linux), when
    files are handed over and when it is started a second time."""
    last = None                         # events before the start (history file) are old news
    while last is None:
        try:
            last = max((e["seq"] for e in service.api("/api/history")), default=0)
        except Exception as e:
            log.debug("event poll: %s", e)
            time.sleep(1)
    while True:
        time.sleep(1)
        try:
            for e in service.api(f"/api/history?since={last}"):   # only what is new
                last = max(last, e["seq"])
                if e["event"] == "offer":
                    offer = e.get("offer") or {}
                    count = len(offer.get("files") or [])
                    notify(f"{offer.get('from', 'A device')} wants to send {count} item(s)",
                           APP_NAME)
                    show()
                elif e["event"] == "show_ui" or (e["event"] == "selection" and e.get("count")):
                    show()
        except Exception as e:          # service restarting or stopping
            log.debug("event poll: %s", e)


class NetworkAccess:
    """Firewall rules for MultiDrop's ports, asked for once automatically and
    afterwards from the notice in the window."""

    MDNS = 5353

    def __init__(self, cfg: Config, config_args: list[str] = ()):
        self.tcp = [p for p in (cfg.localsend.port, cfg.quickshare.port,
                                cfg.localsend.share_port) if p]
        self.udp = [p for p in (cfg.localsend.multicast_port, self.MDNS) if p]
        self._config_args = list(config_args)
        self._asked_flag = cfg.state_dir / "windows-network-asked"

    def status(self) -> dict:
        return {"ok": winshell.firewall_rule_present()}

    def request(self) -> dict:
        self._asked_flag.parent.mkdir(parents=True, exist_ok=True)
        self._asked_flag.touch()
        winshell.request_network_access(self._config_args)
        return self.status()

    def allow(self) -> int:
        """The elevated part (--allow-network)."""
        return winshell.allow_network(self.tcp, self.udp)

    def ask_once(self) -> None:
        if self._asked_flag.exists():
            return
        try:
            if not self.status()["ok"]:
                log.info("asking for network access: %s", self.request())
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("network access: %s", e)


def _exit(code: int) -> None:
    """Leave even if a library thread (zeroconf, executor) is still blocked:
    a lingering thread kept MultiDrop.exe alive after quitting."""
    logging.shutdown()
    if sys.stdout is not None:
        sys.stdout.flush()
    os._exit(code)


def _selftest(service: Service) -> int:
    status = service.api("/api/status")
    if sys.stdout is not None:          # the windowed .exe has no console
        print(json.dumps(status, indent=2))
    ok = "quickshare" in status["protocols"] or "localsend" in status["protocols"]
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="MultiDrop", description=__doc__.splitlines()[0])
    p.add_argument("--config", help="config file")
    p.add_argument("--no-browser", action="store_true",
                   help="start in the background, without showing the window")
    p.add_argument("--browser", action="store_true",
                   help="show the UI in the web browser instead of the app window")
    p.add_argument("--selftest", action="store_true", help="start, check the API, exit")
    p.add_argument("--allow-network", action="store_true",
                   help="let phones on the local network through the Windows Firewall "
                        "(needs admin rights; the installer runs it)")
    p.add_argument("paths", nargs="*", help="files to send (Explorer's \"Send to\")")
    args = p.parse_args(argv)
    config_args = ["--config", args.config] if args.config else []
    if args.allow_network:              # elevated: from the installer or NetworkAccess.request
        cfg = Config.load(Path(args.config) if args.config else None)
        return NetworkAccess(cfg).allow()
    paths = [str(Path(x).resolve()) for x in args.paths]

    log_dir = runtime_dir().parent
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(log_dir / "multidrop.log"),
                        level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # One line per UI request would grow the log all day (the window polls).
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    cfg = Config.load(Path(args.config) if args.config else None)
    service = Service(cfg)
    try:
        url = service.start()
    except daemon.AlreadyRunning:
        # Second start (Start menu, "Send to"): hand over and show the running one.
        try:
            url = (runtime_dir() / "url").read_text().strip()
            if paths:
                api(url, "/api/selection", {"paths": paths})
            else:
                api(url, "/api/show", {})
            if args.browser:
                webbrowser.open(url)
        except (OSError, ValueError) as e:
            log.warning("handing over to the running instance failed: %s", e)
        return 0
    if args.selftest:
        try:
            code = _selftest(service)
        except Exception as e:
            log.exception("self-test failed: %s", e)
            code = 1
        service.stop()
        _exit(code)
    if paths:
        try:
            service.api("/api/selection", {"paths": paths})
        except (OSError, ValueError) as e:
            log.warning("files from the command line: %s", e)
    network = NetworkAccess(cfg, config_args)
    threading.Thread(target=network.ask_once, daemon=True).start()
    window = None
    use_window = not args.browser and importlib.util.find_spec("webview") is not None
    if not use_window and (paths or not args.no_browser):
        webbrowser.open(url)

    try:
        import pystray
        from PIL import Image
    except ImportError:                 # source checkout without the tray extras
        print(f"{APP_NAME} is running: {url}  (Ctrl+C quits)")
        try:
            while service.thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        service.stop()
        return 0

    def open_ui(icon=None, item=None):
        if window is not None:
            window.show()
        else:
            webbrowser.open(service.url)

    def open_downloads(icon=None, item=None):
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(cfg.download_dir)) if os.name == "nt" else webbrowser.open(
            cfg.download_dir.as_uri())

    quitting = threading.Event()

    def quit_app(icon, item=None):
        quitting.set()
        icon.stop()
        if window is not None:
            window.quit()

    def background(fn, *args):
        """Slow actions (network) off the tray thread; errors as a notification."""
        def run():
            try:
                fn(*args)
            except Exception as e:
                log.warning("AirPlay: %s", e)
                icon.notify(f"AirPlay: {e}", APP_NAME)
        threading.Thread(target=run, daemon=True).start()

    audio = None
    if importlib.util.find_spec("soundcard") is not None:
        from multidrop import win_audio
        audio = win_audio.AirPlayAudio(service.loop, on_change=lambda: icon.update_menu())

    def speaker_item(r):
        return pystray.MenuItem(r.name if r.supported else f"{r.name} (not supported)",
                                lambda: background(audio.play, r), enabled=r.supported,
                                checked=lambda item: audio.current is r, radio=True)

    def speaker_items():
        yield pystray.MenuItem("This computer", lambda: background(audio.stop),
                               checked=lambda item: audio.current is None, radio=True)
        yield from (speaker_item(r) for r in audio.receivers)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Search for speakers", lambda: background(audio.scan))

    def volume_item(pct):
        return pystray.MenuItem(f"{pct} %", lambda: background(audio.set_volume, pct / 100),
                                checked=lambda item: round(audio.volume * 100) == pct, radio=True)

    audio_menu = () if audio is None else (
        pystray.MenuItem("Play audio on", pystray.Menu(speaker_items)),
        pystray.MenuItem("AirPlay volume", pystray.Menu(lambda: map(volume_item, (25, 50, 75, 100))),
                         enabled=lambda item: audio.current is not None),
        pystray.Menu.SEPARATOR)

    def toggle(enabled, switch):
        def action(icon, item):
            try:
                switch(not enabled())
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("%s: %s", item.text, e)
                icon.notify(f"Could not change \"{item.text}\"", APP_NAME)
        return action

    icon = pystray.Icon(APP_NAME, Image.open(_resource("multidrop.png")), APP_NAME,
                        menu=pystray.Menu(
                            pystray.MenuItem(f"Open {APP_NAME}", open_ui, default=True),
                            pystray.MenuItem("Received files", open_downloads),
                            pystray.Menu.SEPARATOR,
                            *audio_menu,
                            pystray.MenuItem("Start with Windows",
                                             toggle(winshell.autostart_enabled,
                                                    winshell.set_autostart),
                                             checked=lambda item: winshell.autostart_enabled()),
                            pystray.MenuItem("Show in \"Send to\" menu",
                                             toggle(winshell.sendto_enabled, winshell.set_sendto),
                                             checked=lambda item: winshell.sendto_enabled()),
                            pystray.Menu.SEPARATOR,
                            pystray.MenuItem("Quit", quit_app)))
    from multidrop.win_mirror import ScreenMirror
    mirror = ScreenMirror(cfg.state_dir, log_dir,
                          airplay_caps=lambda: service.api("/api/airplay?timeout=8"))
    if use_window:
        from multidrop.win_window import AppWindow, DesktopApi
        window = AppWindow(
            url, DesktopApi(cfg.state_dir, cfg.download_dir, audio, mirror, network),
            log_dir / "webview",
            hidden=args.no_browser and not paths,
            on_first_hide=lambda: icon.notify(
                f"{APP_NAME} keeps running here, so phones can still send you files. "
                "Quit it from this icon.", APP_NAME))
    threading.Thread(target=_watch_events, args=(service, icon.notify, open_ui),
                     daemon=True).start()
    if audio is not None:
        background(audio.scan)
    if window is None:
        icon.run()
    else:
        icon.run_detached()
        try:
            window.run()                # until "Quit"
        except Exception as e:          # no WebView2 runtime: use the browser
            log.warning("app window unavailable, using the browser: %s", e)
            window = None
            if not (args.no_browser and not paths):     # not when starting with Windows
                webbrowser.open(url)
        if not quitting.is_set():
            # The window ended without "Quit": keep receiving, UI in the browser.
            log.warning("app window ended unexpectedly; MultiDrop keeps running in the tray")
            window = None
        quitting.wait()
    if audio is not None:
        audio.stop()
    mirror.shutdown()
    service.stop()
    _exit(0)


if __name__ == "__main__":
    sys.exit(main())
