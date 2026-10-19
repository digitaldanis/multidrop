"""HTTP/JSON API of multidropd.

The same aiohttp app is served twice:

* on a Unix socket in $XDG_RUNTIME_DIR/multidrop (directory mode 0700), for
  local tools; only the user can connect, so no token is needed;
* on 127.0.0.1 for the web UI. There every /api request needs the token
  (header X-Multidrop-Token; EventSource cannot set headers, so /api/events
  also takes ?t=). The Host header must be 127.0.0.1/localhost with our port
  (DNS rebinding) and a foreign Origin is refused (CSRF). The static UI files
  contain no secrets; the token reaches the page in the URL fragment.

Endpoints:
  GET  /api/status                 alias, ports, accept policy
  GET  /api/devices[?refresh=1]    devices grouped by name, protocols by preference
  GET  /api/airplay[?timeout=3]     AirPlay receivers: name, ip, model, screen, audio
  GET  /api/offers                 pending incoming offers
  POST /api/offers/{id}            {"accept": true|false, "files": [ids]?}; files
                                   only where the offer's "partial" is not "none"
  GET  /api/history[?since=SEQ]    recent events (only those after SEQ)
  GET  /api/events[?since=SEQ]     Server-Sent Events (history first, then live)
  GET  /api/jobs                   send jobs
  POST /api/jobs/{id}/cancel
  GET|POST|DELETE /api/share       browser download (LocalSend download API):
                                   status / start (like send, no target; pin,
                                   no_pin, minutes, address) / stop
  GET|POST|DELETE /api/selection   files waiting to be sent from the UI: list /
                                   add {"paths": [absolute paths]} / clear
  POST /api/show                   ask the desktop app to bring its window up
                                   (event "show_ui"; second start on Windows)
  POST /api/send                   JSON {"target", "paths", "text", "pin"} or
                                   multipart: fields target (JSON), text, pin,
                                   and per file a "path" field then a "file" part
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import shutil
import socket
from pathlib import Path

from aiohttp import web

from multidrop.core import TransferError, safe_relative_path, unique_path

from .service import Daemon

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).with_name("web")
TOKEN_HEADER = "X-Multidrop-Token"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
DAEMON = web.AppKey("daemon", Daemon)
TOKEN = web.AppKey("token", str)
WEB_PORT = web.AppKey("web_port", list)   # [port], set once the TCP site runs


def _via_unix_socket(request: web.Request) -> bool:
    sock = request.transport.get_extra_info("socket") if request.transport else None
    unix = getattr(socket, "AF_UNIX", None)     # missing on Windows
    return sock is not None and unix is not None and sock.family == unix


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def guard(request: web.Request, handler):
    if not _via_unix_socket(request):
        port = request.app[WEB_PORT][0]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if request.host not in hosts:
            return _json_error(403, "bad host")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            return _json_error(403, "bad origin")
        if request.path.startswith("/api/"):
            given = request.headers.get(TOKEN_HEADER)
            if given is None and request.path == "/api/events":
                given = request.query.get("t")
            if not given or not secrets.compare_digest(given, request.app[TOKEN]):
                return _json_error(401, "token required")
    response = await handler(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


async def _static(request: web.Request) -> web.StreamResponse:
    name = request.match_info.get("name") or "index.html"
    if name not in ("index.html", "app.js", "style.css"):
        raise web.HTTPNotFound()
    resp = web.FileResponse(WEB_DIR / name)
    resp.headers["Content-Security-Policy"] = CSP
    return resp


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[DAEMON].status())


def _timeout(request: web.Request, longest: float) -> float:
    try:
        timeout = float(request.query.get("timeout", 3))
    except ValueError:
        raise web.HTTPBadRequest(text="timeout: seconds") from None
    if timeout != timeout:              # nan slips through min/max
        raise web.HTTPBadRequest(text="timeout: seconds")
    return min(max(timeout, 0.5), longest)


async def devices(request: web.Request) -> web.Response:
    d = request.app[DAEMON]
    if request.query.get("refresh"):
        await d.discover(_timeout(request, 15))
    return web.json_response(d.device_groups())


async def airplay(request: web.Request) -> web.Response:
    """AirPlay receivers and what they can do (screen, audio), from mDNS."""
    from multidrop.airplay_scan import scan
    timeout = _timeout(request, 10)
    d = request.app[DAEMON]
    return web.json_response(await scan(timeout, d.config.quickshare.mdns_addresses or None))


async def offers(request: web.Request) -> web.Response:
    return web.json_response([p.to_dict() for p in request.app[DAEMON].offers.values()])


async def answer_offer(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if not isinstance(body.get("accept"), bool):
        return _json_error(400, "accept must be true or false")
    files = body.get("files")
    if files is not None and not (isinstance(files, list)
                                  and all(isinstance(f, str) for f in files)):
        return _json_error(400, "files must be a list of file ids")
    try:
        ok = request.app[DAEMON].answer(request.match_info["id"], body["accept"], files)
    except ValueError as e:
        return _json_error(400, str(e))
    if not ok:
        return _json_error(404, "no such pending offer")
    return web.json_response({"ok": True})


async def history(request: web.Request) -> web.Response:
    try:
        since = int(request.query.get("since", -1))
    except ValueError:
        return _json_error(400, "since: an event number")
    return web.json_response([e for e in request.app[DAEMON].history if e["seq"] > since])


async def jobs(request: web.Request) -> web.Response:
    return web.json_response(list(request.app[DAEMON].jobs.values()))


async def cancel_job(request: web.Request) -> web.Response:
    if not request.app[DAEMON].cancel_job(request.match_info["id"]):
        return _json_error(404, "no such running job")
    return web.json_response({"ok": True})


async def events(request: web.Request) -> web.StreamResponse:
    d = request.app[DAEMON]
    try:
        since = int(request.query.get("since", 0) or 0)
    except ValueError:
        return _json_error(400, "since: an event number")
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-store"})
    await resp.prepare(request)
    q = d.subscribe()
    try:
        for item in list(d.history):
            if item["seq"] > since:
                await resp.write(_sse(item))
        while True:
            try:
                item = await asyncio.wait_for(q.get(), 15)
            except TimeoutError:
                await resp.write(b": keep-alive\n\n")
                continue
            if item is None:            # daemon shutting down
                break
            if item["seq"] > since:
                await resp.write(_sse(item))
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        d.unsubscribe(q)
    return resp


def _sse(item: dict) -> bytes:
    return f"id: {item['seq']}\ndata: {json.dumps(item, default=str)}\n\n".encode()


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="invalid JSON") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return body


def _paths(value) -> list[Path]:
    """A JSON "paths" value: a list of existing absolute paths (or nothing)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ValueError("paths: a list of strings")
    paths = [Path(p).expanduser() for p in value]
    for p in paths:
        if not p.is_absolute() or not p.exists():
            raise ValueError(f"not an existing absolute path: {p}")
    return paths


