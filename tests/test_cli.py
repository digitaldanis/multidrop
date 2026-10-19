"""End to end through the real `multidrop` command: two processes on loopback.

Discovery stays on 127.0.0.1 (LocalSend multicast and Quick Share mDNS) and
Bluetooth is off, so nothing leaves the machine.
"""

import os
import socket
import subprocess
import sys
import time

from conftest import free_port

CMD = [sys.executable, "-m", "multidrop"]


def _common(tmp_path, name, port, qs_port, mc_port):
    # Own runtime dir: a multidropd running on this machine must not be used.
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / name / "data"),
           "MULTIDROP_CONFIG": str(tmp_path / "none.toml"),
           "XDG_RUNTIME_DIR": str(tmp_path / "rt")}
    args = ["--alias", name, "--port", str(port), "--multicast-port", str(mc_port),
            "--multicast-interface", "127.0.0.1", "--qs-port", str(qs_port),
            "--mdns-address", "127.0.0.1", "--no-ble"]
    return env, args


def _wait_for_port(port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(port)


def _run(cmd, env):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    print(r.stdout, r.stderr)
    return r


def test_cli_both_protocols(tmp_path):
    mc_port = free_port(socket.SOCK_DGRAM)
    b_port, b_qs = free_port(), free_port()
    a_port, a_qs = free_port(), free_port()
    inbox = tmp_path / "inbox"
    src = tmp_path / "Bericht.pdf"
    src.write_bytes(os.urandom(300_000))
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(os.urandom(700_000))

    env_b, args_b = _common(tmp_path, "bob", b_port, b_qs, mc_port)
    receiver = subprocess.Popen(
        CMD + args_b + ["receive", "--accept", "all", "--dir", str(inbox)],
        env=env_b, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_for_port(b_port)
        _wait_for_port(b_qs)
        env_a, args_a = _common(tmp_path, "alice", a_port, a_qs, mc_port)

        devices = _run(CMD + args_a + ["devices", "--timeout", "2"], env_a)
        assert devices.returncode == 0
        lines = devices.stdout.splitlines()
        assert lines[0].split() == ["PROTOCOL", "NAME", "ADDRESS", "TYPE"]
        assert any(l.split()[:3] == ["localsend", "bob", f"127.0.0.1:{b_port}"] for l in lines)
        assert any(l.split()[:3] == ["quickshare", "bob", f"127.0.0.1:{b_qs}"] for l in lines)

        # By name: bob is found over both protocols, Quick Share is preferred
        # and its code is printed.
        sent = _run(CMD + args_a + ["send", str(src), "--to", "bob", "--timeout", "2"], env_a)
        assert sent.returncode == 0
        assert "[quickshare]" in sent.stdout and "Quick Share code" in sent.stdout

        # Forced LocalSend, by name.
        sent = _run(CMD + args_a + ["send", str(photo), "--to", "bob", "--via", "localsend",
                                    "--timeout", "2"], env_a)
        assert sent.returncode == 0
        assert "[localsend]" in sent.stdout

        # Quick Share by address, text only.
        sent = _run(CMD + args_a + ["send", "--text", "https://example.org/x",
                                    "--to", f"127.0.0.1:{b_qs}", "--via", "quickshare"], env_a)
        assert sent.returncode == 0
        time.sleep(0.5)
    finally:
        receiver.terminate()
        out, _ = receiver.communicate(timeout=20)
        print(out)
    if os.name != "nt":        # terminate() is TerminateProcess there: exit code 1
        assert receiver.returncode == 0
    assert (inbox / "Bericht.pdf").read_bytes() == src.read_bytes()
    assert (inbox / "foto.jpg").read_bytes() == photo.read_bytes()
    assert "url from alice:\nhttps://example.org/x" in out
    assert out.count("Quick Share code") == 2


def test_cli_send_errors(tmp_path):
    env, args = _common(tmp_path, "alice", free_port(), free_port(),
                        free_port(socket.SOCK_DGRAM))
    r = _run(CMD + args + ["send", "--to", "127.0.0.1:1", "--via", "quickshare",
                           "--text", "x"], env)
    assert r.returncode == 1 and "cannot connect" in r.stderr
    r = _run(CMD + args + ["send"], env)
    assert r.returncode == 1 and "nothing to send" in r.stderr


def test_cli_share(tmp_path):
    import urllib.request
    cfg = tmp_path / "c.toml"
    cfg.write_text('[localsend]\nshare_port = 0\nshare_bind = "127.0.0.1"\n')
    f = tmp_path / "Letter.txt"
    f.write_text("hello")
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"),
           "XDG_RUNTIME_DIR": str(tmp_path / "rt")}
    proc = subprocess.Popen(CMD + ["--config", str(cfg), "share", str(f), "--minutes", "0.05",
                                   "--no-pin", "--address", "127.0.0.1"],
                            env=env, stdout=subprocess.PIPE, text=True)
    try:
        url = None
        for line in proc.stdout:
            if line.startswith("Open on the phone"):
                url = line.split(": ", 1)[1].strip()
                break
        page = urllib.request.urlopen(url, timeout=5).read().decode()
        assert "Letter.txt" in page
        out = proc.stdout.read()      # ends by itself after 3 s
        assert proc.wait(15) == 0
    finally:
        proc.kill()
    assert "share stopped (expired" in out and "opened by 127.0.0.1" in out
