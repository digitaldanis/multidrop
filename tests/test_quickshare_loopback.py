"""Two Quick Share instances on 127.0.0.1, plus hostile and broken peers."""

import asyncio
import os
import struct

import pytest

from multidrop.backends.quickshare import QuickShareBackend, connection, frames, outbound
from multidrop.backends.quickshare.crypto import Ukey2Client
from multidrop.backends.quickshare.mdns import MdnsPublisher, browse
from multidrop.backends.quickshare.pb import ukey_pb2
from multidrop.core import TransferError


class Events(list):
    def __call__(self, event, details):
        self.append((event, details))

    def of(self, name):
        return [d for e, d in self if e == name]


async def accept_all(offer):
    return {f.id for f in offer.files}


async def reject_all(offer):
    return set()


@pytest.fixture
async def qs(make_config):
    started = []

    async def make(on_offer=accept_all):
        a_cfg, b_cfg = make_config("alice"), make_config("bob")
        for cfg in (a_cfg, b_cfg):
            cfg.quickshare.bind = "127.0.0.1"
            cfg.quickshare.mdns = False
            cfg.quickshare.ble = False
        a, b = QuickShareBackend(a_cfg), QuickShareBackend(b_cfg)
        events = Events()
        await b.start(on_offer, events)
        started.append(b)
        target = await a.resolve("127.0.0.1", b.port)
        return a, b, target, events

    yield make
    for backend in started:
        await backend.stop()


