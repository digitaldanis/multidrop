"""Browser download: PC -> any phone without an app (LocalSend v2.2 section 5,
"Download API").

Runs only while the user explicitly shares something and stops by itself
after `minutes`. Plain HTTP on its own port by default, because browsers warn
about self-signed certificates. With `https=True` it uses a throwaway
certificate for the share's address instead: the browser warns once. iOS
refuses untrusted certificates on local IP addresses (#27), so this does not
help iPhones set to refuse plain HTTP. Protection, beyond the spec:

* a random key in the URL (`?k=`); every route needs it, so the port alone
  reveals nothing, and the key travels via QR code or by reading it off;
* a PIN (default: random 4 digits) asked for on the page, or `?pin=` on
  prepare-download as in the spec;
* after 5 wrong PINs an address gets 429, after 30 in total the share stops;
* sessions are random 128-bit ids handed out after the PIN.

Routes (all need ?k=KEY):
  GET  /                                 page: PIN form or file list
  POST /unlock                           form: pin -> redirect to /?s=SESSION
  POST /api/localsend/v2/prepare-download  spec: ?pin= &sessionId= -> JSON
  GET  /api/localsend/v2/download        spec: ?sessionId= &fileId=
  GET  /zip                              all files as one ZIP (?s=SESSION)
"""

from __future__ import annotations

import asyncio
import datetime
import errno
import html
import ipaddress
import logging
import mimetypes
import secrets
import ssl
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from multidrop.core import EventHandler, _noop_event, collect_files

log = logging.getLogger(__name__)

DEFAULT_PORT = 53320
MAX_FAILS_PER_IP = 5
MAX_FAILS_TOTAL = 30


@dataclass
class SharedFile:
    id: str
    path: Path
    name: str          # relative name, "dir/file.jpg" for folders
    size: int
    mime: str


