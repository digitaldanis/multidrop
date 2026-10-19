"""multidropd: accepting single files of an offer, LocalSend PIN when sending,
history kept across restarts (all on loopback)."""

import asyncio
import json
import os

import pytest

from conftest import WINDOWS, api_session
from multidrop.backends.localsend import LocalSendBackend
from multidrop.backends.quickshare import outbound
from multidrop.cli import _choose
from multidrop.core import IncomingOffer, OfferFile
from multidrop.daemon import run
from multidrop.daemon.service import HISTORY, Daemon, PendingOffer

from test_daemon import daemon, daemon_cfg, get, post, wait_until  # noqa: F401 (fixtures)


async def _pending(sess):
    return (await get(sess, "/api/offers"))[1]


async def test_localsend_single_files(daemon, make_config, tmp_path):
    """LocalSend lets the receiver choose: only the chosen files are sent."""
    cfg, _, sess = daemon
    phone = LocalSendBackend(make_config("phone"))
    files = []
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        f = tmp_path / name
        f.write_bytes(os.urandom(20_000))
        files.append(f)
    try:
        device = await phone.resolve("127.0.0.1", cfg.localsend.port)
        sending = asyncio.ensure_future(phone.send(device, files))
        offer = (await wait_until(lambda: _pending(sess)))[0]
        assert offer["partial"] == "native" and len(offer["files"]) == 3
        ids = {f["name"]: f["id"] for f in offer["files"]}
        assert await post(sess, f"/api/offers/{offer['id']}",
                          {"accept": True, "files": ["nope"]}) == (400, {"error": "unknown file id"})
        status, _ = await post(sess, f"/api/offers/{offer['id']}",
                               {"accept": True, "files": [ids["a.jpg"], ids["c.jpg"]]})
        assert status == 200
        await asyncio.wait_for(sending, 15)
    finally:
        await phone.stop()
    got = sorted(p.name for p in cfg.download_dir.iterdir())
    assert got == ["a.jpg", "c.jpg"]
    accepted = next(e for e in (await get(sess, "/api/history"))[1]
                    if e["event"] == "offer_accepted")
    assert accepted["partial"] is True and accepted["offer"]["offered"] == 3
    assert [f["name"] for f in accepted["offer"]["files"]] == ["a.jpg", "c.jpg"]


async def test_quickshare_single_files_discarded(daemon, tmp_path):
    """Quick Share accepts only whole offers: unchosen files arrive but are
    not saved."""
    cfg, _, sess = daemon
    port = (await get(sess, "/api/status"))[1]["protocols"]["quickshare"]["port"]
    a, b = tmp_path / "eins.txt", tmp_path / "zwei.txt"
    a.write_bytes(b"1" * 5000)
    b.write_bytes(b"2" * 7000)
    sending = asyncio.ensure_future(outbound.send(
        "127.0.0.1", port, our_name="Pixel", entries=[(a, "eins.txt"), (b, "zwei.txt")],
        on_event=lambda e, d: None))
    offer = (await wait_until(lambda: _pending(sess)))[0]
    assert offer["partial"] == "discard"
    keep = next(f["id"] for f in offer["files"] if f["name"] == "zwei.txt")
    assert (await post(sess, f"/api/offers/{offer['id']}",
                       {"accept": True, "files": [keep]}))[0] == 200
    await asyncio.wait_for(sending, 15)
    await wait_until(lambda: _async((cfg.download_dir / "zwei.txt").exists()))
    assert sorted(p.name for p in cfg.download_dir.iterdir()) == ["zwei.txt"]


async def _async(v):
    return v


async def test_all_or_nothing_protocol(make_config):
    """AirDrop (partial = "none"): a subset is refused, all or none works."""
    d = Daemon(make_config("pc"), backends=[])
    files = [OfferFile("0", "a", 1), OfferFile("1", "b", 1)]
    fut = asyncio.get_running_loop().create_future()
    d.offers["x"] = PendingOffer("x", "airdrop", IncomingOffer("airdrop", "iPhone", "fe80::1", files),
                                 fut, partial="none")
    with pytest.raises(ValueError, match="only all files or none"):
        d.answer("x", True, ["0"])
    assert not fut.done()
    assert d.answer("x", True, ["0", "1"]) and fut.result() == {"0", "1"}


