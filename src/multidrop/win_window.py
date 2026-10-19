"""MultiDrop's own window on Windows: the web UI in Edge WebView2 (pywebview),
plus what only the desktop can do, offered to the page as
`window.pywebview.api`: Windows integration settings, AirPlay audio, AirPlay
screen mirroring and the one-time welcome.

Closing the window keeps MultiDrop running in the tray, so phones can still
send files; "Quit" in the tray menu ends it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from multidrop import winshell

log = logging.getLogger(__name__)

TITLE = "MultiDrop"


class DesktopApi:
    """Called from app.js; every method runs on a pywebview worker thread."""

    def __init__(self, state_dir: Path, downloads: Path, audio=None, mirror=None,
                 network=None):
        self._welcome_flag = state_dir / "windows-welcome-done"
        self._downloads = downloads
        self._audio = audio
        self._mirror = mirror
        self._network = network

    def settings(self) -> dict:
        return {"autostart": winshell.autostart_enabled(),
                "sendto": winshell.sendto_enabled(),
                "welcome": not self._welcome_flag.exists(),
                "audio": self._audio is not None,
                "mirror": self._mirror is not None and self._mirror.available}

    def set_autostart(self, on: bool) -> dict:
        winshell.set_autostart(bool(on))
        return self.settings()

    def set_sendto(self, on: bool) -> dict:
        winshell.set_sendto(bool(on))
        return self.settings()

    def open_downloads(self) -> None:
        self._downloads.mkdir(parents=True, exist_ok=True)
        os.startfile(self._downloads)       # noqa: S606 (our own folder)

    def network(self) -> dict:
        return self._network.status() if self._network else {"ok": True}

    def allow_network(self) -> dict:
        return self._network.request()

    def welcome_done(self) -> None:
        self._welcome_flag.parent.mkdir(parents=True, exist_ok=True)
        self._welcome_flag.touch()

    # AirPlay audio --------------------------------------------------------

    def speakers(self) -> dict:
        a = self._audio
        return {"current": a.current.name if a.current else None,
                "volume": a.volume,
                "list": [{"name": r.name, "supported": r.supported} for r in a.receivers]}

    def search_speakers(self) -> dict:
        self._audio.scan()
        return self.speakers()

    def play_on(self, name: str) -> dict:
        receiver = next((r for r in self._audio.receivers if r.name == name), None)
        if receiver is None:
            raise ValueError(f"{name} is no longer available – search again")
        self._audio.play(receiver)
        return self.speakers()

    def stop_audio(self) -> dict:
        self._audio.stop()
        return self.speakers()

    def set_volume(self, level: float) -> dict:
        self._audio.set_volume(min(max(float(level), 0.0), 1.0))
        return self.speakers()

    # AirPlay screen mirroring (win_mirror.ScreenMirror) -----------------------

    def screens(self) -> dict:
        return self._mirror.snapshot()

    def mirror_to(self, ip: str, port: int | None = None) -> dict:
        self._mirror.start(str(ip), int(port) if port else None)
        return self.screens()

    def mirror_pin(self, ip: str, code: str) -> dict:
        self._mirror.pin(str(ip), str(code).strip())
        return self.screens()

    def stop_mirror(self, ip: str | None = None) -> dict:
        self._mirror.stop(str(ip) if ip else None)
        return self.screens()


class AppWindow:
    def __init__(self, url: str, api: DesktopApi, storage: Path, hidden: bool = False,
                 on_first_hide: Callable[[], None] = lambda: None):
        import webview
        self._webview = webview
        self._storage = storage
        self._on_first_hide = on_first_hide
        self._hidden_once = False
        self.quitting = False
        self.window = webview.create_window(TITLE, url, js_api=api, width=1040, height=780,
                                            min_size=(420, 520), hidden=hidden)
        self.window.events.closing += self._closing

    def _closing(self) -> bool:
        if self.quitting:
            return True
        self.window.hide()                  # keep receiving in the background
        log.info("window closed: hidden, MultiDrop keeps running")
        if not self._hidden_once:
            self._hidden_once = True
            try:
                self._on_first_hide()
            except Exception as e:          # a failed hint must not end the app
                log.warning("hint after closing: %s", e)
        return False

    def show(self) -> None:
        self.window.show()
        self.window.restore()
        # Bring it to the front without staying on top.
        self.window.on_top = True
        self.window.on_top = False

    def quit(self) -> None:
        self.quitting = True
        self.window.destroy()

    def run(self) -> None:
        """Blocks until quit(); raises when WebView2 is not available."""
        self._webview.start(gui="edgechromium", private_mode=False,
                            storage_path=str(self._storage))
