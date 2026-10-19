"""AirPlay audio sender (RAOP, "AirPlay 1"): 44.1 kHz 16-bit stereo PCM in
uncompressed ALAC frames over RTSP + RTP/UDP, like PipeWire's raop-sink.

Receivers that offer no encryption (et=0) or RSA (et=1) and no password are
supported; FairPlay/MFi-only receivers are listed as unsupported.

  sender = RaopSender("192.168.1.20", 7000)
  await sender.start()
  sender.feed(pcm)             # little-endian int16 stereo, any length
  await sender.set_volume(0.5)
  await sender.stop()
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from multidrop.backends.quickshare.mdns import _zeroconf, default_address

log = logging.getLogger(__name__)

SERVICE = "_raop._tcp.local."
RATE = 44100
FRAMES = 352                      # frames per RTP packet
FRAME_BYTES = 4                   # 16-bit stereo
LATENCY = 2 * RATE                # receiver plays 2 s behind the sync time
NTP_EPOCH = 2208988800            # 1900-01-01 to 1970-01-01 in seconds
# AirPort Express public key, used to hand the AES key to et=1 receivers.
AIRPORT_N = (
    "59dE8qLieItsH1WgjrcFRKj6eUWqi+bGLOX1HL3U3GhC/j0Qg90u3sG/1CUtwC"
    "5vOYvfDmFI6oSFXi5ELabWJmT2dKHzBJKa3k9ok+8t9ucRqMd6DZHJ2YCCLlDR"
    "KSKv6kDqnw4UwPdpOMXziC/AMj3Z/lUVX1G7WSHCAWKf1zNS1eLvqr+boEjXuB"
    "OitnZ/bDzPHrTOZz0Dew0uowxf/+sG+NCK3eQJVxqcaJ/vEHKIVd2M+5qL71yJ"
    "Q+87X6oV3eaYvt3zWZYD6z5vYTcrtij2VZ9Zmni/UAaHqn9JdsBWLUEpVviYnh"
    "imNVvYFZeCXg/IdTQ+x4IRdiXNv5hEew==")


class RaopError(Exception):
    pass


# -- discovery -----------------------------------------------------------------

@dataclass
class Receiver:
    name: str
    host: str
    port: int
    encryption: set[int]          # TXT et: 0 none, 1 RSA, 3/5 FairPlay, 4 MFi
    password: bool
    model: str = ""

    @property
    def supported(self) -> bool:
        return not self.password and bool(self.encryption & {0, 1})


def parse_txt(name: str, host: str, port: int, txt: dict[bytes, bytes | None]) -> Receiver:
    def text(key: bytes) -> str:
        raw = txt.get(key)
        return raw.decode(errors="replace") if raw else ""
    et = {int(x) for x in text(b"et").split(",") if x.strip().isdigit()} if text(b"et") else {0}
    # Service names are "<MAC>@<name>".
    return Receiver(name=name.split("@", 1)[-1], host=host, port=port, encryption=et,
                    password=text(b"pw").lower() == "true", model=text(b"am"))


async def discover(timeout: float = 3.0, addresses: list[str] | None = None) -> list[Receiver]:
    zc = _zeroconf(addresses or [default_address()])
    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    browser = AsyncServiceBrowser(zc.zeroconf, SERVICE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)

        async def one(name: str) -> Receiver | None:
            info = AsyncServiceInfo(SERVICE, name)
            if not await info.async_request(zc.zeroconf, 2000):
                return None
            host = next(iter(info.parsed_addresses(IPVersion.V4Only)), "")
            if not host or not info.port:
                return None
            return parse_txt(name[: -len(SERVICE) - 1], host, info.port, info.properties or {})
        found = [r for r in await asyncio.gather(*(one(n) for n in names)) if r]
    finally:
        await browser.async_cancel()
        await zc.async_close()
    return sorted(found, key=lambda r: r.name.lower())


# -- packets -------------------------------------------------------------------

def ntp_now() -> int:
    t = time.time() + NTP_EPOCH
    return (int(t) << 32) | int((t % 1) * (1 << 32))


def alac_frame(pcm: bytes) -> bytes:
    """Uncompressed ALAC frame: a 23-bit header (stereo, has size, not
    compressed), the frame count, then the samples big-endian, all shifted
    by the header's 7 odd bits."""
    n = len(pcm) // FRAME_BYTES
    samples = bytearray(pcm[: n * FRAME_BYTES])
    samples[0::2], samples[1::2] = samples[1::2], samples[0::2]    # to big-endian
    body = n.to_bytes(4, "big") + bytes(samples)
    header = (1 << 20) | (1 << 3) | 1
    value = ((header << (8 * len(body))) | int.from_bytes(body, "big")) << 1
    return value.to_bytes(len(body) + 3, "big")