async def test_localsend_pin_when_sending(daemon, make_config, tmp_path):
    """A LocalSend receiver with a PIN: the job fails with pin_required,
    the same send with the PIN works."""
    cfg, _, sess = daemon
    rcfg = make_config("tablet", pin="4711")
    receiver = LocalSendBackend(rcfg)

    async def accept_all(offer):
        return {f.id for f in offer.files}
    await receiver.start(accept_all, lambda e, d: None)
    f = tmp_path / "plan.pdf"
    f.write_bytes(b"%PDF-1.7" * 500)
    target = {"protocol": "localsend", "address": "127.0.0.1", "port": rcfg.localsend.port}

    async def job_end(job):
        async def ended():
            return [e for e in (await get(sess, "/api/history"))[1]
                    if e.get("job") == job and e["event"] in ("job_done", "job_failed")]
        return (await wait_until(ended, 15))[0]
    try:
        _, body = await post(sess, "/api/send", {"target": target, "paths": [str(f)]})
        failed = await job_end(body["job"])
        assert failed["event"] == "job_failed" and failed["pin_required"] is True
        _, body = await post(sess, "/api/send", {"target": target, "paths": [str(f)],
                                                 "pin": "0000"})
        assert (await job_end(body["job"]))["pin_required"] is True        # wrong PIN
        _, body = await post(sess, "/api/send", {"target": target, "paths": [str(f)],
                                                 "pin": "4711"})
        assert (await job_end(body["job"]))["event"] == "job_done"
    finally:
        await receiver.stop()
    assert (rcfg.download_dir / "plan.pdf").read_bytes() == f.read_bytes()


async def test_history_survives_restart(daemon_cfg, tmp_path):
    async def start():
        started = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        task = asyncio.ensure_future(run(daemon_cfg, notifications=False, started=started,
                                         stop=stop))
        await asyncio.wait_for(asyncio.shield(started), 10)
        return stop, task

    stop, task = await start()
    sess = api_session(daemon_cfg)
    try:
        port = (await get(sess, "/api/status"))[1]["protocols"]["quickshare"]["port"]
        sending = asyncio.ensure_future(outbound.send(
            "127.0.0.1", port, our_name="Pixel", text="vor dem Neustart",
            on_event=lambda e, d: None))
        offer = (await wait_until(lambda: _pending(sess)))[0]
        await post(sess, f"/api/offers/{offer['id']}", {"accept": True})
        await asyncio.wait_for(sending, 10)
        await wait_until(lambda: _has(sess, "message"))
        g = tmp_path / "g.txt"
        g.write_text("x")
        _, share = await post(sess, "/api/share", {"paths": [str(g)], "no_pin": True})
        async with sess.delete("/api/share"):
            pass
        before = (await get(sess, "/api/history"))[1]
    finally:
        await sess.close()
        stop.set()
        await asyncio.wait_for(task, 10)

    path = daemon_cfg.daemon.history_path()
    if not WINDOWS:
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    stored = [json.loads(line) for line in path.read_text().splitlines()]
    names = [e["event"] for e in stored]
    assert "message" in names and "offer_accepted" in names and "share_stopped" in names
    assert "progress" not in names and "pin" not in names and "started" not in names
    assert share["url"] not in path.read_text()          # secret link not kept

    stop, task = await start()
    sess = api_session(daemon_cfg)
    try:
        after = (await get(sess, "/api/history"))[1]
    finally:
        await sess.close()
        stop.set()
        await asyncio.wait_for(task, 10)
    restored = [e for e in after if e.get("restored")]
    assert [e["seq"] for e in restored] == [e["seq"] for e in before
                                            if e["event"] in {s["event"] for s in stored}]
    assert any(e["event"] == "message" and e["text"] == "vor dem Neustart" for e in restored)
    new = [e for e in after if not e.get("restored")]
    assert new[0]["event"] == "started" and new[0]["seq"] > before[-1]["seq"]


async def _has(sess, event):
    return any(e["event"] == event for e in (await get(sess, "/api/history"))[1])


def test_history_is_compacted(make_config):
    cfg = make_config("pc")
    d = Daemon(cfg, backends=[])
    for i in range(3 * HISTORY):
        d.emit("message", text=str(i))
    lines = cfg.daemon.history_path().read_text().splitlines()
    assert HISTORY <= len(lines) <= 2 * HISTORY
    d2 = Daemon(cfg, backends=[])
    assert [e["text"] for e in d2.history] == [str(i) for i in range(2 * HISTORY, 3 * HISTORY)]
    cfg.daemon.history = False
    assert not Daemon(cfg, backends=[]).history


def test_cli_choose(monkeypatch):
    answers = iter(["s", "j", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert _choose(["a", "b", "c"], "native") == [0, 2]
    monkeypatch.setattr("builtins.input", lambda prompt="": "s")
    assert _choose(["a", "b"], "none") == []            # no selection: "s" is not "yes"
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _choose(["a", "b"], "none") == [0, 1]


def test_devices_survive_a_missed_scan(make_config):
    from multidrop.core import Device
    from multidrop.daemon.service import DEVICE_TTL, Daemon
    d = Daemon(make_config("pc"))
    phone = Device(protocol="quickshare", id="abcd", name="Galaxy", address="10.0.0.5", port=4000)
    mac = Device(protocol="airdrop", id="m1", name="Mac", address="fe80::1", port=8770)
    d._merge([phone, mac], now=100.0)
    d._merge([mac], now=110.0)                       # phone missed one scan
    assert {g["name"] for g in d.device_groups()} == {"Galaxy", "Mac"}
    d._merge([mac], now=100.0 + DEVICE_TTL + 1)      # gone for longer than the TTL
    assert {g["name"] for g in d.device_groups()} == {"Mac"}
