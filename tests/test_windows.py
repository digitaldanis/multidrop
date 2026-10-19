"""Windows code paths (most of them also run on Linux)."""

import socket
import subprocess
import sys

import pytest

from multidrop import config, daemon


def test_runtime_dir_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WINDOWS", True)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config.runtime_dir() == tmp_path / "multidrop" / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    assert config.runtime_dir() == tmp_path / "xdg" / "multidrop"


def test_single_instance_by_web_port(tmp_path):
    url_file = tmp_path / "url"
    daemon._check_not_running_tcp(url_file)            # no file: fine
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        url_file.write_text(f"http://127.0.0.1:{port}/#t=abc\n")
        with pytest.raises(daemon.AlreadyRunning):
            daemon._check_not_running_tcp(url_file)
    daemon._check_not_running_tcp(url_file)            # port closed: stale file
    assert not url_file.exists()


def test_windows_entry_selftest(tmp_path, monkeypatch, make_config):
    """The .exe's --selftest path: service thread, API over HTTP with token."""
    from multidrop import windows
    cfg = make_config("winpc")
    cfg.daemon.web_port = 0
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "rt").mkdir(mode=0o700)
    service = windows.Service(cfg)
    url = service.start()
    try:
        assert url.startswith("http://127.0.0.1:")
        assert windows._selftest(service) == 0
    finally:
        service.stop()


async def test_api_without_af_unix(monkeypatch, make_config, tmp_path):
    """Windows' socket module has no AF_UNIX (seen in CI: HTTP 500)."""
    import socket as socket_mod

    monkeypatch.delattr(socket_mod, "AF_UNIX", raising=False)
    from multidrop.daemon import api
    req = type("R", (), {"transport": type("T", (), {"get_extra_info": lambda self, k: object()})()})()
    assert api._via_unix_socket(req) is False


async def test_cli_client_over_tcp(monkeypatch, make_config, tmp_path):
    """On Windows the CLI reaches multidropd over 127.0.0.1 with the token."""
    import asyncio

    from multidrop.daemon import client, run, socket_path
    monkeypatch.setattr(client, "WINDOWS", True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    cfg = make_config("winpc")
    cfg.daemon.web_port = 0
    assert await client.DaemonClient.connect(socket_path(cfg)) is None
    started = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.ensure_future(run(cfg, notifications=False, started=started, stop=stop))
    try:
        await asyncio.wait_for(asyncio.shield(started), 10)
        async with await client.DaemonClient.connect(socket_path(cfg)) as c:
            assert (await c.get("/api/status"))["alias"] == "winpc"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


def test_cli_output_is_utf8(monkeypatch):
    """The terminal QR code must not crash on a cp1252 pipe."""
    import io
    import sys

    from multidrop import cli
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    cli._utf8_output()
    print("\u2580\u2584\u2588", end="")
    out.flush()
    assert out.buffer.getvalue() == "\u2580\u2584\u2588".encode()


def test_second_start_hands_over_files(tmp_path, monkeypatch, make_config):
    """`MultiDrop.exe FILE` (Explorer's "Send to") while MultiDrop runs."""
    from multidrop import windows
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("MULTIDROP_CONFIG", str(tmp_path / "none.toml"))
    opened = []
    monkeypatch.setattr(windows.webbrowser, "open", opened.append)
    cfg = make_config("winpc")
    cfg.daemon.web_port = 0
    running = windows.Service(cfg)
    url = running.start()
    try:
        f = tmp_path / "Foto.jpg"
        f.write_bytes(b"jpg")
        assert windows.main([str(f)]) == 0
        assert running.api("/api/selection")["paths"] == [str(f.resolve())]
        assert opened == []                 # the running app shows its own window
        assert windows.main([]) == 0        # Start menu: just bring it up
        assert running.api("/api/history")[-1]["event"] == "show_ui"
        assert windows.main(["--browser"]) == 0
        assert opened == [url]
    finally:
        running.stop()


def test_watch_events_brings_the_window_up(tmp_path, monkeypatch, make_config):
    """Offers, handed-over files and a second start show the window; old
    events from before the start do not."""
    import threading
    import time

    from multidrop import windows
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    cfg = make_config("winpc")
    cfg.daemon.web_port = 0
    service = windows.Service(cfg)
    service.start()
    try:
        service.api("/api/show", {})                    # before the watcher: ignored
        shown, notes = [], []
        threading.Thread(target=windows._watch_events,
                         args=(service, lambda *a: notes.append(a), lambda: shown.append(1)),
                         daemon=True).start()
        time.sleep(1.5)
        assert shown == []
        f = tmp_path / "a.txt"
        f.write_text("x")
        service.api("/api/selection", {"paths": [str(f)]})
        service.api("/api/show", {})
        service.api("/api/selection", method="DELETE")   # count 0: nothing to show
        for _ in range(50):
            if len(shown) >= 2:
                break
            time.sleep(0.1)
        time.sleep(1.2)
        assert len(shown) == 2 and notes == []
    finally:
        service.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry and shortcuts")
def test_autostart_and_send_to(tmp_path, monkeypatch):
    import winreg

    from multidrop import winshell
    monkeypatch.setattr(winshell, "RUN_KEY", r"Software\MultiDropTest\Run")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    try:
        assert not winshell.autostart_enabled()
        winshell.set_autostart(True)
        assert winshell.autostart_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, winshell.RUN_KEY) as key:
            command = winreg.QueryValueEx(key, "MultiDrop")[0]
        assert "multidrop.windows" in command and command.endswith("--no-browser")
        winshell.set_autostart(False)
        winshell.set_autostart(False)       # already off: no error
        assert not winshell.autostart_enabled()
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, winshell.RUN_KEY)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\MultiDropTest")

    winshell.set_sendto(True)
    assert winshell.sendto_link() == tmp_path / "Microsoft/Windows/SendTo/MultiDrop.lnk"
    assert winshell.sendto_enabled() and winshell.sendto_link().stat().st_size > 0
    winshell.set_sendto(False)
    assert not winshell.sendto_enabled()


