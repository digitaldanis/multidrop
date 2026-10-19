"""AirDrop backend (protocol side). Needs an AWDL interface (awdl0, provided
by OWL as root); without it start() fails with a clear message. Everything
above the radio is tested on loopback (tests/test_airdrop_loopback.py).

Based on omdrop-plugin by Brent Kearney (MIT), see THIRD_PARTY_NOTICES.md."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import tempfile
from pathlib import Path

import aiohttp

from multidrop.config import Config
from multidrop.core import (Backend, Device, EventHandler, OfferHandler, TransferError,
                            _noop_event)

from . import client
from .server import AirDropServer

log = logging.getLogger(__name__)
WAKE_TIMEOUT = 15.0
WAKE_RETRY = 0.5


def interface_address(name: str) -> str | None:
    """IPv6 link-local address of an interface as "fe80::…%name", or None."""
    import ifaddr
    for adapter in ifaddr.get_adapters():
        if adapter.name == name:
            for ip in adapter.ips:
                if isinstance(ip.ip, tuple) and ip.ip[0].startswith("fe80:"):
                    return f"{ip.ip[0]}%{name}"
    return None



def text_file_name(text: str) -> str:
    """"Shopping list.txt" for a text starting with "Shopping list\n…": the
    receiver sees a file (AirDrop knows only files and links), so name it
    after its first line."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    name = "".join(c for c in first if c.isprintable() and c not in '/\\:*?"<>|').strip(" .")
    if len(name) > 40:
        name = name[:40].rsplit(" ", 1)[0] if " " in name[:40] else name[:40]
    return f"{name.strip(' .') or 'Text'}.txt"

