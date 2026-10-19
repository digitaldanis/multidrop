"""multidropd on loopback: API (Unix socket and 127.0.0.1 with token),
offers answered through the API, sending uploaded files and folders, events,
and desktop notifications against a fake notification server on a private
session bus."""

import asyncio
import json
import os
import shutil
import subprocess

import aiohttp
import pytest

from conftest import WINDOWS, api_session, posix_only
from multidrop.backends.quickshare import QuickShareBackend, outbound
from multidrop.daemon import AlreadyRunning, run, socket_path


@pytest.fixture
def daemon_cfg(make_config, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "rt").mkdir(mode=0o700)
    cfg = make_config("pc")
    cfg.quickshare.bind = "127.0.0.1"
    cfg.daemon.web_port = 0
    cfg.daemon.ask_timeout = 10
    return cfg


@pytest.fixture
async def daemon(daemon_cfg):
    """Running daemon; yields (cfg, web_url_with_token, unix_session, stop)."""
    started = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.ensure_future(run(daemon_cfg, notifications=False,
                                     started=started, stop=stop))
    url = await asyncio.wait_for(asyncio.shield(started), 10)
    sess = api_session(daemon_cfg)
    try:
        yield daemon_cfg, url, sess
    finally:
        await sess.close()
        stop.set()
        await asyncio.wait_for(task, 10)
    assert not socket_path(daemon_cfg).exists()


async def get(sess, path):
    async with sess.get(path) as r:
        return r.status, await r.json()


async def post(sess, path, body):
    async with sess.post(path, json=body) as r:
        return r.status, await r.json()


async def wait_until(pred, timeout=8.0):
    for _ in range(int(timeout / 0.05)):
        result = await pred()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_status_and_single_instance(daemon):
    cfg, url, sess = daemon
    status, body = await get(sess, "/api/status")
    assert status == 200 and body["alias"] == "pc"
    assert set(body["protocols"]) == {"localsend", "quickshare"}
    assert (socket_path(cfg).parent / "url").read_text().strip() == url
    if not WINDOWS:     # no POSIX modes there
        assert oct(os.stat(socket_path(cfg).parent).st_mode & 0o777) == "0o700"
    with pytest.raises(AlreadyRunning):
        await run(cfg, notifications=False)


async def test_web_guard(daemon):
    cfg, url, _ = daemon
    base, token = url.split("/#t=")
    port = base.rsplit(":", 1)[1]
    async with aiohttp.ClientSession() as s:
        async with s.get(base + "/") as r:     # static page: no token needed
            assert r.status == 200 and "default-src 'self'" in r.headers["Content-Security-Policy"]
            assert token not in await r.text()
        async with s.get(base + "/api/status") as r:
            assert r.status == 401
        async with s.get(base + "/api/status", headers={"X-Multidrop-Token": "wrong"}) as r:
            assert r.status == 401
        ok = {"X-Multidrop-Token": token}
        async with s.get(base + "/api/status", headers=ok) as r:
            assert r.status == 200
        async with s.get(base + "/api/status", headers={**ok, "Host": f"evil.example:{port}"}) as r:
            assert r.status == 403                 # DNS rebinding
        async with s.post(base + "/api/offers/x", json={"accept": True},
                          headers={**ok, "Origin": "http://evil.example"}) as r:
            assert r.status == 403                 # cross-site request
        async with s.get(base + "/api/history?t=" + token) as r:
            assert r.status == 401                 # query token only for the event stream
        async with s.get(base + "/../../etc/passwd") as r:
            assert r.status in (403, 404)


