"""AirPlay screen mirroring on Windows (win_mirror.py). Runs on Linux too:
the control protocol is loopback TCP with a token on every platform.

MULTIDROP_TEST_MIRROR_DIR=<dir with doubletake and doubletake-test-receiver>
also runs the real engine against doubletake's in-process test receiver,
with synthetic video instead of screen capture (encoder:
MULTIDROP_TEST_MIRROR_HWACCEL, default openh264;
MULTIDROP_TEST_MIRROR_EXPECT_VIDEO=1 where that encoder works)."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from multidrop import win_mirror
from multidrop.win_mirror import MirrorError, ScreenMirror


class FakeDoubletake:
    """doubletake's TCP control endpoint: one JSON request, one answer."""

    def __init__(self, control: Path, answers: dict):
        self.answers = answers
        self.requests: list[dict] = []
        self.sock = socket.create_server(("127.0.0.1", 0))
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(json.dumps({"addr": "127.0.0.1:%d" % self.sock.getsockname()[1],
                                       "token": "secret", "pid": 1}))
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile().readline())
                self.requests.append(req)
                if req.get("token") != "secret":
                    answer = {"ok": False, "error": "unauthorized"}
                else:
                    answer = self.answers.get(req["cmd"], {"ok": True})
                conn.sendall(json.dumps(answer).encode() + b"\n")

    def close(self):
        # close() alone does not wake an accept() blocked in another thread on
        # Linux; the socket would keep accepting.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


@pytest.fixture
def engine(tmp_path):
    d = tmp_path / "mirror"
    d.mkdir()
    (d / win_mirror.EXE).write_text("")
    return d


def test_engine_dir_from_environment_or_next_to_the_exe(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("MULTIDROP_MIRROR_DIR", str(engine))
    assert win_mirror.engine_dir() == engine
    monkeypatch.setenv("MULTIDROP_MIRROR_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MultiDrop.exe"))
    assert win_mirror.engine_dir() == engine