def alac_samples(frame: bytes) -> bytes:
    """Inverse of alac_frame (for tests and the simulated receiver)."""
    value = int.from_bytes(frame, "big") >> 1
    body = (value & ((1 << (8 * (len(frame) - 3))) - 1)).to_bytes(len(frame) - 3, "big")
    if value >> (8 * len(body)) != (1 << 20) | (1 << 3) | 1:
        raise ValueError("not an uncompressed stereo ALAC frame")
    n = int.from_bytes(body[:4], "big")
    samples = bytearray(body[4: 4 + n * FRAME_BYTES])
    samples[0::2], samples[1::2] = samples[1::2], samples[0::2]
    return bytes(samples)


def _airport_key() -> rsa.RSAPublicKey:
    n = int.from_bytes(base64.b64decode(AIRPORT_N), "big")
    return rsa.RSAPublicNumbers(65537, n).public_key()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.on_packet(data, addr)


# -- sender --------------------------------------------------------------------

class RaopSender:
    def __init__(self, host: str, port: int, encryption: set[int] | None = None,
                 latency: int = LATENCY):
        self.host, self.port, self.latency = host, port, latency
        self.encrypt = bool(encryption) and 0 not in encryption and 1 in encryption
        self._aes_key, self._aes_iv = os.urandom(16), os.urandom(16)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = 0
        self._session = ""
        self._uri = ""
        rnd = os.urandom(8).hex().upper()
        self._headers = {"User-Agent": "MultiDrop/1.0", "Client-Instance": rnd,
                         "DACP-ID": rnd, "Active-Remote": str(int.from_bytes(os.urandom(4), "big"))}
        self._seq = int.from_bytes(os.urandom(2), "big")
        self._rtptime = int.from_bytes(os.urandom(4), "big")
        self._ssrc = int.from_bytes(os.urandom(4), "big")
        self._buffer = bytearray()
        self._sent: dict[int, bytes] = {}            # seq -> packet, for resend requests
        self._audio = self._control = self._timing = None
        self._server_ports: tuple[int, int, int] = (0, 0, 0)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.error: BaseException | None = None

    # RTSP ----------------------------------------------------------------

    async def _request(self, method: str, uri: str | None = None, headers: dict | None = None,
                       body: bytes = b"", content_type: str | None = None) -> dict:
        async with self._lock:
            self._cseq += 1
            lines = [f"{method} {uri or self._uri} RTSP/1.0", f"CSeq: {self._cseq}"]
            hdrs = {**self._headers, **(headers or {})}
            if self._session:
                hdrs["Session"] = self._session
            if body:
                hdrs["Content-Type"] = content_type or "application/octet-stream"
                hdrs["Content-Length"] = str(len(body))
            lines += [f"{k}: {v}" for k, v in hdrs.items()]
            self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
            await self._writer.drain()
            status = await asyncio.wait_for(self._reader.readline(), 10)
            parts = status.decode(errors="replace").split(" ", 2)
            reply: dict = {}
            while (line := (await self._reader.readline()).decode(errors="replace").strip()):
                k, _, v = line.partition(":")
                reply[k.strip().lower()] = v.strip()
            if int(reply.get("content-length", 0)):
                await self._reader.readexactly(int(reply["content-length"]))
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if code != 200:
            hint = {401: " (password required)", 403: " (access denied on the receiver)",
                    453: " (receiver busy)"}.get(code, "")
            raise RaopError(f"{method}: RTSP {code or status!r}{hint}")
        return reply

    def _sdp(self, local: str) -> str:
        sid = int.from_bytes(os.urandom(4), "big")
        lines = ["v=0", f"o=iTunes {sid} 0 IN IP4 {local}", "s=iTunes",
                 f"c=IN IP4 {self.host}", "t=0 0", "m=audio 0 RTP/AVP 96",
                 "a=rtpmap:96 AppleLossless",
                 f"a=fmtp:96 {FRAMES} 0 16 40 10 14 2 255 0 0 {RATE}"]
        if self.encrypt:
            key = _airport_key().encrypt(self._aes_key, padding.OAEP(
                mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
            lines += [f"a=rsaaeskey:{_b64(key)}", f"a=aesiv:{_b64(self._aes_iv)}"]
        return "\r\n".join(lines) + "\r\n"

    async def start(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), 10)
        local = self._writer.get_extra_info("sockname")[0]
        self._uri = f"rtsp://{local}/{int.from_bytes(os.urandom(4), 'big')}"
        loop = asyncio.get_running_loop()
        try:
            await self._request("OPTIONS", "*")
            await self._request("ANNOUNCE", body=self._sdp(local).encode(),
                                content_type="application/sdp")
            _, self._control = await loop.create_datagram_endpoint(
                lambda: _Udp(self._on_control), local_addr=(local, 0))
            _, self._timing = await loop.create_datagram_endpoint(
                lambda: _Udp(self._on_timing), local_addr=(local, 0))
            control_port = self._control.transport.get_extra_info("sockname")[1]
            timing_port = self._timing.transport.get_extra_info("sockname")[1]
            reply = await self._request("SETUP", headers={
                "Transport": "RTP/AVP/UDP;unicast;interleaved=0-1;mode=record;"
                             f"control_port={control_port};timing_port={timing_port}"})
            self._session = reply.get("session", "").split(";")[0]
            ports = dict(p.split("=", 1) for p in reply.get("transport", "").split(";") if "=" in p)
            server = int(ports.get("server_port", 0))
            if not server:
                raise RaopError("SETUP: no server_port")
            self._server_ports = (server, int(ports.get("control_port", 0)),
                                  int(ports.get("timing_port", 0)))
            _, self._audio = await loop.create_datagram_endpoint(
                lambda: _Udp(lambda d, a: None), remote_addr=(self.host, server))
            await self._request("RECORD", headers={
                "Range": "npt=0-", "RTP-Info": f"seq={self._seq};rtptime={self._rtptime}"})
        except BaseException:
            await self.stop()
            raise
        self._task = asyncio.ensure_future(self._stream())

    async def set_volume(self, level: float) -> None:
        """0.0 (mute) .. 1.0; RAOP volume is -30..0 dB, -144 is mute."""
        db = -144.0 if level <= 0 else -30.0 + 30.0 * min(level, 1.0)
        await self._request("SET_PARAMETER", body=f"volume: {db:.6f}\r\n".encode(),
                            content_type="text/parameters")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._writer is not None:
            try:
                if self._session:
                    await asyncio.wait_for(self._request("TEARDOWN"), 3)
            except (OSError, RaopError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
            self._writer.close()
            self._writer = None
        for ep in (self._audio, self._control, self._timing):
            if ep is not None and ep.transport is not None:
                ep.transport.close()
        self._audio = self._control = self._timing = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # RTP -----------------------------------------------------------------

    def feed(self, pcm: bytes) -> None:
        """Queue audio; at most 1 s is kept, older audio is dropped."""
        self._buffer += pcm
        excess = len(self._buffer) - RATE * FRAME_BYTES
        if excess > 0:
            del self._buffer[: excess - excess % FRAME_BYTES]

    def _payload(self, pcm: bytes) -> bytes:
        frame = alac_frame(pcm)
        if not self.encrypt:
            return frame
        n = len(frame) & ~0xF           # only whole blocks, the rest stays plain
        enc = Cipher(algorithms.AES(self._aes_key), modes.CBC(self._aes_iv)).encryptor()
        return enc.update(frame[:n]) + enc.finalize() + frame[n:]

    def _send_sync(self, first: bool) -> None:
        if self._control is None or not self._server_ports[1]:
            return
        now = ntp_now()
        pkt = struct.pack(">BBHIQI", 0x90 if first else 0x80, 0xD4, 7,
                          (self._rtptime - self.latency) & 0xFFFFFFFF, now, self._rtptime)
        self._control.transport.sendto(pkt, (self.host, self._server_ports[1]))

    async def _stream(self) -> None:
        """One packet per 352 frames on the wall clock; silence when no
        audio is queued, so the receiver's clock keeps running."""
        period = FRAMES / RATE
        start, count = time.monotonic(), 0
        try:
            while True:
                if count % (RATE // FRAMES) == 0:
                    self._send_sync(first=count == 0)
                chunk = bytes(self._buffer[: FRAMES * FRAME_BYTES])
                del self._buffer[: FRAMES * FRAME_BYTES]
                chunk += bytes(FRAMES * FRAME_BYTES - len(chunk))
                header = struct.pack(">BBHII", 0x80, 0xE0 if count == 0 else 0x60,
                                     self._seq, self._rtptime, self._ssrc)
                packet = header + self._payload(chunk)
                self._audio.transport.sendto(packet)
                self._sent[self._seq] = packet
                self._sent.pop((self._seq - 512) & 0xFFFF, None)
                self._seq = (self._seq + 1) & 0xFFFF
                self._rtptime = (self._rtptime + FRAMES) & 0xFFFFFFFF
                count += 1
                await asyncio.sleep(max(0.0, start + count * period - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as e:          # socket gone: reported through .error
            log.warning("AirPlay stream ended: %s", e)
            self.error = e

    def _on_control(self, data: bytes, addr) -> None:
        # Retransmit request: 0x80 0xD5, seq, first lost seq, count.
        if len(data) >= 8 and data[1] & 0x7F == 0x55:
            first, num = struct.unpack(">HH", data[4:8])
            for i in range(num):
                packet = self._sent.get((first + i) & 0xFFFF)
                if packet is not None:
                    self._control.transport.sendto(b"\x80\xd6\x00\x01" + packet, addr)

    def _on_timing(self, data: bytes, addr) -> None:
        # Timing request 0x80 0xD2: answer with its send time, our receive and send time.
        if len(data) == 32 and data[:4] == b"\x80\xd2\x00\x07":
            received = ntp_now()
            reply = b"\x80\xd3\x00\x07" + bytes(4) + data[24:32] + struct.pack(">QQ", received, ntp_now())
            self._timing.transport.sendto(reply, addr)

