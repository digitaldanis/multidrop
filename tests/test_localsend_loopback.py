"""Two LocalSend instances on 127.0.0.1 talking to each other."""

import hashlib
import os

import aiohttp
import pytest

from multidrop.backends.localsend import API, LocalSendBackend
from multidrop.core import TransferError


async def accept_all(offer):
    return {f.id for f in offer.files}


async def reject_all(offer):
    return set()


class Events(list):
    def __call__(self, event, details):
        self.append((event, details))

    def names(self):
        return [e for e, _ in self]


@pytest.fixture
async def pair(make_config):
    """Returns a factory for (sender, receiver) backends; stops them after."""
    started = []

    async def make(https=True, pin=None, on_offer=accept_all):
        a = LocalSendBackend(make_config("alice", https=https))
        b = LocalSendBackend(make_config("bob", https=https, pin=pin))
        events = Events()
        await b.start(on_offer, events)
        started.extend([a, b])
        return a, b, events

    yield make
    for backend in started:
        await backend.stop()


@pytest.mark.parametrize("https", [True, False])
async def test_send_file_a_to_b(pair, tmp_path, https):
    a, b, events = await pair(https=https)
    src = tmp_path / "hello.txt"
    src.write_bytes(b"Hello from A to B\n" * 1000)

    target = await a.resolve("127.0.0.1", b.port)
    assert target.name == "bob"
    assert target.id == b.fingerprint
    await a.send(target, [src])

    out = b.config.download_dir / "hello.txt"
    assert out.read_bytes() == src.read_bytes()
    assert "file_received" in events.names()
    assert "session_done" in events.names()
    assert b.server.session is None


async def test_multicast_discovery(make_config):
    a = LocalSendBackend(make_config("alice"))
    b = LocalSendBackend(make_config("bob"))
    try:
        await b.start(accept_all)
        devices = await a.discover(timeout=1.5)
        assert [(d.name, d.id) for d in devices] == [("bob", b.fingerprint)]
        assert devices[0].port == b.port
        # b learned about a too, from a's announcement or register call.
        assert a.fingerprint in b.peers
    finally:
        await a.stop()
        await b.stop()


async def test_large_file_and_folder(pair, tmp_path):
    a, b, events = await pair()
    folder = tmp_path / "album"
    (folder / "sub").mkdir(parents=True)
    big = os.urandom(20 * 1024 * 1024)
    (folder / "big.bin").write_bytes(big)
    (folder / "sub" / "small.txt").write_text("klein")
    (folder / "empty").write_bytes(b"")

    sent = Events()
    await a.send(await a.resolve("127.0.0.1", b.port), [folder], sent)

    root = b.config.download_dir / "album"
    assert hashlib.sha256((root / "big.bin").read_bytes()).digest() == \
        hashlib.sha256(big).digest()
    assert (root / "sub" / "small.txt").read_text() == "klein"
    assert (root / "empty").read_bytes() == b""
    assert sent.names().count("file_sent") == 3


async def test_rejected(pair, tmp_path):
    a, b, events = await pair(on_offer=reject_all)
    src = tmp_path / "x.txt"
    src.write_text("x")
    with pytest.raises(TransferError, match="rejected"):
        await a.send(await a.resolve("127.0.0.1", b.port), [src])
    assert not b.config.download_dir.exists() or not any(b.config.download_dir.iterdir())


async def test_pin(pair, tmp_path):
    a, b, events = await pair(pin="4711")
    src = tmp_path / "x.txt"
    src.write_text("geheim")
    target = await a.resolve("127.0.0.1", b.port)
    with pytest.raises(TransferError, match="PIN"):
        await a.send(target, [src])
    with pytest.raises(TransferError, match="PIN"):
        await a.send(target, [src], pin="0000")
    await a.send(target, [src], pin="4711")
    assert (b.config.download_dir / "x.txt").read_text() == "geheim"


