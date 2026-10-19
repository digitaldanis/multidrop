"""`multidrop` as a client of a running multidropd (real processes, loopback).

The daemon "pc" holds the ports; a direct-mode `multidrop receive` "bob" in
another runtime directory is the peer. Bluetooth is off, discovery stays on
127.0.0.1.
"""

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import free_port
from multidrop import cli_daemon
from multidrop.backends.quickshare import outbound
from multidrop.daemon import run, socket_path
from multidrop.daemon.client import DaemonClient

CLI = [sys.executable, "-m", "multidrop"]
DAEMON = [sys.executable, "-c", "import sys; from multidrop.daemon import main; sys.exit(main())"]


def _wait_for(pred, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise TimeoutError


def _port_open(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _run(cmd, env, **kw):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, **kw)
    print(r.stdout, r.stderr)
    return r


@pytest.fixture
def setup(tmp_path):
    mc = free_port(socket.SOCK_DGRAM)
    pc_ls, pc_qs, bob_ls, bob_qs = free_port(), free_port(), free_port(), free_port()
    cfg = tmp_path / "pc.toml"
    cfg.write_text(f'''alias = "pc"
download_dir = "{(tmp_path / 'pc-in').as_posix()}"
state_dir = "{(tmp_path / 'pc-state').as_posix()}"
[localsend]
port = {pc_ls}
bind = "127.0.0.1"
multicast_port = {mc}
multicast_interface = "127.0.0.1"
share_port = 0
share_bind = "127.0.0.1"
[quickshare]
port = {pc_qs}
bind = "127.0.0.1"
mdns_addresses = ["127.0.0.1"]
ble = false
[daemon]
web_port = 0
notifications = false
''')
    (tmp_path / "rt-pc").mkdir(mode=0o700)
    (tmp_path / "rt-bob").mkdir(mode=0o700)
    pc_env = {**os.environ, "MULTIDROP_CONFIG": str(cfg), "XDG_RUNTIME_DIR": str(tmp_path / "rt-pc"),
              "XDG_STATE_HOME": str(tmp_path / "pc-xdgstate")}
    bob_env = {**os.environ, "MULTIDROP_CONFIG": str(tmp_path / "none.toml"),
               "XDG_RUNTIME_DIR": str(tmp_path / "rt-bob"),
               "XDG_DATA_HOME": str(tmp_path / "bob-data")}
    bob_args = ["--alias", "bob", "--port", str(bob_ls), "--multicast-port", str(mc),
                "--multicast-interface", "127.0.0.1", "--qs-port", str(bob_qs),
                "--mdns-address", "127.0.0.1", "--no-ble"]
    daemon = subprocess.Popen(DAEMON, env=pc_env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    bob = subprocess.Popen(CLI + bob_args + ["receive", "--accept", "all",
                                             "--dir", str(tmp_path / "bob-in")],
                           env=bob_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    try:
        url = tmp_path / "rt-pc" / "multidrop" / "url"     # written once the API is up
        _wait_for(lambda: url.exists() and _port_open(pc_qs))
        _wait_for(lambda: _port_open(bob_ls) and _port_open(bob_qs))
        yield dict(tmp=tmp_path, pc_env=pc_env, bob_env=bob_env, bob_args=bob_args,
                   pc_ls=pc_ls, pc_qs=pc_qs, bob_qs=bob_qs)
    finally:
        for p in (bob, daemon):
            p.terminate()
            print(p.communicate(timeout=20)[0])


def test_cli_through_daemon(setup):
    s, tmp = setup, setup["tmp"]
    env = s["pc_env"]

    r = _run(CLI + ["devices", "--timeout", "2"], env)
    assert r.returncode == 0 and r.stderr == ""
    lines = r.stdout.splitlines()
    assert lines[0].split() == ["PROTOCOL", "NAME", "ADDRESS", "TYPE"]
    assert any(l.split()[:2] == ["quickshare", "bob"] for l in lines)
    assert any(l.split()[:2] == ["localsend", "bob"] for l in lines)

    # By name through the daemon: Quick Share preferred, code printed.
    doc = tmp / "Bericht.pdf"
    doc.write_bytes(os.urandom(200_000))
    r = _run(CLI + ["send", str(doc), "--to", "bob", "--timeout", "2"], env)
    assert r.returncode == 0
    assert "(via multidropd)" in r.stdout and "[quickshare]" in r.stdout
    assert "Quick Share code" in r.stdout and "sent Bericht.pdf" in r.stdout
    _wait_for(lambda: (tmp / "bob-in" / "Bericht.pdf").exists())
    assert (tmp / "bob-in" / "Bericht.pdf").read_bytes() == doc.read_bytes()

    # By address, without --via: tried as LocalSend first.
    r = _run(CLI + ["send", "--text", "hello bob", "--to", "127.0.0.1:" + str(s["bob_qs"]),
                    "--via", "quickshare"], env)
    assert r.returncode == 0 and "(via multidropd)" in r.stdout

    # Errors come back from the daemon's job.
    r = _run(CLI + ["send", "--to", "127.0.0.1:1", "--via", "quickshare", "--text", "x"], env)
    assert r.returncode == 1 and "cannot connect" in r.stderr

    # Direct-mode options are reported as ignored.
    r = _run(CLI + ["--qs-port", "1", "devices", "--timeout", "0.5", "--via", "localsend"], env)
    assert r.returncode == 0 and "ignored: --qs-port" in r.stderr

    # receive: events of the daemon, accepting in the CLI; ends after one transfer.
    recv = subprocess.Popen(CLI + ["receive", "--accept", "all", "--once"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out = []
        for line in recv.stdout:
            out.append(line)
            if line.startswith("saving to"):
                break
        photo = tmp / "foto.jpg"
        photo.write_bytes(os.urandom(300_000))
        r = _run(CLI + s["bob_args"] + ["send", str(photo), "--to",
                                        f"127.0.0.1:{s['pc_qs']}", "--via", "quickshare"],
                 s["bob_env"])
        assert r.returncode == 0
        rest, _ = recv.communicate(timeout=30)
        out = "".join(out) + rest
    finally:
        recv.kill()
    print(out)
    assert recv.returncode == 0
    assert "multidropd is running: receiving through it as 'pc'" in out
    assert "accepting 1 item(s) from bob" in out and "received foto.jpg" in out
    assert (tmp / "pc-in" / "foto.jpg").read_bytes() == photo.read_bytes()

    # share: the daemon serves the download page.
    f = tmp / "Letter.txt"
    f.write_text("hello")
    proc = subprocess.Popen(CLI + ["share", str(f), "--minutes", "0.05", "--no-pin",
                                   "--address", "127.0.0.1"],
                            env=env, stdout=subprocess.PIPE, text=True)
    try:
        url = None
        for line in proc.stdout:
            if line.startswith("Open on the phone"):
                url = line.split(": ", 1)[1].strip()
                break
        assert "Letter.txt" in urllib.request.urlopen(url, timeout=5).read().decode()
        out = proc.stdout.read()
        assert proc.wait(20) == 0
    finally:
        proc.kill()
    assert "shared by multidropd" in out
    assert "share stopped (expired" in out and "opened by 127.0.0.1" in out

    # --direct bypasses the daemon (here: its ports are taken, so this fails).
    r = _run(CLI + ["--direct", "--port", str(s["pc_ls"]), "receive", "--accept", "all",
                    "--via", "localsend"], env)
    assert r.returncode == 1 and "(via multidropd)" not in r.stdout


@pytest.fixture
def daemon_cfg(make_config, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "rt").mkdir(mode=0o700)
    cfg = make_config("pc")
    cfg.quickshare.bind = "127.0.0.1"
    cfg.daemon.web_port = 0
    return cfg


@pytest.mark.parametrize("answer", ["j", "n"])
async def test_receive_asks_in_cli(daemon_cfg, tmp_path, monkeypatch, capsys, answer):
    """Offer from the daemon, answered at the CLI prompt."""
    started = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.ensure_future(run(daemon_cfg, notifications=False, started=started, stop=stop))
    await asyncio.wait_for(asyncio.shield(started), 10)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    args = argparse.Namespace(command="receive", accept="ask", via=None, once=True,
                              pin=None, dir=None)
    client = await DaemonClient.connect(socket_path(daemon_cfg))
    src = tmp_path / "photo.png"
    src.write_bytes(os.urandom(40_000))
    try:
        receiving = asyncio.ensure_future(cli_daemon.receive(args, client))
        await asyncio.sleep(0.5)
        port = (await client.get("/api/status"))["protocols"]["quickshare"]["port"]
        sending = outbound.send("127.0.0.1", port, our_name="Pixel",
                                entries=[(src, "photo.png")], on_event=lambda e, d: None)
        if answer == "j":
            await asyncio.wait_for(sending, 15)
            assert await asyncio.wait_for(receiving, 15) == 0
        else:
            with pytest.raises(Exception):
                await asyncio.wait_for(sending, 15)
            receiving.cancel()
    finally:
        await client.close()
        stop.set()
        await asyncio.wait_for(task, 10)
    out = capsys.readouterr().out
    assert "Pixel (127.0.0.1, quickshare) wants to send 1 item(s)" in out
    saved = daemon_cfg.download_dir / "photo.png"
    assert saved.exists() == (answer == "j")