async def send(request: web.Request) -> web.Response:
    d = request.app[DAEMON]
    try:
        if request.content_type.startswith("multipart/"):
            job = await _send_upload(request, d)
        else:
            body = await _json_body(request)
            paths = _paths(body.get("paths"))
            if not paths and not body.get("text"):
                return _json_error(400, "nothing to send")
            job = await d.send(_target(body.get("target")), paths,
                               text=body.get("text") or None, pin=body.get("pin") or None)
    except (TransferError, ValueError, KeyError) as e:
        return _json_error(400, str(e) or type(e).__name__)
    return web.json_response({"job": job})


def _target(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("target must be an object")
    return value


async def _send_upload(request: web.Request, d: Daemon) -> str:
    """Send uploaded files; the top-level entries are sent, so dropped
    folders keep their structure."""
    job, root = d.new_upload_dir()
    try:
        fields, paths = await _receive_upload(request, root)
        if not paths and not fields.get("text"):
            raise ValueError("nothing to send")
        return await d.send(_target(fields.get("target")), paths,
                            text=fields.get("text") or None, pin=fields.get("pin") or None,
                            job=job, cleanup=root)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


async def _receive_upload(request: web.Request, root: Path) -> tuple[dict[str, str], list[Path]]:
    """Store multipart files under root; returns the other fields and the
    top-level paths."""
    fields: dict[str, str] = {}
    top: list[str] = []
    rel_name: str | None = None
    reader = await request.multipart()
    while (part := await reader.next()) is not None:
        if part.name == "file":
            rel = safe_relative_path(rel_name or part.filename or "unnamed")
            rel_name = None
            dest = unique_path(root / rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "xb") as f:
                while chunk := await part.read_chunk(1 << 20):
                    f.write(chunk)
            first = dest.relative_to(root).parts[0]
            if first not in top:
                top.append(first)
        elif part.name == "path":
            rel_name = await part.text()
        else:
            fields[part.name] = await part.text()
    return fields, [root / t for t in top]


async def share_get(request: web.Request) -> web.Response:
    return web.json_response(request.app[DAEMON].share_status())


async def share_stop(request: web.Request) -> web.Response:
    if not await request.app[DAEMON].stop_share():
        return _json_error(404, "nothing shared")
    return web.json_response({"ok": True})


async def share_start(request: web.Request) -> web.Response:
    """Like /api/send, without target; options: pin ("" = random, PIN),
    no_pin (true), minutes, https (true)."""
    d = request.app[DAEMON]
    try:
        if request.content_type.startswith("multipart/"):
            _, root = d.new_upload_dir()
            try:
                opts, paths = await _receive_upload(request, root)
                result = await d.start_share(paths, **_share_options(opts), cleanup=root)
            except BaseException:
                shutil.rmtree(root, ignore_errors=True)
                raise
        else:
            opts = await _json_body(request)
            paths = _paths(opts.get("paths"))
            if not paths:
                return _json_error(400, "nothing to share")
            result = await d.start_share(paths, **_share_options(opts))
    except (TransferError, ValueError, OSError) as e:
        return _json_error(400, str(e) or type(e).__name__)
    return web.json_response(result)


def _share_options(opts: dict) -> dict:
    no_pin = opts.get("no_pin") in (True, "1", "true")
    pin = str(opts.get("pin") or "")
    if pin and not (pin.isdigit() and 4 <= len(pin) <= 12):
        raise ValueError("PIN: 4 to 12 digits")
    try:
        minutes = float(opts.get("minutes") or 15)
    except (TypeError, ValueError):
        raise ValueError("minutes: a number") from None
    if not 0 < minutes <= 24 * 60:
        raise ValueError("minutes: more than 0, at most 1440")
    address = str(opts.get("address") or "") or None
    if address:
        try:
            ipaddress.ip_address(address)
        except ValueError:
            raise ValueError("address: an IP address") from None
    https = opts.get("https") in (True, "1", "true")
    return {"pin": None if no_pin else pin, "minutes": minutes, "address": address,
            "https": https}


async def selection_get(request: web.Request) -> web.Response:
    return web.json_response({"paths": request.app[DAEMON].selection})


async def selection_add(request: web.Request) -> web.Response:
    d = request.app[DAEMON]
    body = await _json_body(request)
    try:
        paths = _paths(body.get("paths"))
    except ValueError as e:
        return _json_error(400, str(e))
    if not paths:
        return _json_error(400, "paths: a non-empty list")
    d.select(paths)
    return web.json_response({"paths": d.selection})


async def selection_clear(request: web.Request) -> web.Response:
    request.app[DAEMON].clear_selection()
    return web.json_response({"paths": []})


async def show_ui(request: web.Request) -> web.Response:
    request.app[DAEMON].emit("show_ui")
    return web.json_response({})


def build_app(daemon: Daemon, token: str) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[DAEMON] = daemon
    app[TOKEN] = token
    app[WEB_PORT] = [0]

    async def end_streams(app):
        daemon.close_subscribers()
    app.on_shutdown.append(end_streams)
    app.add_routes([
        web.get("/", _static),
        web.get("/{name:(app\\.js|style\\.css)}", _static),
        web.get("/api/status", status),
        web.get("/api/devices", devices),
        web.get("/api/airplay", airplay),
        web.get("/api/offers", offers),
        web.post("/api/offers/{id}", answer_offer),
        web.get("/api/history", history),
        web.get("/api/events", events),
        web.get("/api/jobs", jobs),
        web.post("/api/jobs/{id}/cancel", cancel_job),
        web.post("/api/send", send),
        web.get("/api/share", share_get),
        web.post("/api/share", share_start),
        web.delete("/api/share", share_stop),
        web.get("/api/selection", selection_get),
        web.post("/api/selection", selection_add),
        web.delete("/api/selection", selection_clear),
        web.post("/api/show", show_ui),
    ])
    return app
