"""AirPlay screen mirroring on Windows: doubletake's Windows build with a
private GStreamer, driven like on Linux (desktop.py's Mirror) but over
loopback TCP, because Python has no AF_UNIX on Windows.

doubletake publishes its address and a random token in a control file in
MultiDrop's state directory; every request carries the token. The engine
lives in a "mirror" folder next to MultiDrop.exe (installer) or wherever
MULTIDROP_MIRROR_DIR points:

    mirror/doubletake.exe
    mirror/gstreamer/bin/gst-launch-1.0.exe …
    mirror/gstreamer/lib/gstreamer-1.0/*.dll
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

EXE = "doubletake.exe" if os.name == "nt" else "doubletake"


class MirrorError(Exception):
    pass


def engine_dir() -> Path | None:
    """The folder with doubletake and its GStreamer, if this build has one."""
    candidates = []
    if os.environ.get("MULTIDROP_MIRROR_DIR"):
        candidates.append(Path(os.environ["MULTIDROP_MIRROR_DIR"]))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "mirror")
    candidates.append(Path(__file__).resolve().parents[2] / "build" / "mirror")
    return next((d for d in candidates if (d / EXE).is_file()), None)


def engine_env(engine: Path, state_dir: Path) -> dict[str, str]:
    """Keep the private GStreamer to itself: its own plugins and registry,
    never those of a GStreamer the user installed system-wide."""
    env = dict(os.environ)
    gst = engine / "gstreamer"
    if gst.is_dir():
        env["DOUBLETAKE_GSTREAMER_ROOT"] = str(gst)
        env["GST_PLUGIN_SYSTEM_PATH"] = str(gst / "lib" / "gstreamer-1.0")
        env["GST_REGISTRY"] = str(state_dir / "gstreamer-registry.bin")
        scanner = gst / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"
        if scanner.is_file():
            env["GST_PLUGIN_SCANNER"] = str(scanner)
        env.pop("GST_PLUGIN_PATH", None)
    return env


def is_mirror_target(device: dict, caps: dict[str, dict]) -> bool:
    """caps: multidropd's /api/airplay entries by device id and IP. Receivers
    that announce no screen mirroring (speakers) are hidden; unknown ones are
    shown, so nothing disappears just because its mDNS answer was late."""
    info = caps.get((device.get("device_id") or "").upper()) or caps.get(device.get("ip") or "")
    return info is None or info.get("screen") is not False


class ScreenMirror:
    """One doubletake daemon for MultiDrop's lifetime, started on first use."""

    def __init__(self, state_dir: Path, log_dir: Path, engine: Path | None = None,
                 airplay_caps: Callable[[], list[dict]] | None = None,
                 extra_args: tuple[str, ...] = ()):
        self.engine = engine if engine is not None else engine_dir()
        self.extra_args = extra_args         # e.g. ("-test",): synthetic video in tests
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.control = state_dir / "doubletake-control.json"
        self.proc: subprocess.Popen | None = None
        self._airplay_caps = airplay_caps
        self._caps: dict[str, dict] = {}
        self._caps_at = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.engine is not None

    # -- doubletake's control protocol ---------------------------------------

    def request(self, cmd: str, timeout: float = 30, **fields) -> dict:
        try:
            info = json.loads(self.control.read_text())
            host, _, port = info["addr"].rpartition(":")
            req = {"cmd": cmd, "token": info["token"],
                   **{k: v for k, v in fields.items() if v}}
        except (OSError, ValueError, KeyError) as e:
            raise MirrorError(f"doubletake is not running ({e})") from e
        try:
            with socket.create_connection((host.strip("[]"), int(port)), timeout) as s:
                s.sendall(json.dumps(req).encode() + b"\n")
                buf = b""
                while chunk := s.recv(65536):
                    buf += chunk
                    try:
                        return json.loads(buf)
                    except ValueError:
                        continue        # incomplete
        except OSError as e:
            raise MirrorError(f"doubletake not reachable ({e})") from e
        raise MirrorError("doubletake: empty answer")

    def alive(self) -> bool:
        try:
            return bool(self.request("status", timeout=3).get("ok"))
        except MirrorError:
            return False

    def ensure(self) -> None:
        """Start doubletake unless it already runs (also one left over from
        an earlier MultiDrop that ended without stopping it)."""
        with self._lock:
            if self.alive():
                return
            if self.engine is None:
                raise MirrorError("screen mirroring is not part of this MultiDrop build")
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            argv = [str(self.engine / EXE), *self.extra_args, "-daemonize",
                    "-socket", "tcp:127.0.0.1:0", "-control-file", str(self.control)]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            with open(self.log_dir / "doubletake.log", "ab") as out:
                self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                             stderr=subprocess.STDOUT, creationflags=flags,
                                             env=engine_env(self.engine, self.state_dir))
            for _ in range(100):
                if self.alive():
                    return
                if self.proc.poll() is not None:
                    raise MirrorError(f"doubletake exited: {self._log_tail()}")
                time.sleep(0.1)
            raise MirrorError("doubletake does not start")

    def _log_tail(self, lines: int = 2) -> str:
        try:
            text = (self.log_dir / "doubletake.log").read_text(errors="replace")
        except OSError:
            return "no log"
        return " / ".join(text.strip().splitlines()[-lines:]) or "no log"

    # -- what the window shows -------------------------------------------------

    def _screen_caps(self) -> dict[str, dict]:
        if self._airplay_caps is not None and time.time() - self._caps_at > 30:
            try:
                found = self._airplay_caps()
                self._caps = {k: d for d in found for k in (d.get("device_id"), d.get("ip")) if k}
                self._caps_at = time.time()
            except Exception as e:      # the service may be busy; keep the old answer
                log.debug("AirPlay capabilities: %s", e)
        return self._caps

    def snapshot(self) -> dict:
        """Screens to mirror to and active streams, for the window."""
        if not self.available:
            return {"available": False, "screens": [], "streams": [], "error": ""}
        try:
            self.ensure()
            devices = self.request("devices").get("devices") or []
            status = self.request("status")
        except MirrorError as e:
            return {"available": True, "screens": [], "streams": [], "error": str(e)}
        caps = self._screen_caps()
        screens = sorted(({"name": d.get("name") or d.get("ip"), "ip": d.get("ip"),
                           "port": d.get("port"), "model": d.get("model") or ""}
                          for d in devices if d.get("ip") and is_mirror_target(d, caps)),
                         key=lambda d: d["name"].lower())
        streams = [{"device": s.get("device") or s.get("device_ip"), "ip": s.get("device_ip"),
                    "state": s.get("state"), "credential": s.get("credential_kind") or ""}
                   for s in status.get("streams") or []]
        return {"available": True, "screens": screens, "streams": streams,
                "error": status.get("error") or ""}

    def _checked(self, resp: dict) -> dict:
        if not resp.get("ok"):
            raise MirrorError(resp.get("error") or "doubletake refused")
        return resp

    def start(self, ip: str, port: int | None = None) -> None:
        self.ensure()
        self._checked(self.request("connect", target=ip, port=port))

    def pin(self, ip: str, code: str) -> None:
        self._checked(self.request("connect", target=ip, pin=code))

    def stop(self, ip: str | None = None) -> None:
        self._checked(self.request("disconnect", target=ip, timeout=10))

    def shutdown(self) -> None:
        """End all streams; stop doubletake if MultiDrop started it."""
        try:
            self.request("disconnect", timeout=5)
        except MirrorError:
            pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
            # Windows' terminate() is TerminateProcess: doubletake cannot
            # remove its control file itself, and the address would be stale.
            self.control.unlink(missing_ok=True)
