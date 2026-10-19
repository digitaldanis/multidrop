"""AirPlay audio sender (RAOP) against a simulated receiver on 127.0.0.1."""

import asyncio
import base64
import os
import shutil
import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from multidrop import raop


class FakeReceiver:
    """Just enough of an AirPlay 1 speaker: RTSP, audio, timing and control."""

    def __init__(self, key: rsa.RSAPrivateKey | None = None, refuse: int | None = None):
        self.key, self.refuse = key, refuse
        self.methods: list[str] = []
        self.sdp = ""
        self.volume: float | None = None
        self.audio: dict[int, bytes] = {}       # seq -> decoded PCM
        self.syncs: list[bytes] = []
        self.timing_reply: asyncio.Future | None = None
        self.resent: asyncio.Future | None = None
        self.client_ports: dict[str, int] = {}
        self._aes: tuple[bytes, bytes] | None = None

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        self.timing_reply, self.resent = loop.create_future(), loop.create_future()
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.server.close()
        for ep in getattr(self, "endpoints", []):
            ep.close()

    def _decode(self, payload: bytes) -> bytes:
        if self._aes:
            n = len(payload) & ~0xF
            dec = Cipher(algorithms.AES(self._aes[0]), modes.CBC(self._aes[1])).decryptor()
            payload = dec.update(payload[:n]) + dec.finalize() + payload[n:]
        return raop.alac_samples(payload)

    async def _udp(self, handler):
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: raop._Udp(handler), local_addr=("127.0.0.1", 0))
        self.endpoints = getattr(self, "endpoints", []) + [transport]
        return transport

    async def _client(self, reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            method = line.decode().split()[0]
            headers = {}
            while (h := (await reader.readline()).decode().strip()):
                k, _, v = h.partition(":")
                headers[k.strip().lower()] = v.strip()
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            self.methods.append(method)
            extra = ""
            status = 200
            if method == self.refuse:
                status = 403
            elif method == "ANNOUNCE":
                self.sdp = body.decode()
                attrs = dict(l[2:].split(":", 1) for l in self.sdp.splitlines() if l.startswith("a="))
                if "rsaaeskey" in attrs:
                    pad = lambda s: base64.b64decode(s + "=" * (-len(s) % 4))
                    aes = self.key.decrypt(pad(attrs["rsaaeskey"]), padding.OAEP(
                        mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
                    self._aes = (aes, pad(attrs["aesiv"]))
            elif method == "SETUP":
                t = dict(p.split("=", 1) for p in headers["transport"].split(";") if "=" in p)
                self.client_ports = {k: int(t[k]) for k in ("control_port", "timing_port")}
                audio = await self._udp(lambda d, a: self.audio.__setitem__(
                    struct.unpack(">H", d[2:4])[0], self._decode(d[12:])))
                self.control = await self._udp(self._on_control)
                timing = await self._udp(self._on_timing)
                port = lambda tr: tr.get_extra_info("sockname")[1]
                extra = (f"Session: 1234\r\nTransport: RTP/AVP/UDP;unicast;mode=record;"
                         f"server_port={port(audio)};control_port={port(self.control)};"
                         f"timing_port={port(timing)}\r\n")
                # Ask the sender for the time, as a receiver does.
                self.asked = raop.ntp_now()
                timing.sendto(b"\x80\xd2\x00\x07" + bytes(20) + struct.pack(">Q", self.asked),
                              ("127.0.0.1", self.client_ports["timing_port"]))
            elif method == "SET_PARAMETER":
                self.volume = float(body.decode().split(":")[1])
            writer.write(f"RTSP/1.0 {status} X\r\nCSeq: {headers['cseq']}\r\n{extra}\r\n".encode())
            await writer.drain()
        writer.close()

    def _on_timing(self, data, addr):
        if not self.timing_reply.done():
            self.timing_reply.set_result(data)

    def _on_control(self, data, addr):
        if data[1] == 0xD4:
            self.syncs.append(data)
        elif data[1] == 0xD6 and not self.resent.done():
            self.resent.set_result(data)


def test_alac_frame_round_trip_and_size():
    pcm = os.urandom(raop.FRAMES * raop.FRAME_BYTES)
    frame = raop.alac_frame(pcm)
    assert len(frame) == 7 + 4 * raop.FRAMES            # as PipeWire's write_codec_pcm
    assert frame[:3] == b"\x20\x00\x12"                 # stereo, has size, not compressed
    assert raop.alac_samples(frame) == pcm


def test_receiver_from_txt():
    r = raop.parse_txt("A1B2C3D4E5F6@Wohnzimmer", "10.0.0.5", 7000,
                       {b"et": b"0,3,5", b"am": b"AppleTV6,2"})
    assert (r.name, r.supported, r.model) == ("Wohnzimmer", True, "AppleTV6,2")
    assert not raop.parse_txt("x@HomePod", "h", 1, {b"et": b"4"}).supported
    assert not raop.parse_txt("x@Box", "h", 1, {b"et": b"0", b"pw": b"true"}).supported
    assert raop.parse_txt("x@Old", "h", 1, {b"et": b"1"}).supported


@pytest.mark.parametrize("encrypted", [False, True])
async def test_stream_to_simulated_receiver(monkeypatch, encrypted):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if encrypted else None
    if key:
        monkeypatch.setattr(raop, "_airport_key", key.public_key)
    receiver = FakeReceiver(key)
    port = await receiver.start()
    sender = raop.RaopSender("127.0.0.1", port, encryption={1} if encrypted else {0})
    pcm = os.urandom(3 * raop.FRAMES * raop.FRAME_BYTES)
    sender.feed(pcm)
    try:
        await sender.start()
        assert ("a=rsaaeskey:" in receiver.sdp) == encrypted
        assert "a=rtpmap:96 AppleLossless" in receiver.sdp
        reply = await asyncio.wait_for(receiver.timing_reply, 5)
        assert reply[:4] == b"\x80\xd3\x00\x07" and reply[8:16] == struct.pack(">Q", receiver.asked)
        await sender.set_volume(0.5)
        assert receiver.volume == -15.0
        for _ in range(100):
            if len(receiver.audio) >= 5 and receiver.syncs:
                break
            await asyncio.sleep(0.05)
        first = min(receiver.audio)
        got = b"".join(receiver.audio[(first + i) & 0xFFFF] for i in range(4))
        assert got[: len(pcm)] == pcm and not any(got[len(pcm):])     # then silence
        sync = receiver.syncs[0]
        assert sync[:4] == b"\x90\xd4\x00\x07"
        rtp_minus_latency, _, rtp = struct.unpack(">IQI", sync[4:])
        assert (rtp - rtp_minus_latency) & 0xFFFFFFFF == raop.LATENCY

        # A lost packet is sent again on the control port.
        receiver.control.sendto(b"\x80\xd5\x00\x01" + struct.pack(">HH", first, 1),
                                ("127.0.0.1", receiver.client_ports["control_port"]))
        resent = await asyncio.wait_for(receiver.resent, 5)
        assert struct.unpack(">H", resent[6:8])[0] == first
    finally:
        await sender.stop()
        await receiver.close()
    assert receiver.methods == ["OPTIONS", "ANNOUNCE", "SETUP", "RECORD", "SET_PARAMETER",
                                "TEARDOWN"]


async def test_refused_by_receiver():
    receiver = FakeReceiver(refuse="ANNOUNCE")
    port = await receiver.start()
    sender = raop.RaopSender("127.0.0.1", port)
    with pytest.raises(raop.RaopError, match="403"):
        await sender.start()
    await receiver.close()
    assert not sender.running


SHAIRPORT = shutil.which("shairport-sync")


@pytest.mark.skipif(not SHAIRPORT, reason="shairport-sync not installed")
@pytest.mark.parametrize("encryption", [{0}, {1}])
async def test_interop_shairport_sync(tmp_path, encryption):
    """A real AirPlay 1 receiver (shairport-sync, audio to stdout) plays a
    440 Hz tone; {1} checks the AirPort key against its private key."""
    import array
    import math
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = tmp_path / "shairport.conf"
    conf.write_text("")
    proc = await asyncio.create_subprocess_exec(
        SHAIRPORT, "-vv", "-c", str(conf), "-p", str(port), "-a", "multidrop-test", "-o", "stdout",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out = bytearray()

    async def collect():
        while chunk := await proc.stdout.read(65536):
            out.extend(chunk)
    reading = asyncio.ensure_future(collect())
    sender = raop.RaopSender("127.0.0.1", port, encryption=encryption)
    try:
        for _ in range(50):
            try:
                await sender.start()
                break
            except OSError:             # not listening yet
                await asyncio.sleep(0.2)
        await sender.set_volume(1.0)    # its default (-18) attenuates by ~38 dB
        tone = array.array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / raop.RATE))
                                 for i in range(raop.RATE // 10) for _ in range(2)))
        for _ in range(50):             # 5 s of audio in real time
            sender.feed(tone.tobytes())
            await asyncio.sleep(0.1)
    finally:
        await sender.stop()
        proc.terminate()
        log = (await proc.stderr.read()).decode(errors="replace")
        await proc.wait()
        reading.cancel()
    samples = array.array("h", bytes(out[: len(out) // 4 * 4]))[0::2]
    loud = [x for x in samples if abs(x) > 1000]
    peak = max(map(abs, samples), default=0)
    assert len(loud) > raop.RATE // 2, f"{len(out)} bytes, {len(loud)} loud, peak {peak}\n{log[-6000:]}"
    # Still a 440 Hz tone: rising swings through ±1000 (ignores dither in the silence).
    first = next(i for i, x in enumerate(samples) if abs(x) > 1000)
    last = len(samples) - next(i for i, x in enumerate(reversed(samples)) if abs(x) > 1000)
    cycles, low = 0, False
    for x in samples[first:last]:
        if x < -1000:
            low = True
        elif x > 1000 and low:
            cycles, low = cycles + 1, False
    assert 400 < cycles / ((last - first) / raop.RATE) < 480


def test_airplay_audio_controller():
    """Tray side (win_audio): capture → sender, volume, stop; capture faked."""
    import threading
    import time

    from multidrop.win_audio import AirPlayAudio
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    receiver = FakeReceiver()
    port = asyncio.run_coroutine_threadsafe(receiver.start(), loop).result(5)
    chunk = b"\x01\x02\x03\x04" * 1000
    captured = threading.Event()

    def capture(stop, on_pcm):
        while not stop.is_set():
            on_pcm(chunk)
            time.sleep(0.02)
        captured.set()

    changes = []
    audio = AirPlayAudio(loop, capture=capture, on_change=lambda: changes.append(audio.current))
    speaker = raop.Receiver("Küche", "127.0.0.1", port, {0}, False)
    try:
        audio.play(speaker)
        assert audio.current is speaker and receiver.volume == -7.5        # default 75 %
        for _ in range(100):
            if any(chunk[:8] in pcm for pcm in receiver.audio.values()):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("captured audio did not arrive")
        audio.set_volume(0.25)
        assert receiver.volume == -22.5
        audio.stop()
        assert captured.wait(5) and audio.current is None
        assert receiver.methods[-1] == "TEARDOWN"
        assert changes[0] is speaker and changes[-1] is None
    finally:
        asyncio.run_coroutine_threadsafe(receiver.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)


def test_airplay_audio_timeout_cancels_on_the_loop():
    """A receiver that never answers: the call times out and the coroutine is
    cancelled on the service loop instead of running on."""
    import threading

    from multidrop.win_audio import AirPlayAudio
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    cancelled = threading.Event()

    async def hang():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
    try:
        with pytest.raises(TimeoutError):
            AirPlayAudio(loop)._run(hang(), timeout=0.2)
        assert cancelled.wait(2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