def test_desktop_api_settings_and_welcome(tmp_path, monkeypatch):
    """What the app window's page can call (win_window.DesktopApi)."""
    from multidrop import win_window, winshell
    state = {"autostart": False, "sendto": False}
    monkeypatch.setattr(winshell, "autostart_enabled", lambda: state["autostart"])
    monkeypatch.setattr(winshell, "sendto_enabled", lambda: state["sendto"])
    monkeypatch.setattr(winshell, "set_autostart", lambda on: state.update(autostart=on))
    monkeypatch.setattr(winshell, "set_sendto", lambda on: state.update(sendto=on))
    api = win_window.DesktopApi(tmp_path / "state", tmp_path / "in")
    assert api.settings() == {"autostart": False, "sendto": False, "welcome": True,
                              "audio": False, "mirror": False}
    assert api.set_autostart(True)["autostart"] and api.set_sendto(True)["sendto"]
    api.welcome_done()
    assert api.settings()["welcome"] is False
    assert win_window.DesktopApi(tmp_path / "state", tmp_path / "in").settings()["welcome"] is False


def test_desktop_api_speakers(tmp_path):
    from multidrop import raop, win_window

    class FakeAudio:
        receivers = [raop.Receiver("Küche", "10.0.0.2", 7000, {0}, False),
                     raop.Receiver("HomePod", "10.0.0.3", 7000, {4}, False)]
        current, volume, played = None, 0.75, []

        def play(self, r):
            self.current = r

        def stop(self):
            self.current = None

        def set_volume(self, level):
            self.volume = level

        def scan(self):
            pass

    api = win_window.DesktopApi(tmp_path, tmp_path, FakeAudio())
    assert api.speakers()["list"] == [{"name": "Küche", "supported": True},
                                      {"name": "HomePod", "supported": False}]
    assert api.play_on("Küche")["current"] == "Küche"
    assert api.set_volume(7)["volume"] == 1.0
    assert api.stop_audio()["current"] is None
    with pytest.raises(ValueError, match="search again"):
        api.play_on("Gone")


def test_firewall_rule_any_network_local_devices_only():
    from multidrop import winshell
    *deletes, tcp, udp = winshell.firewall_commands([53320, 53317, 53317], [5353])
    assert [d[-1] for d in deletes] == ["name=MultiDrop (local network)", "name=MultiDrop"]
    for rule in (tcp, udp):
        # Most Wi-Fis count as "public" on Windows; the subnet keeps the internet out.
        assert "profile=any" in rule and "remoteip=localsubnet" in rule and "dir=in" in rule
    assert "protocol=TCP" in tcp and "localport=53317,53320" in tcp
    assert "protocol=UDP" in udp and "localport=5353" in udp


def test_allow_network_ignores_missing_old_rules(monkeypatch):
    from multidrop import winshell
    ran = []

    def run(args):
        ran.append(args)
        return subprocess.CompletedProcess(args, 1 if args[3] == "delete" else 0, "", "")
    monkeypatch.setattr(winshell, "_run", run)
    assert winshell.allow_network([53317], [5353]) == 0
    assert [a[3] for a in ran] == ["delete", "delete", "add", "add"]
    monkeypatch.setattr(winshell, "_run", lambda args: subprocess.CompletedProcess(args, 1))
    assert winshell.allow_network([53317], [5353]) == 1


def test_elevated_start_allows_configured_ports(monkeypatch, tmp_path):
    from multidrop import windows, winshell
    calls = []
    monkeypatch.setattr(winshell, "allow_network", lambda *a: calls.append(a) or 0)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[localsend]\nport = 40001\nshare_port = 40002\n[quickshare]\nport = 40003\n")
    assert windows.main(["--allow-network", "--config", str(cfg)]) == 0
    assert calls == [([40001, 40003, 40002], [53317, 5353])]


def test_network_access_asks_only_once(monkeypatch, make_config):
    from multidrop import windows, winshell
    requests = []
    monkeypatch.setattr(winshell, "firewall_rule_present", lambda: False)
    monkeypatch.setattr(winshell, "request_network_access",
                        lambda args: requests.append(args) or False)
    access = windows.NetworkAccess(make_config("winpc"), ["--config", "x.toml"])
    access.ask_once()
    access.ask_once()
    assert requests == [["--config", "x.toml"]]


def test_webview2_failure_closes_the_empty_window(monkeypatch):
    """pywebview only logs a failed WebView2 start; MultiDrop must not stay a white window."""
    import logging

    from multidrop import win_window
    closed = []
    handler = win_window._WebView2Failure(lambda: closed.append(True))
    log = logging.getLogger("pywebview")
    log.addHandler(handler)
    try:
        log.error("something else went wrong")
        assert not closed
        log.error("WebView2 initialization failed with exception:\n (0x800700AA): in use")
        assert closed == [True]
    finally:
        log.removeHandler(handler)