def test_private_gstreamer_ignores_the_system_one(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("GST_PLUGIN_PATH", r"C:\gstreamer\lib")
    assert "DOUBLETAKE_GSTREAMER_ROOT" not in win_mirror.engine_env(engine, tmp_path)
    (engine / "gstreamer" / "lib" / "gstreamer-1.0").mkdir(parents=True)
    env = win_mirror.engine_env(engine, tmp_path / "state")
    assert env["DOUBLETAKE_GSTREAMER_ROOT"] == str(engine / "gstreamer")
    assert env["GST_PLUGIN_SYSTEM_PATH"] == str(engine / "gstreamer" / "lib" / "gstreamer-1.0")
    assert env["GST_REGISTRY"] == str(tmp_path / "state" / "gstreamer-registry.bin")
    assert "GST_PLUGIN_PATH" not in env


def test_requests_carry_the_token(tmp_path, engine):
    m = ScreenMirror(tmp_path / "state", tmp_path / "logs", engine)
    fake = FakeDoubletake(m.control, {"status": {"ok": True, "state": "idle"}})
    try:
        assert m.request("status") == {"ok": True, "state": "idle"}
        m.start("10.0.0.5", 7000)
        m.pin("10.0.0.5", "1234")
        m.stop("10.0.0.5")
    finally:
        fake.close()
    assert all(r["token"] == "secret" for r in fake.requests)
    assert fake.requests[-3:] == [
        {"cmd": "connect", "token": "secret", "target": "10.0.0.5", "port": 7000},
        {"cmd": "connect", "token": "secret", "target": "10.0.0.5", "pin": "1234"},
        {"cmd": "disconnect", "token": "secret", "target": "10.0.0.5"},
    ]


def test_refusals_become_errors(tmp_path, engine):
    m = ScreenMirror(tmp_path / "state", tmp_path / "logs", engine)
    fake = FakeDoubletake(m.control, {"connect": {"ok": False, "error": "device busy"}})
    try:
        with pytest.raises(MirrorError, match="device busy"):
            m.start("10.0.0.5")
    finally:
        fake.close()
    with pytest.raises(MirrorError, match="not reachable"):
        m.request("status", timeout=1)
    m.control.unlink()
    with pytest.raises(MirrorError, match="not running"):
        m.request("status")


def test_snapshot_lists_screens_but_not_speakers(tmp_path, engine):
    caps = [{"device_id": "AA:BB", "ip": "10.0.0.7", "screen": False, "audio": True},
            {"device_id": "CC:DD", "ip": "10.0.0.5", "screen": True, "audio": True}]
    m = ScreenMirror(tmp_path / "state", tmp_path / "logs", engine, airplay_caps=lambda: caps)
    fake = FakeDoubletake(m.control, {
        "devices": {"ok": True, "devices": [
            {"name": "Living room", "model": "AppleTV14,1", "ip": "10.0.0.5", "port": 7000,
             "device_id": "CC:DD"},
            {"name": "HomePod", "model": "AudioAccessory5,1", "ip": "10.0.0.7", "port": 7000,
             "device_id": "aa:bb"},
            {"name": "bedroom tv", "model": "", "ip": "10.0.0.9", "port": 7100,
             "device_id": "EE:FF"}]},
        "status": {"ok": True, "state": "pin_required", "streams": [
            {"device": "Living room", "device_ip": "10.0.0.5", "state": "pin_required",
             "credential_kind": "pin"}]},
    })
    try:
        snap = m.snapshot()
    finally:
        fake.close()
    assert snap == {
        "available": True, "error": "",
        "screens": [{"name": "bedroom tv", "ip": "10.0.0.9", "port": 7100, "model": ""},
                    {"name": "Living room", "ip": "10.0.0.5", "port": 7000,
                     "model": "AppleTV14,1"}],
        "streams": [{"device": "Living room", "ip": "10.0.0.5", "state": "pin_required",
                     "credential": "pin"}],
    }


def test_snapshot_without_engine(tmp_path):
    m = ScreenMirror(tmp_path, tmp_path, engine=None)
    m.engine = None
    assert m.snapshot() == {"available": False, "screens": [], "streams": [], "error": ""}
    m.shutdown()                        # nothing to stop


def test_engine_that_exits_reports_its_log(tmp_path):
    engine = tmp_path / "mirror"
    engine.mkdir()
    if os.name == "nt":
        pytest.skip("needs a shell script as the engine")
    exe = engine / win_mirror.EXE
    exe.write_text("#!/bin/sh\necho 'GStreamer d3d11screencapturesrc not found' >&2\nexit 1\n")
    exe.chmod(0o755)
    m = ScreenMirror(tmp_path / "state", tmp_path / "logs", engine)
    snap = m.snapshot()
    assert snap["available"] is True
    assert "doubletake exited: GStreamer d3d11screencapturesrc not found" in snap["error"]


def test_desktop_api_mirroring(tmp_path, monkeypatch):
    from multidrop import win_window, winshell
    monkeypatch.setattr(winshell, "autostart_enabled", lambda: False)
    monkeypatch.setattr(winshell, "sendto_enabled", lambda: False)

    class FakeMirror:
        available = True
        calls: list = []

        def snapshot(self):
            return {"available": True, "screens": [], "streams": [], "error": ""}

        def start(self, ip, port):
            self.calls.append(("start", ip, port))

        def pin(self, ip, code):
            self.calls.append(("pin", ip, code))

        def stop(self, ip):
            self.calls.append(("stop", ip))

    mirror = FakeMirror()
    api = win_window.DesktopApi(tmp_path, tmp_path, mirror=mirror)
    assert api.settings()["mirror"] is True
    assert win_window.DesktopApi(tmp_path, tmp_path).settings()["mirror"] is False
    assert api.mirror_to("10.0.0.5", "7000")["available"] is True
    api.mirror_pin("10.0.0.5", " 1234 ")
    api.stop_mirror("")
    assert mirror.calls == [("start", "10.0.0.5", 7000), ("pin", "10.0.0.5", "1234"),
                            ("stop", None)]


@pytest.mark.skipif(not os.environ.get("MULTIDROP_TEST_MIRROR_DIR"),
                    reason="MULTIDROP_TEST_MIRROR_DIR not set")
def test_real_engine_mirrors_to_test_receiver_with_pin(tmp_path):
    engine = Path(os.environ["MULTIDROP_TEST_MIRROR_DIR"]).resolve()
    receiver_exe = engine / ("doubletake-test-receiver" + (".exe" if os.name == "nt" else ""))
    hwaccel = os.environ.get("MULTIDROP_TEST_MIRROR_HWACCEL", "openh264")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    receiver = subprocess.Popen([str(receiver_exe), "-listen",
                                 f"127.0.0.1:{port}", "-auth", "pin", "-code", "4321",
                                 "-name", "Test TV"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # Synthetic video: CI machines have no screen to capture.
    m = ScreenMirror(tmp_path / "state", tmp_path / "logs", engine,
                     extra_args=("-test", "-no-audio", "-hwaccel", hwaccel,
                                 "-fps", "10", "-bitrate", "500"))
    try:
        m.start("127.0.0.1", port)
        deadline = time.time() + 20
        states = []
        while time.time() < deadline:
            streams = m.snapshot()["streams"]
            states.append(streams[0]["state"] if streams else None)
            if streams and streams[0]["state"] == "pin_required":
                m.pin("127.0.0.1", "4321")
            if streams and streams[0]["state"] == "streaming":
                break
            time.sleep(0.3)
        assert "pin_required" in states, states
        # The stream starts after the code. Whether video keeps flowing depends
        # on a working encoder (Fedora's openh264enc is a stub without Cisco's
        # library); then the state stays "streaming".
        log = (tmp_path / "logs" / "doubletake.log").read_text()
        assert "streaming to Test TV (127.0.0.1)" in log, log
        if os.environ.get("MULTIDROP_TEST_MIRROR_EXPECT_VIDEO"):
            time.sleep(3)               # a broken encoder ends the stream within this
            assert [s["state"] for s in m.snapshot()["streams"]] == ["streaming"], log
        if m.snapshot()["streams"]:
            m.stop("127.0.0.1")
    finally:
        m.shutdown()
        receiver.terminate()
        receiver.wait(5)
    assert not m.control.exists()       # no stale address for the next start