class WebShare:
    def __init__(self, paths: list[Path], *, alias: str, pin: str | None = "",
                 minutes: float = 15, host: str = "0.0.0.0", port: int = DEFAULT_PORT,
                 public_address: str | None = None, https: bool = False,
                 on_event: EventHandler = _noop_event):
        """pin: "" = generate 4 digits, None = no PIN."""
        self.files: dict[str, SharedFile] = {}
        for path, name in collect_files(paths):
            fid = secrets.token_hex(8)
            self.files[fid] = SharedFile(fid, path, name, path.stat().st_size,
                                         mimetypes.guess_type(name)[0]
                                         or "application/octet-stream")
        self.alias = alias
        self.pin = f"{secrets.randbelow(10000):04d}" if pin == "" else pin
        self.key = secrets.token_urlsafe(9)
        self.minutes = minutes
        self.host, self.port = host, port
        self.public_address = public_address
        self.https = https
        self.on_event = on_event
        self.sessions: dict[str, str] = {}        # session id -> client address
        self.fails: dict[str, int] = {}
        self.downloads = 0
        self.expires = 0.0
        self.stopped = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._stopping: asyncio.Task | None = None     # held so it isn't garbage collected
        self._follow: asyncio.Future | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.public_address or self.host}:{self.port}/?k={self.key}"

    async def start(self) -> None:
        app = web.Application(middlewares=[self._guard])
        app.add_routes([
            web.get("/", self._page),
            web.post("/unlock", self._unlock),
            web.post("/api/localsend/v2/prepare-download", self._prepare),
            web.get("/api/localsend/v2/download", self._download),
            web.get("/zip", self._zip),
        ])
        follow = self.public_address is None and self.host == "0.0.0.0"
        if self.public_address is None:
            self.public_address = _current_address() if follow else self.host
        self._runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await self._runner.setup()
        try:
            await self._listen(self.port)
        except OSError as e:
            if e.errno != errno.EADDRINUSE or not self.port:
                await self._runner.cleanup()
                raise
            await self._listen(0)
            log.warning("share port %d is in use, using a random port", self.port)
        self.expires = time.time() + self.minutes * 60
        self._timer = asyncio.get_running_loop().call_later(
            self.minutes * 60, self._stop_soon, "expired")
        if follow:
            self._follow = asyncio.ensure_future(self._follow_network())
        self.on_event("share_started", self.status())

    async def _listen(self, port: int) -> None:
        tls = _throwaway_tls(self.public_address) if self.https else None
        self._site = web.TCPSite(self._runner, self.host, port, ssl_context=tls)
        await self._site.start()
        self.port = next(a[1] for a in self._runner.addresses)

    async def _follow_network(self, interval: float = 3.0) -> None:
        """Another Wi-Fi gives the PC another address: move the link (and the
        certificate, which names the address) along, so the QR code stays right."""
        while True:
            await asyncio.sleep(interval)
            address = _current_address()
            if address == self.public_address or address.startswith("127."):
                continue
            log.info("share moves to %s", address)
            self.public_address = address
            if self.https:
                await self._site.stop()
                try:
                    await self._listen(self.port)
                except OSError as e:        # port taken meanwhile: not listening anymore
                    log.warning("share cannot move to %s: %s", address, e)
                    self._stop_soon("network changed")
                    return
            self.on_event("share_moved", self.status())

    def _stop_soon(self, reason: str) -> None:
        """Stop from a callback or a request handler, once."""
        if self._stopping is None:
            self._stopping = asyncio.ensure_future(self.stop(reason))

    async def stop(self, reason: str = "stopped") -> None:
        if self._runner is None:
            return
        runner, self._runner = self._runner, None
        if self._timer:
            self._timer.cancel()
        if self._follow:
            self._follow.cancel()
        await runner.cleanup()
        self.stopped.set()
        self.on_event("share_stopped", {"reason": reason, "downloads": self.downloads})

    def status(self) -> dict:
        return {"url": self.url, "pin": self.pin, "expires": self.expires,
                "files": [f.name for f in self.files.values()],
                "size": sum(f.size for f in self.files.values()),
                "downloads": self.downloads, "running": self._runner is not None}

    # -- access control ---------------------------------------------------

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if not secrets.compare_digest(request.query.get("k", ""), self.key):
            return web.Response(status=404, text="Not found")
        resp = await handler(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    def _client(self, request: web.Request) -> str:
        return request.remote or "?"

    def _check_pin(self, request: web.Request, pin: str | None) -> web.Response | None:
        ip = self._client(request)
        if self.fails.get(ip, 0) >= MAX_FAILS_PER_IP:
            return web.Response(status=429, text="Too many requests")
        if self.pin is None or (pin is not None and secrets.compare_digest(pin, self.pin)):
            return None
        if pin is not None:
            self.fails[ip] = self.fails.get(ip, 0) + 1
            self.on_event("share_wrong_pin", {"from": ip})
            if sum(self.fails.values()) >= MAX_FAILS_TOTAL:
                self._stop_soon("too many wrong PINs")
        return web.Response(status=401, text="PIN required" if pin is None else "Invalid PIN")

    def _new_session(self, request: web.Request) -> str:
        sid = secrets.token_urlsafe(16)
        self.sessions[sid] = self._client(request)
        self.on_event("share_visit", {"from": self._client(request)})
        return sid

    def _session_ok(self, sid: str | None) -> bool:
        return bool(sid) and sid in self.sessions

    # -- spec API ---------------------------------------------------------

    async def _prepare(self, request: web.Request) -> web.Response:
        sid = request.query.get("sessionId")
        if not self._session_ok(sid):
            denied = self._check_pin(request, request.query.get("pin"))
            if denied:
                return denied
            sid = self._new_session(request)
        return web.json_response({
            "info": {"alias": self.alias, "version": "2.1", "deviceModel": "Linux",
                     "deviceType": "desktop", "fingerprint": "", "download": True},
            "sessionId": sid,
            "files": {f.id: {"id": f.id, "fileName": f.name, "size": f.size,
                             "fileType": f.mime, "sha256": None, "preview": None}
                      for f in self.files.values()}})

    async def _download(self, request: web.Request) -> web.StreamResponse:
        if not self._session_ok(request.query.get("sessionId")):
            return web.Response(status=403, text="Rejected")
        f = self.files.get(request.query.get("fileId", ""))
        if f is None:
            return web.Response(status=404, text="Not found")
        if not request.headers.get("Range"):
            self.downloads += 1
            self.on_event("share_download", {"from": self._client(request), "file": f.name})
        resp = web.FileResponse(f.path, headers={
            "Content-Type": f.mime,
            "Content-Disposition": _attachment(f.name.rsplit("/", 1)[-1])})
        return resp

    # -- browser page -----------------------------------------------------

    async def _unlock(self, request: web.Request) -> web.Response:
        form = await request.post()
        denied = self._check_pin(request, str(form.get("pin", "")).strip())
        if denied is not None:
            if denied.status == 429:
                return denied
            return _html(self._pin_page(error=True), 401)
        raise web.HTTPSeeOther(f"/?k={self.key}&s={self._new_session(request)}")

    async def _page(self, request: web.Request) -> web.Response:
        sid = request.query.get("s")
        if not self._session_ok(sid):
            if self.pin is not None:
                return _html(self._pin_page())
            sid = self._new_session(request)
        rows = []
        for f in self.files.values():
            href = (f"/api/localsend/v2/download?k={self.key}&sessionId={quote(sid)}"
                    f"&fileId={f.id}")
            rows.append(f'<li><a href="{html.escape(href)}" download="'
                        f'{html.escape(f.name.rsplit("/", 1)[-1])}">'
                        f'{html.escape(f.name)}</a> <small>{_human(f.size)}</small></li>')
        zip_link = (f'<p><a class="btn" href="/zip?k={self.key}&amp;s={quote(sid)}">'
                    f'Download all as ZIP</a></p>' if len(self.files) > 1 else "")
        return _html(_PAGE.format(
            title=html.escape(f"Files from {self.alias}"),
            body=f'<ul>{"".join(rows)}</ul>{zip_link}'))

    def _pin_page(self, error: bool = False) -> str:
        msg = '<p class="err">Wrong PIN.</p>' if error else ""
        return _PAGE.format(title=html.escape(f"Files from {self.alias}"), body=(
            f'{msg}<form method="post" action="/unlock?k={self.key}">'
            '<label>PIN (shown on the computer)<br><input name="pin" inputmode="numeric" '
            'autocomplete="off" autofocus required></label> '
            '<button>Open</button></form>'))

    async def _zip(self, request: web.Request) -> web.StreamResponse:
        if not self._session_ok(request.query.get("s")):
            return web.Response(status=403, text="Rejected")
        resp = web.StreamResponse(headers={
            "Content-Type": "application/zip",
            "Content-Disposition": _attachment(f"{self.alias}.zip")})
        await resp.prepare(request)
        self.downloads += 1
        self.on_event("share_download", {"from": self._client(request), "file": "(ZIP)"})
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
        cancelled = threading.Event()
        files = list(self.files.values())

        class Pipe:      # unseekable sink: zipfile then writes data descriptors
            def write(self, b):
                if cancelled.is_set():
                    raise OSError("client gone")
                asyncio.run_coroutine_threadsafe(queue.put(bytes(b)), loop).result()
                return len(b)

            def flush(self):
                pass

        def produce():
            try:
                with zipfile.ZipFile(Pipe(), "w", zipfile.ZIP_STORED) as zf:
                    for f in files:
                        zf.write(f.path, f.name)
            except OSError:
                pass
            finally:
                if not cancelled.is_set():
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        producer = loop.run_in_executor(None, produce)
        try:
            while (chunk := await queue.get()) is not None:
                await resp.write(chunk)
        finally:
            cancelled.set()
            while not queue.empty():        # unblock a waiting producer
                queue.get_nowait()
            await producer
        return resp


def _attachment(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


def _current_address() -> str:
    from multidrop.backends.quickshare.mdns import default_address
    return default_address()


def _throwaway_tls(address: str) -> ssl.SSLContext:
    """Self-signed certificate for this share only, valid for its address."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    # iOS drops the connection, without a warning to click through, unless the
    # certificate meets Apple's TLS rules: SAN, serverAuth EKU, validity <= 825 days.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MultiDrop")])
    try:
        san = x509.IPAddress(ipaddress.ip_address(address))
    except ValueError:
        san = x509.DNSName(address)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False,
                                         decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # load_cert_chain only reads files; the key never outlives this call.
    with tempfile.TemporaryDirectory() as d:
        cert_file, key_file = Path(d, "cert.pem"), Path(d, "key.pem")
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        ctx.load_cert_chain(cert_file, key_file)
    return ctx


def _html(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/html", headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                   "form-action 'self'; frame-ancestors 'none'"})


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{title}</title>
<style>body{{font:17px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}}
li{{margin:.6rem 0}} a{{color:#1f6feb}} small{{color:#777}} .err{{color:#c62828}}
input,button{{font:inherit;padding:.4rem .7rem}} .btn{{display:inline-block;padding:.5rem 1rem;
background:#1f6feb;color:#fff;border-radius:6px;text-decoration:none}}</style></head>
<body><h1>{title}</h1>{body}<p><small>MultiDrop · LocalSend download</small></p></body></html>"""