async def test_offer_answered_through_api(daemon, tmp_path):
    cfg, _, sess = daemon
    port = (await get(sess, "/api/status"))[1]["protocols"]["quickshare"]["port"]
    src = tmp_path / "photo.png"
    src.write_bytes(os.urandom(50_000))
    events = []
    send = asyncio.ensure_future(outbound.send(
        "127.0.0.1", port, our_name="Pixel", entries=[(src, "photo.png")],
        on_event=lambda e, d: events.append((e, d))))

    async def pending():
        return (await get(sess, "/api/offers"))[1]
    offers = await wait_until(pending)
    assert offers[0]["from"] == "Pixel" and offers[0]["files"][0]["name"] == "photo.png"
    assert offers[0]["pin"] == next(d["pin"] for e, d in events if e == "pin")
    assert await post(sess, f"/api/offers/{offers[0]['id']}", {"accept": "yes"}) \
        == (400, {"error": "accept must be true or false"})
    assert (await post(sess, f"/api/offers/{offers[0]['id']}", {"accept": True}))[0] == 200
    await asyncio.wait_for(send, 10)
    assert (cfg.download_dir / "photo.png").read_bytes() == src.read_bytes()
    assert (await post(sess, f"/api/offers/{offers[0]['id']}", {"accept": True}))[0] == 404

    # Rejected through the API: the sender is told.
    send = asyncio.ensure_future(outbound.send(
        "127.0.0.1", port, our_name="Pixel", text="hello", on_event=lambda e, d: None))
    offers = await wait_until(pending)
    await post(sess, f"/api/offers/{offers[0]['id']}", {"accept": False})
    with pytest.raises(Exception, match="reject"):
        await asyncio.wait_for(send, 10)
    names = [e["event"] for e in (await get(sess, "/api/history"))[1]]
    assert names.count("offer") == 2 and "offer_accepted" in names and "offer_rejected" in names


async def test_send_upload_with_folder(daemon, make_config):
    cfg, _, sess = daemon
    rcfg = make_config("phone")
    rcfg.quickshare.bind = "127.0.0.1"
    rcfg.quickshare.mdns = False
    receiver = QuickShareBackend(rcfg)

    async def accept_all(offer):
        return {f.id for f in offer.files}
    await receiver.start(accept_all, lambda e, d: None)
    try:
        form = aiohttp.FormData()
        form.add_field("target", json.dumps({"protocol": "quickshare",
                                             "address": "127.0.0.1", "port": receiver.port}))
        for path, data in (("Holiday/Day 1/a.jpg", b"A" * 70_000), ("Holiday/b.txt", b"B"),
                           ("../../escape.txt", b"C")):
            form.add_field("path", path)
            form.add_field("file", data, filename=path.rsplit("/", 1)[-1])
        async with sess.post("/api/send", data=form) as r:
            assert r.status == 200, await r.text()
            job = (await r.json())["job"]

        async def finished():
            jobs = (await get(sess, "/api/jobs"))[1]
            return [j for j in jobs if j["id"] == job and j["state"] != "running"]
        assert (await wait_until(finished))[0]["state"] == "done"
        inbox = rcfg.download_dir
        assert (inbox / "Holiday" / "Day 1" / "a.jpg").read_bytes() == b"A" * 70_000
        assert (inbox / "Holiday" / "b.txt").read_bytes() == b"B"
        assert (inbox / "escape.txt").read_bytes() == b"C"     # ".." stripped
        assert not any((cfg.state_dir / "outgoing").iterdir())  # upload cleaned up
    finally:
        await receiver.stop()


async def test_send_errors(daemon, tmp_path):
    _, _, sess = daemon
    assert (await post(sess, "/api/send", {"target": {"key": "quickshare:nope"},
                                           "text": "x"}))[0] == 400
    assert (await post(sess, "/api/send", {"target": {"protocol": "airdrop", "address": "1.2.3.4"},
                                           "text": "x"}))[0] == 400
    assert (await post(sess, "/api/send", {"target": {"qr": True}, "paths": ["relative.txt"]}))[0] == 400
    assert (await post(sess, "/api/send", {"target": {"qr": True}}))[0] == 400
    for paths in ("/etc", [1], [None], {"a": 1}):      # malformed: 400, not a server error
        assert (await post(sess, "/api/send", {"target": {"qr": True}, "paths": paths}))[0] == 400
        assert (await post(sess, "/api/share", {"paths": paths}))[0] == 400
        assert (await post(sess, "/api/selection", {"paths": paths}))[0] == 400
    assert (await post(sess, "/api/share", {"paths": [str(tmp_path)], "minutes": [5]}))[0] == 400
    async with sess.post("/api/send", data=b"{oops",
                         headers={"Content-Type": "application/json"}) as r:
        assert r.status == 400
    for path in ("/api/devices?refresh=1&timeout=abc", "/api/devices?refresh=1&timeout=nan",
                 "/api/airplay?timeout=abc", "/api/events?since=abc", "/api/history?since=abc"):
        async with sess.get(path) as r:
            assert r.status == 400, path


