"""LocalSend receiver: the HTTP(S) server side of the upload API (spec 4.x)."""

from __future__ import annotations

import logging
import secrets
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from multidrop.core import (ChecksumError, Device, EventHandler,
                            IncomingFileWriter, IncomingOffer, OfferFile,
                            OfferHandler, TransferError)

from .protocol import API, device_from_info

log = logging.getLogger(__name__)

SESSION_IDLE_TIMEOUT = 300.0
MAX_PIN_FAILURES = 5
CHUNK = 64 * 1024


@dataclass
class _Session:
    id: str
    sender_ip: str
    sender_name: str
    files: dict[str, OfferFile]
    tokens: dict[str, str]
    pending: set[str]
    in_progress: set[str] = field(default_factory=set)
    cancelled: bool = False
    last_activity: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return (not self.in_progress
                and time.monotonic() - self.last_activity > SESSION_IDLE_TIMEOUT)


def _parse_files(raw) -> list[OfferFile]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("files missing")
    out = []
    for key, f in raw.items():
        if not isinstance(f, dict):
            raise ValueError("bad file entry")
        fid, name, size = f.get("id", key), f.get("fileName"), f.get("size")
        if not isinstance(fid, str) or not isinstance(name, str):
            raise ValueError("bad file id or name")
        if not isinstance(size, int) or size < 0:
            raise ValueError("bad file size")
        mime = f.get("fileType") if isinstance(f.get("fileType"), str) else None
        sha = f.get("sha256") if isinstance(f.get("sha256"), str) else None
        preview = f.get("preview")
        text = None
        # A text message is sent as a text/plain "file" whose content is in
        # `preview`; it needs no upload.
        if (isinstance(preview, str) and mime and mime.startswith("text/")
                and len(preview.encode()) == size):
            text = preview
        out.append(OfferFile(id=fid, name=name, size=size, mime=mime,
                             sha256=sha, text=text))
    return out


class LocalSendServer:
    def __init__(self, *, info: Callable[[], dict], download_dir: Path,
                 host: str, port: int, ssl_context: ssl.SSLContext | None,
                 pin: str | None, on_offer: OfferHandler | None,
                 on_event: EventHandler, on_peer: Callable[[Device], None]):
        self.info = info
        self.download_dir = download_dir
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.pin = pin
        self.on_offer = on_offer
        self.on_event = on_event
        self.on_peer = on_peer
        self.session: _Session | None = None
        self._deciding = False
        self._pin_failures: dict[str, int] = {}
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_get(f"{API}/info", self._info)
        app.router.add_post(f"{API}/register", self._register)
        app.router.add_post(f"{API}/prepare-upload", self._prepare_upload)
        app.router.add_post(f"{API}/upload", self._upload)
        app.router.add_post(f"{API}/cancel", self._cancel)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port,
                           ssl_context=self.ssl_context)
        await site.start()
        # Resolve port 0 to the port actually bound.
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- discovery --------------------------------------------------------

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response(self.info())

    async def _register(self, request: web.Request) -> web.Response:
        try:
            device = device_from_info(await request.json(), request.remote or "")
        except ValueError:
            return web.Response(status=400)
        if device.id != self.info()["fingerprint"]:
            self.on_peer(device)
        return web.json_response(self.info())

    # -- upload API -------------------------------------------------------

    def _current_session(self) -> _Session | None:
        if self.session and (self.session.cancelled or self.session.expired()):
            self.session = None
        return self.session

    async def _prepare_upload(self, request: web.Request) -> web.Response:
        ip = request.remote or ""
        if self.pin is not None:
            if self._pin_failures.get(ip, 0) >= MAX_PIN_FAILURES:
                return web.Response(status=429)
            if request.query.get("pin") != self.pin:
                self._pin_failures[ip] = self._pin_failures.get(ip, 0) + 1
                return web.Response(status=401)
            self._pin_failures.pop(ip, None)
        try:
            body = await request.json()
            sender = device_from_info(body.get("info"), ip)
            files = _parse_files(body.get("files"))
        except (ValueError, AttributeError):
            return web.Response(status=400)
        if self._current_session() is not None or self._deciding:
            return web.Response(status=409)
        if self.on_offer is None:
            return web.Response(status=403)

        offer = IncomingOffer("localsend", sender.name, ip, files)
        self._deciding = True
        try:
            accepted = await self.on_offer(offer)
        finally:
            self._deciding = False
        chosen = [f for f in files if f.id in accepted]
        if not chosen:
            self.on_event("rejected", {"from": sender.name})
            return web.Response(status=403)

        for f in chosen:
            if f.text is not None:
                self.on_event("message", {"from": sender.name, "text": f.text})
        uploads = {f.id: f for f in chosen if f.text is None}
        if not uploads:
            return web.Response(status=204)

        session = _Session(
            id=secrets.token_urlsafe(16), sender_ip=ip, sender_name=sender.name,
            files=uploads, tokens={fid: secrets.token_urlsafe(16) for fid in uploads},
            pending=set(uploads))
        self.session = session
        self.on_event("session_started", {
            "from": sender.name, "files": len(uploads),
            "size": sum(f.size for f in uploads.values())})
        return web.json_response({"sessionId": session.id, "files": session.tokens})

    async def _upload(self, request: web.Request) -> web.Response:
        q = request.query
        sid, fid, token = q.get("sessionId"), q.get("fileId"), q.get("token")
        if not (sid and fid and token):
            return web.Response(status=400)
        session = self._current_session()
        if session is None:
            return web.Response(status=403)
        if session.id != sid:
            return web.Response(status=409)
        if request.remote != session.sender_ip or session.tokens.get(fid) != token:
            return web.Response(status=403)
        if fid not in session.pending or fid in session.in_progress:
            return web.Response(status=409)

        offer_file = session.files[fid]
        writer = IncomingFileWriter(self.download_dir, offer_file)
        session.in_progress.add(fid)
        try:
            async for chunk in request.content.iter_chunked(CHUNK):
                if session.cancelled:
                    raise TransferError("cancelled by sender")
                writer.write(chunk)
                session.last_activity = time.monotonic()
                self.on_event("progress", {"file": offer_file.name,
                                           "done": writer.received,
                                           "size": offer_file.size})
            path = writer.finish()
        except ChecksumError:
            self.on_event("file_failed", {"file": offer_file.name,
                                          "error": "sha256 mismatch"})
            return web.Response(status=422)
        except TransferError as e:
            writer.abort()
            self.on_event("file_failed", {"file": offer_file.name, "error": str(e)})
            return web.Response(status=400)
        except BaseException:
            # Connection dropped or server shutting down.
            writer.abort()
            raise
        finally:
            session.in_progress.discard(fid)

        session.pending.discard(fid)
        session.last_activity = time.monotonic()
        self.on_event("file_received", {"file": offer_file.name, "path": str(path),
                                        "from": session.sender_name})
        if not session.pending:
            self.on_event("session_done", {"from": session.sender_name})
            if self.session is session:
                self.session = None
        return web.Response(status=200)

    async def _cancel(self, request: web.Request) -> web.Response:
        session = self._current_session()
        if (session is not None and request.query.get("sessionId") == session.id
                and request.remote == session.sender_ip):
            session.cancelled = True
            self.session = None
            self.on_event("cancelled", {"from": session.sender_name})
        return web.Response(status=200)