class AirDropBackend(Backend):
    name = "airdrop"
    partial_accept = "none"      # one archive for all files

    def __init__(self, config: Config):
        from multidrop.backends.localsend.identity import Identity
        self.config = config
        self.ad = config.airdrop
        state = config.state_dir / "airdrop"
        self.identity = Identity.load_or_create(state, common_name="multidrop")
        id_file = state / "service-id"
        if self.ad.service_id == "mac":
            mac = Path(f"/sys/class/net/{self.ad.interface}/address")
            self.service_id = mac.read_text().strip().replace(":", "") if mac.exists() else ""
        elif self.ad.service_id:
            self.service_id = self.ad.service_id
        else:
            if not id_file.exists():
                id_file.write_text(f"{secrets.randbits(48):012x}\n")
            self.service_id = id_file.read_text().strip()
        self.server: AirDropServer | None = None
        self._publisher = None
        self._http: aiohttp.ClientSession | None = None
        self._ble = None
        self._ble_users = 0

    @contextlib.asynccontextmanager
    async def _wake(self):
        """BLE AirDrop advertisement while discovering/sending, so iPhones
        publish themselves; shared by concurrent users. Failing BLE is only
        a warning (the receiver may be awake anyway)."""
        if not self.ad.ble:
            yield
            return
        self._ble_users += 1
        try:
            if self._ble is None:
                from . import ble
                adv = ble.advertiser(self.ad.ble_shape, self.ad.ble_connectable)
                try:
                    await adv.start()
                    self._ble = adv
                except ble.BleError as e:
                    log.warning("AirDrop wake-up: %s", e)
            yield
        finally:
            self._ble_users -= 1
            if self._ble_users == 0 and self._ble is not None:
                ble_adv, self._ble = self._ble, None
                await ble_adv.stop()

    @property
    def port(self) -> int:
        return self.server.port if self.server else self.ad.port

    def _bind(self) -> str:
        if self.ad.bind:
            return self.ad.bind
        addr = interface_address(self.ad.interface)
        if addr is None:
            raise TransferError(
                f"AirDrop: no interface {self.ad.interface} (the AWDL radio is not "
                "running, see docs/INSTALL-ROOT.md); AirDrop stays off")
        return addr

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
                ssl=self.identity.client_ssl_context(), force_close=True))
        return self._http

    async def start(self, on_offer: OfferHandler | None = None,
                    on_event: EventHandler = _noop_event) -> None:
        host = self._bind()
        server = AirDropServer(name=self.config.alias, model=self.ad.model,
                               download_dir=self.config.download_dir, host=host,
                               port=self.ad.port, ssl_context=self.identity.server_ssl_context(),
                               on_offer=on_offer, on_event=on_event)
        await server.start()
        self.server = server
        if self.ad.mdns:
            from .mdns import Publisher
            self._publisher = Publisher(self.ad.mdns_addresses or [host],
                                        interval=self.ad.announce_interval,
                                        burst=self.ad.announce_burst)
            await self._publisher.start(self.service_id, server.port)

    async def stop(self) -> None:
        if self._publisher is not None:
            await self._publisher.stop()
            self._publisher = None
        if self.server is not None:
            await self.server.stop()
            self.server = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def discover(self, timeout: float = 4.0) -> list[Device]:
        from .mdns import browse
        addresses = self.ad.mdns_addresses or [self._bind()]
        scope = None if self.ad.mdns_addresses else self.ad.interface
        async with self._wake():
            found = await browse(timeout, addresses, scope, exclude=self.service_id)
            # Keep the phone awake through the HTTPS handshake as well.
            async def ask(f) -> Device | None:
                try:
                    answer = await client.discover(self.http, f.address, f.port)
                except (aiohttp.ClientError, OSError, TransferError, asyncio.TimeoutError):
                    return None
                return Device("airdrop", f.service_id,
                              str(answer.get("ReceiverComputerName") or f.service_id),
                              f.address, f.port, model=answer.get("ReceiverModelName"),
                              device_type="apple")
            devices = [d for d in await asyncio.gather(*(ask(f) for f in found)) if d]
        return sorted(devices, key=lambda d: d.name.lower())

    async def resolve(self, address: str, port: int | None = None) -> Device:
        port = port or 8771
        async with self._wake():
            answer = await self._ready(address, port)
        name = str(answer.get("ReceiverComputerName") or address)
        return Device("airdrop", f"{address}:{port}", name, address, port,
                      model=answer.get("ReceiverModelName"), device_type="apple")

    async def _ready(self, address: str, port: int) -> dict:
        """BLE wake-up is asynchronous; retry only the read-only handshake.

        Macs close their listener while asleep, and need time after advertising
        starts. Never retry Ask/Upload automatically: that could send twice.
        """
        deadline = asyncio.get_running_loop().time() + WAKE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                async with asyncio.timeout(max(0, remaining)):
                    return await client.discover(self.http, address, port)
            except (aiohttp.ClientError, OSError, TimeoutError) as e:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TransferError(
                        f"AirDrop receiver at {address}:{port} did not wake up; "
                        "open AirDrop on the receiving device and enable Everyone, "
                        f"then search again ({e})") from None
                await asyncio.sleep(min(WAKE_RETRY, max(0, deadline - asyncio.get_running_loop().time())))

    async def send(self, device: Device, paths: list[Path],
                   on_event: EventHandler = _noop_event, text: str | None = None) -> None:
        urls: list[str] = []
        tmp = None
        if text is not None:
            if text.startswith(("http://", "https://")) and not paths:
                urls = [text]
            else:   # AirDrop has no plain-text type: send it as a .txt file
                tmp = tempfile.TemporaryDirectory()
                note = Path(tmp.name) / text_file_name(text)
                note.write_bytes(text.encode())     # as typed, no CRLF on Windows
                paths = [*paths, note]
        try:
            async with self._wake():
                receiver = await self._ready(device.address, device.port)
                # sharingd retires the Discover handler after its response.
                # Ask needs a NEW connection; Upload must reuse the Ask one.
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(
                        ssl=self.identity.client_ssl_context(), limit=1)) as transfer:
                    await client.send(transfer, device.address, device.port,
                                      our_name=self.config.alias, sender_id=self.service_id,
                                      model=self.ad.model, paths=paths, urls=urls,
                                      on_event=on_event,
                                      target=receiver.get("ReceiverComputerName") or device.name)
        finally:
            if tmp is not None:
                tmp.cleanup()


__all__ = ["AirDropBackend"]