async def wait_for(events, name, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if events.of(name):
            return events.of(name)
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {name} event in {events}")


async def test_send_files_and_folder(qs, tmp_path):
    a, b, target, events = await qs()
    folder = tmp_path / "Holiday"
    (folder / "Day 1").mkdir(parents=True)
    big = os.urandom(3 * 1024 * 1024 + 17)   # several 512 KiB chunks
    (folder / "Day 1" / "IMG_0001.jpg").write_bytes(big)
    (folder / "empty.txt").write_bytes(b"")
    single = tmp_path / "Bericht.pdf"
    single.write_bytes(b"%PDF-1.7 test")

    sent = Events()
    await a.send(target, [folder, single], sent)
    await wait_for(events, "session_done")

    root = b.config.download_dir
    assert (root / "Holiday" / "Day 1" / "IMG_0001.jpg").read_bytes() == big
    assert (root / "Holiday" / "empty.txt").read_bytes() == b""
    assert (root / "Bericht.pdf").read_bytes() == single.read_bytes()
    # Both sides derived the same 4-digit code.
    assert sent.of("pin")[0]["pin"] == events.of("pin")[0]["pin"]
    assert len(sent.of("file_sent")) == 3
    assert not list(root.rglob("*.part"))


@pytest.mark.parametrize("text, kind", [
    ("https://example.org/pfad?x=1", "url"),
    ("Hello from the PC, äöü 👋", "text"),
])
async def test_text_and_url(qs, text, kind):
    a, b, target, events = await qs()
    await a.send(target, [], Events(), text=text)
    msgs = await wait_for(events, "message")
    assert msgs[0]["text"] == text and msgs[0]["kind"] == kind


async def test_rejected(qs, tmp_path):
    a, b, target, events = await qs(on_offer=reject_all)
    src = tmp_path / "x.bin"
    src.write_bytes(b"data")
    with pytest.raises(TransferError, match="rejected"):
        await a.send(target, [src], Events())
    assert not b.config.download_dir.exists() or not any(b.config.download_dir.iterdir())


async def test_path_traversal_is_contained(qs, tmp_path):
    a, b, target, events = await qs()
    src = tmp_path / "x.txt"
    src.write_bytes(b"evil")
    # Name with "../" parts ends up as parent_folder "../.." on the wire.
    await outbound.send("127.0.0.1", b.port, our_name="mallory",
                        entries=[(src, "../../evil.txt"), (src, "/etc/cron.d/x")],
                        on_event=Events())
    await wait_for(events, "session_done")
    root = b.config.download_dir
    assert (root / "evil.txt").read_bytes() == b"evil"
    assert (root / "etc" / "cron.d" / "x").read_bytes() == b"evil"
    assert not (root.parent / "evil.txt").exists()


async def test_sender_sends_more_than_announced(qs, tmp_path, monkeypatch):
    a, b, target, events = await qs()
    src = tmp_path / "x.bin"
    src.write_bytes(b"12345")

    async def lying_send_file(conn, path, name, pid, size, on_event):
        await conn.send_file_chunk(pid, size, 0, b"1234567890", last=True)

    monkeypatch.setattr(outbound, "_send_file", lying_send_file)
    with pytest.raises(TransferError):
        await a.send(target, [src], Events())
    failed = await wait_for(events, "failed")
    assert "more data" in failed[0]["error"]
    assert not any(b.config.download_dir.rglob("*"))


async def test_sender_gives_up_while_receiver_decides(qs, tmp_path):
    decided = asyncio.Event()

    async def slow(offer):
        await decided.wait()
        return {f.id for f in offer.files}

    a, b, target, events = await qs(on_offer=slow)
    src = tmp_path / "x.bin"
    src.write_bytes(b"data")
    task = asyncio.ensure_future(a.send(target, [src], Events()))
    await wait_for(events, "pin")
    await asyncio.sleep(0.2)
    task.cancel()
    failed = await wait_for(events, "failed")
    assert "gave up" in failed[0]["error"] or "cancel" in failed[0]["error"]
    decided.set()


async def test_keepalives_during_slow_consent(qs, tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "KEEPALIVE_INTERVAL", 0.05)

    async def slow(offer):
        await asyncio.sleep(0.6)   # ~12 keep-alives each way meanwhile
        return {f.id for f in offer.files}

    a, b, target, events = await qs(on_offer=slow)
    src = tmp_path / "x.bin"
    src.write_bytes(os.urandom(2 * 1024 * 1024))
    await a.send(target, [src], Events())
    await wait_for(events, "session_done")
    assert (b.config.download_dir / "x.bin").read_bytes() == src.read_bytes()


async def _raw(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def send(data):
        writer.write(struct.pack(">I", len(data)) + data)
        await writer.drain()

    async def recv():
        (n,) = struct.unpack(">I", await reader.readexactly(4))
        return await reader.readexactly(n)

    return reader, writer, send, recv


async def test_garbage_and_oversized_frames(qs, tmp_path):
    a, b, target, events = await qs()
    # Not a protobuf at all.
    _, w, send, _ = await _raw(b.port)
    await send(b"\xff\xfe garbage \x00")
    assert "malformed" in (await wait_for(events, "failed"))[0]["error"]
    w.close()
    # Length prefix far beyond the 5 MiB limit.
    _, w, _, _ = await _raw(b.port)
    w.write(struct.pack(">I", 0x7FFFFFFF))
    await w.drain()
    await asyncio.sleep(0.2)
    assert any("too large" in f["error"] for f in events.of("failed"))
    w.close()
    # The server still works afterwards.
    src = tmp_path / "ok.txt"
    src.write_text("ok")
    await a.send(target, [src], Events())
    await wait_for(events, "session_done")


async def test_bad_ukey2_gets_alert(qs):
    a, b, target, events = await qs()
    _, w, send, recv = await _raw(b.port)
    await send(frames.connection_request("Test", frames.EndpointInfo("x")))
    ci = ukey_pb2.Ukey2ClientInit(version=1, random=b"short", next_protocol="AES_256_CBC-HMAC_SHA256")
    await send(ukey_pb2.Ukey2Message(message_type=ukey_pb2.Ukey2Message.CLIENT_INIT,
                                     message_data=ci.SerializeToString()).SerializeToString())
    msg = ukey_pb2.Ukey2Message()
    msg.ParseFromString(await recv())
    alert = ukey_pb2.Ukey2Alert()
    alert.ParseFromString(msg.message_data)
    assert msg.message_type == ukey_pb2.Ukey2Message.ALERT
    assert alert.type == ukey_pb2.Ukey2Alert.BAD_RANDOM
    w.close()
    assert "key exchange" in (await wait_for(events, "failed"))[0]["error"]


async def test_tampered_encrypted_frame(qs):
    """Correct handshake, then a frame with a broken HMAC."""
    a, b, target, events = await qs()
    _, w, send, recv = await _raw(b.port)
    await send(frames.connection_request("Test", frames.EndpointInfo("x")))
    client = Ukey2Client()
    await send(client.client_init)
    client.handle_server_init(await recv())
    await send(client.client_finish)
    await send(frames.connection_response())
    await recv()   # server's connection response
    await send(b"\x0a\x03abc\x12\x20" + bytes(32))  # SecureMessage, bogus signature
    failed = await wait_for(events, "failed")
    assert "bad frame" in failed[0]["error"]
    w.close()


async def test_mdns_loopback():
    pub = MdnsPublisher(["127.0.0.1"])
    await pub.start("AbC1", 45678, frames.EndpointInfo("Pixel ü", frames.DEVICE_PHONE))
    try:
        found = await browse(1.5, ["127.0.0.1"])
    finally:
        await pub.stop()
    match = [f for f in found if f.endpoint_id == "AbC1"]
    assert match and match[0].port == 45678 and match[0].info.name == "Pixel ü"
    assert match[0].info.device_type == frames.DEVICE_PHONE


def test_endpoint_info_and_names():
    info = frames.EndpointInfo("Galaxy S24", frames.DEVICE_PHONE)
    raw = info.serialize()
    assert raw[0] == 0b0000_0010 and raw[17] == len("Galaxy S24")
    assert frames.EndpointInfo.parse(raw) == info
    hidden = frames.EndpointInfo.parse(bytes([0x10 | 2]) + bytes(16))
    assert hidden.name is None and not hidden.visible
    with pytest.raises(ValueError):
        frames.EndpointInfo.parse(bytes([2]) + bytes(16) + bytes([50]) + b"short")
    name = frames.mdns_instance_name("aB3x")
    assert frames.parse_mdns_instance_name(name) == "aB3x"
    assert frames.b64url_decode(name) == b"\x23aB3x\xfc\x9f\x5e\x00\x00"
    assert frames.parse_mdns_instance_name("not-quickshare") is None


def test_ble_service_data_format():
    pytest.importorskip("dbus_fast")      # BlueZ over D-Bus, Linux only
    from multidrop.backends.quickshare import ble
    data = ble.service_data()
    # NearDrop PROTOCOL.md / rquickshare: fc128e0142, 9 zero bytes, 10 random bytes.
    assert len(data) == 24
    assert data[:14] == bytes.fromhex("fc128e0142") + bytes(9)
    assert ble.service_data()[14:] != data[14:]


@pytest.mark.skipif(os.environ.get("MULTIDROP_TEST_BLE") != "1",
                    reason="real Bluetooth; set MULTIDROP_TEST_BLE=1")
async def test_ble_register_unregister_real_adapter():
    from multidrop.backends.quickshare.ble import BleAdvertiser
    adv = BleAdvertiser()
    path = await adv.start()
    assert path.startswith("/org/bluez/")
    await adv.stop()