async def test_event_stream_and_failed_job(daemon, tmp_path):
    _, url, sess = daemon
    base, token = url.split("/#t=")
    f = tmp_path / "x.txt"
    f.write_text("x")
    # Nothing listens on port 9 → the job fails, and the stream reports it.
    status, body = await post(sess, "/api/send", {
        "target": {"protocol": "quickshare", "address": "127.0.0.1", "port": 9},
        "paths": [str(f)]})
    assert status == 200
    seen = []
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/events?t={token}") as r:
            assert r.headers["Content-Type"] == "text/event-stream"
            async with asyncio.timeout(8):
                async for line in r.content:
                    if line.startswith(b"data: "):
                        seen.append(json.loads(line[6:]))
                        if seen[-1]["event"] == "job_failed":
                            break
    assert seen[0]["event"] == "started"
    assert seen[-1]["job"] == body["job"] and "connect" in seen[-1]["error"].lower()


async def test_accept_all_policy(daemon_cfg, tmp_path):
    daemon_cfg.daemon.accept = "all"
    started = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.ensure_future(run(daemon_cfg, notifications=False, started=started, stop=stop))
    await asyncio.wait_for(asyncio.shield(started), 10)
    try:
        sess = api_session(daemon_cfg)
        port = (await get(sess, "/api/status"))[1]["protocols"]["quickshare"]["port"]
        await sess.close()
        await outbound.send("127.0.0.1", port, our_name="Pixel", text="auto",
                            on_event=lambda e, d: None)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


# -- notifications on a private session bus --------------------------------

@pytest.fixture
def private_bus(tmp_path):
    if not shutil.which("dbus-daemon"):
        pytest.skip("dbus-daemon not installed")
    proc = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address",
                             f"--address=unix:path={tmp_path}/bus"],
                            stdout=subprocess.PIPE, text=True)
    address = proc.stdout.readline().strip()
    yield address
    proc.terminate()
    proc.wait(5)


async def test_notification_accepts_offer(private_bus, daemon_cfg, tmp_path):
    from fake_notifications import FakeNotifications
    fake = await FakeNotifications.start(private_bus)
    started = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.ensure_future(run(daemon_cfg, notifications=True, started=started,
                                     stop=stop, bus_address=private_bus))
    await asyncio.wait_for(asyncio.shield(started), 10)
    try:
        sess = api_session(daemon_cfg)
        port = (await get(sess, "/api/status"))[1]["protocols"]["quickshare"]["port"]
        await sess.close()
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"%PDF" * 100)
        send = asyncio.ensure_future(outbound.send(
            "127.0.0.1", port, our_name="Galaxy <b>S25</b>", entries=[(src, "doc.pdf")],
            on_event=lambda e, d: None))
        note = await wait_until(lambda: _first_with_actions(fake))
        nid, summary, body, actions = note
        assert "Galaxy <b>S25</b>" in summary and "doc.pdf" in body and "Code:" in body
        assert actions[:4] == ["accept", "Accept", "reject", "Decline"]
        await fake.invoke(nid, "accept")
        await asyncio.wait_for(send, 10)
        assert (daemon_cfg.download_dir / "doc.pdf").read_bytes() == src.read_bytes()
        await wait_until(lambda: _async(nid in fake.closed))           # offer note closed
        await wait_until(lambda: _async(any("Received" in n[1] for n in fake.notes)))
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
        await fake.stop()