async def test_fingerprint_pinning(pair, tmp_path):
    a, b, events = await pair()
    src = tmp_path / "x.txt"
    src.write_text("x")
    target = await a.resolve("127.0.0.1", b.port)
    target.id = "AB" * 32  # pretend the announced fingerprint was different
    with pytest.raises(TransferError, match="fingerprint"):
        await a.send(target, [src])


async def _raw_prepare(b, files, info_alias="mallory"):
    """Talk to the receiver directly, as a hostile or foreign client would."""
    url = f"https://127.0.0.1:{b.port}{API}"
    info = {"alias": info_alias, "version": "2.1", "fingerprint": "f" * 8,
            "port": 1, "protocol": "https", "deviceType": "mobile"}
    http = aiohttp.ClientSession()
    r = await http.post(f"{url}/prepare-upload", ssl=False,
                        json={"info": info, "files": files})
    body = await r.json(content_type=None) if r.status == 200 else None
    return http, url, r.status, body


async def test_path_traversal_is_contained(pair, tmp_path):
    a, b, events = await pair()
    data = b"evil"
    http, url, status, body = await _raw_prepare(b, {
        "f1": {"id": "f1", "fileName": "../../../escape.txt", "size": len(data),
               "fileType": "text/plain", "sha256": None, "preview": None}})
    try:
        assert status == 200
        q = {"sessionId": body["sessionId"], "fileId": "f1", "token": body["files"]["f1"]}
        r = await http.post(f"{url}/upload", params=q, data=data, ssl=False)
        assert r.status == 200
    finally:
        await http.close()
    assert (b.config.download_dir / "escape.txt").read_bytes() == data
    assert not (tmp_path / "escape.txt").exists()
    assert not (b.config.download_dir.parent / "escape.txt").exists()


async def test_checksum_mismatch_and_bad_token(pair):
    a, b, events = await pair()
    data = b"payload"
    http, url, status, body = await _raw_prepare(b, {
        "f1": {"id": "f1", "fileName": "c.bin", "size": len(data),
               "fileType": "application/octet-stream", "sha256": "0" * 64}})
    try:
        assert status == 200
        sid, token = body["sessionId"], body["files"]["f1"]
        r = await http.post(f"{url}/upload", ssl=False, data=data,
                            params={"sessionId": sid, "fileId": "f1", "token": "nope"})
        assert r.status == 403
        r = await http.post(f"{url}/upload", ssl=False, data=data,
                            params={"sessionId": sid, "fileId": "f1", "token": token})
        assert r.status == 422
        # A second sender is told the receiver is busy.
        http2, _, status2, _ = await _raw_prepare(b, {
            "g": {"id": "g", "fileName": "g", "size": 1, "fileType": "x"}})
        await http2.close()
        assert status2 == 409
        r = await http.post(f"{url}/cancel", ssl=False, params={"sessionId": sid})
        assert r.status == 200
    finally:
        await http.close()
    assert list(b.config.download_dir.iterdir()) == []
    assert b.server.session is None


async def test_text_message(pair):
    a, b, events = await pair()
    text = "Hello world"
    http, url, status, body = await _raw_prepare(b, {
        "m": {"id": "m", "fileName": "m.txt", "size": len(text.encode()),
              "fileType": "text/plain", "preview": text}})
    await http.close()
    assert status == 204
    assert ("message", {"from": "mallory", "text": text}) in events


async def test_bad_request(pair):
    a, b, events = await pair()
    http, url, status, body = await _raw_prepare(b, {"x": {"id": "x"}})
    await http.close()
    assert status == 400


async def test_send_text_message(pair):
    a, b, events = await pair()
    sent = Events()
    await a.send(await a.resolve("127.0.0.1", b.port), [], sent, text="Hello 👋")
    assert ("message", {"from": "alice", "text": "Hello 👋"}) in events
    assert sent.names()[-1] == "done"
