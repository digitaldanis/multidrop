"""AirDrop announcement without the radio: mDNS bursts on loopback, the BLE
wake-up payload, and that discovery/sending wrap themselves in the wake-up
(with a fake advertiser; no Bluetooth is used)."""

import asyncio
import socket
import struct
import time

import pytest
from zeroconf import DNSIncoming, const

# The BLE wake-up talks to BlueZ over D-Bus (Linux only).
pytest.importorskip("dbus_fast")

from multidrop.backends.airdrop import AirDropBackend, ble  # noqa: E402
from multidrop.backends.airdrop.mdns import FLAGS, Publisher


def _listener() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("224.0.0.251", 5353))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 struct.pack("4s4s", socket.inet_aton("224.0.0.251"), socket.inet_aton("127.0.0.1")))
    s.setblocking(False)
    return s


async def test_announcement_bursts_on_loopback():
    sock = _listener()
    pub = Publisher(["127.0.0.1"], interval=0.3, burst=3)
    seen = []
    try:
        await pub.start("a1b2c3d4e5f6", 8771)
        deadline = time.monotonic() + 1.6
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(9000)
            except BlockingIOError:
                await asyncio.sleep(0.01)
                continue
            msg = DNSIncoming(data)
            if not msg.is_response():
                continue
            names = {r.name for r in msg.answers()}
            if "a1b2c3d4e5f6._airdrop._tcp.local." in names:
                seen.append(msg)
    finally:
        await pub.stop()
        sock.close()
    assert pub.announcements >= 9               # >= 3 bursts of 3
    assert len(seen) >= pub.announcements       # plus zeroconf's own announcements
    burst = seen[-1]
    assert burst.flags & const._FLAGS_AA
    types = {r.type for r in burst.answers()}
    assert {const._TYPE_PTR, const._TYPE_SRV, const._TYPE_TXT, const._TYPE_A} <= types
    srv = next(r for r in burst.answers() if r.type == const._TYPE_SRV)
    txt = next(r for r in burst.answers() if r.type == const._TYPE_TXT)
    assert srv.port == 8771 and f"flags={FLAGS}".encode() in txt.text


async def test_no_bursts_when_off():
    pub = Publisher(["127.0.0.1"], interval=0)
    await pub.start("0000000000aa", 8771)
    await asyncio.sleep(0.2)
    await pub.stop()
    assert pub.announcements == 0


def test_ble_payload_shapes():
    os26 = ble.payload("os26")
    assert os26.hex() == "0512" + "40000000" + "00000000" + "03" + "00" * 8 + "00"
    old = ble.payload("published")
    assert old.hex() == "0512" + "00" * 8 + "01" + "00" * 8 + "00"
    assert len(os26) == len(old) == 20


class FakeAdvertiser:
    starts = stops = 0
    fail = False

    def __init__(self, shape, connectable):
        self.shape, self.connectable = shape, connectable

    async def start(self):
        if FakeAdvertiser.fail:
            raise ble.BleError("no adapter")
        FakeAdvertiser.starts += 1
        return "/org/bluez/hci0"

    async def stop(self):
        FakeAdvertiser.stops += 1


async def test_wake_around_send_and_discover(make_config, monkeypatch, tmp_path):
    monkeypatch.setattr(ble, "advertiser", FakeAdvertiser)
    FakeAdvertiser.starts = FakeAdvertiser.stops = 0
    a_cfg, b_cfg = make_config("Linux-PC"), make_config("iPhone")
    for cfg in (a_cfg, b_cfg):
        cfg.airdrop.bind = "127.0.0.1"
        cfg.airdrop.port = 0
        cfg.airdrop.mdns = False
        cfg.airdrop.mdns_addresses = ["127.0.0.1"]
    a_cfg.airdrop.ble = True
    a, b = AirDropBackend(a_cfg), AirDropBackend(b_cfg)

    async def accept_all(offer):
        return {f.id for f in offer.files}
    await b.start(accept_all, lambda e, d: None)
    try:
        f = tmp_path / "foto.jpg"
        f.write_bytes(b"\xff\xd8" + bytes(5000))
        device = await a.resolve("127.0.0.1", b.port)
        assert (FakeAdvertiser.starts, FakeAdvertiser.stops) == (1, 1)
        await a.send(device, [f])
        assert (FakeAdvertiser.starts, FakeAdvertiser.stops) == (2, 2)

        # Concurrent users share one advertisement.
        async with a._wake():
            async with a._wake():
                assert a._ble is not None
            assert a._ble is not None
        assert a._ble is None and (FakeAdvertiser.starts, FakeAdvertiser.stops) == (3, 3)

        await a.discover(0.3)
        assert FakeAdvertiser.starts == 4

        # No Bluetooth: only a warning, the transfer still runs.
        FakeAdvertiser.fail = True
        await a.send(device, [f])
        assert (b.config.download_dir / "foto (1).jpg").exists()
    finally:
        FakeAdvertiser.fail = False
        await a.stop()
        await b.stop()


async def test_discover_keeps_ble_awake_during_https(make_config, monkeypatch):
    from multidrop.backends.airdrop import client, mdns
    cfg = make_config("pc")
    cfg.airdrop.ble = True
    cfg.airdrop.mdns_addresses = ["127.0.0.1"]
    a = AirDropBackend(cfg)
    monkeypatch.setattr(ble, "advertiser", FakeAdvertiser)

    async def browse(*args, **kwargs):
        assert a._ble is not None
        return [mdns.Found("abcdefabcdef", "127.0.0.1", 8771, 137)]

    async def discover(*args, **kwargs):
        assert a._ble is not None
        return {"ReceiverComputerName": "iPhone"}

    monkeypatch.setattr(mdns, "browse", browse)
    monkeypatch.setattr(client, "discover", discover)
    try:
        assert (await a.discover())[0].name == "iPhone"
        assert a._ble is None
    finally:
        await a.stop()