async def _async(v):
    return v


async def _first_with_actions(fake):
    return next((n for n in fake.notes if n[3]), None)


async def test_selection_through_api(daemon, tmp_path):
    """Files handed over by the desktop (Explorer "Send to"), sent from the UI."""
    cfg, _, sess = daemon
    f = tmp_path / "Brief.txt"
    f.write_text("x")
    assert (await post(sess, "/api/selection", {"paths": ["relative.txt"]}))[0] == 400
    assert (await post(sess, "/api/selection", {"paths": []}))[0] == 400
    for _ in range(2):          # the same file twice is listed once
        status, body = await post(sess, "/api/selection", {"paths": [str(f)]})
        assert status == 200 and body["paths"] == [str(f)]
    assert (await get(sess, "/api/selection"))[1]["paths"] == [str(f)]
    async with sess.delete("/api/selection") as r:
        assert r.status == 200
    assert (await get(sess, "/api/selection"))[1]["paths"] == []
    events = [e for e in (await get(sess, "/api/history"))[1] if e["event"] == "selection"]
    assert [e["count"] for e in events] == [1, 1, 0]


async def test_history_since(daemon):
    """?since=SEQ returns only newer events (the Windows app polls with it)."""
    _, _, sess = daemon
    async with sess.post("/api/show") as r:
        assert r.status == 200
    events = (await get(sess, "/api/history"))[1]
    last = events[-1]["seq"]
    assert (await get(sess, f"/api/history?since={last}"))[1] == []
    newer = (await get(sess, f"/api/history?since={last - 1}"))[1]
    assert [e["seq"] for e in newer] == [last]
    assert (await get(sess, "/api/history?since=x"))[0] == 400


async def test_share_through_api(daemon):
    cfg, _, sess = daemon
    form = aiohttp.FormData()
    form.add_field("minutes", "5")
    form.add_field("path", "Folder/f.txt")
    form.add_field("file", b"inhalt", filename="f.txt")
    async with sess.post("/api/share", data=form) as r:
        assert r.status == 200, await r.text()
        info = await r.json()
    assert info["running"] and len(info["pin"]) == 4 and info["svg"].startswith("<svg")
    assert info["files"] == ["Folder/f.txt"]
    assert info["url"].startswith("http://127.0.0.1:")
    prep = info["url"].replace("/?k=", "/api/localsend/v2/prepare-download?k=") + f"&pin={info['pin']}"
    async with aiohttp.ClientSession() as http:
        async with http.post(prep) as r:
            assert r.status == 200
    names = [e["event"] for e in (await get(sess, "/api/history"))[1]]
    assert "share_started" in names and "share_visit" in names
    started = next(e for e in (await get(sess, "/api/history"))[1] if e["event"] == "share_started")
    assert "pin" not in started                      # PIN not in the event log
    assert (await post(sess, "/api/share", {"paths": ["rel"]}))[0] == 400
    assert (await post(sess, "/api/share", {"paths": [str(cfg.download_dir)], "pin": "12"}))[0] == 400
    async with sess.delete("/api/share") as r:
        assert r.status == 200
    assert (await get(sess, "/api/share"))[1] is None
    assert not any((cfg.state_dir / "outgoing").iterdir())


@posix_only
async def test_shutdown_keeps_a_replaced_socket(daemon_cfg):
    """A daemon that lost its socket to another instance must not delete the
    other one's socket and URL file on exit (it would run unreachable)."""
    started = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.ensure_future(run(daemon_cfg, notifications=False, started=started, stop=stop))
    await asyncio.wait_for(asyncio.shield(started), 10)
    sock = socket_path(daemon_cfg)
    url = sock.parent / "url"
    sock.unlink()
    sock.write_text("other daemon")         # new inode, as a second daemon's would be
    url.write_text("http://127.0.0.1:1/#t=other\n")
    stop.set()
    await asyncio.wait_for(task, 10)
    assert sock.read_text() == "other daemon"
    assert "other" in url.read_text()
    sock.unlink()
