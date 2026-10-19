"""multidropd core: runs the backends, holds pending offers and send jobs,
and fans events out to API clients (web UI) and desktop notifications.

Front ends never talk to backends directly. They read `history`/`subscribe()`
for events, answer offers with `answer()` and start transfers with `send()`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import secrets
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from multidrop.backends import create_backends
from multidrop.config import Config
from multidrop.core import Backend, Device, IncomingOffer, PinRequired, TransferError

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 0.25   # seconds between progress events per file
HISTORY = 300
# Seconds a device stays listed after the last scan that found it.
DEVICE_TTL = 90.0

# Events written to the history file: what the user wants to see after a
# restart. Not: progress, QR codes and codes of past transfers, job_started
# (a restored job would look as if it were still running).
PERSISTED = {"offer", "offer_accepted", "offer_rejected", "offer_expired", "offer_withdrawn",
             "file_received", "message", "session_done", "failed", "file_failed", "cancelled",
             "rejected", "job_done", "job_failed", "job_cancelled", "qr_scanned",
             "share_started", "share_visit", "share_download", "share_wrong_pin",
             "share_stopped"}


@dataclass
class PendingOffer:
    id: str
    protocol: str
    offer: IncomingOffer
    future: asyncio.Future
    pin: str | None = None
    partial: str = "none"           # Backend.partial_accept
    created: float = field(default_factory=time.time)

    def to_dict(self, accepted: set[str] | None = None) -> dict:
        o = self.offer
        files = o.files if accepted is None else [f for f in o.files if f.id in accepted]
        return {"id": self.id, "protocol": self.protocol, "from": o.sender_name,
                "address": o.sender_address, "pin": self.pin, "created": self.created,
                "total_size": sum(f.size for f in files), "partial": self.partial,
                "offered": len(o.files),
                "size_unknown": not all(f.size_known for f in files),
                "files": [{"id": f.id, "name": f.name,
                           "size": f.size if f.size_known else None, "text": f.text}
                          for f in files]}


def device_key(d: Device) -> str:
    return f"{d.protocol}:{d.id}"


class Daemon:
    def __init__(self, config: Config, notifier=None,
                 backends: list[Backend] | None = None):
        self.config = config
        self.notifier = notifier
        self.backends: dict[str, Backend] = {
            b.name: b for b in (backends if backends is not None else create_backends(config))}
        self.offers: dict[str, PendingOffer] = {}
        self.jobs: dict[str, dict] = {}
        self.devices: dict[str, Device] = {}
        self._seen: dict[str, float] = {}       # device key -> monotonic time last found
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self.outgoing_dir = config.state_dir / "outgoing"
        self._subscribers: set[asyncio.Queue] = set()
        self._history_file = config.daemon.history_path() if config.daemon.history else None
        self._history_lines = 0
        self._load_history()
        self._seq = itertools.count(max((e["seq"] for e in self.history), default=0) + 1)
        self._pins: dict[tuple[str, str], str] = {}
        self._last_progress: dict[tuple, float] = {}
        self._discovery: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._job_tasks: dict[str, asyncio.Task] = {}
        self.share = None                  # running WebShare, if any
        self.selection: list[str] = []     # files handed over to send from the UI
        self._share_cleanup: Path | None = None

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.outgoing_dir, ignore_errors=True)  # leftovers of a crash
        failed = {}
        for name, b in list(self.backends.items()):
            try:
                await b.start(self._offer_handler(name), self._event_handler(name))
                log.info("%s listening on port %s", name, b.port)
            except (TransferError, OSError) as e:
                # One protocol failing (e.g. AirDrop without awdl0) must not
                # take the others down.
                log.warning("%s not started: %s", name, e)
                failed[name] = str(e)
                del self.backends[name]
        if not self.backends:
            raise TransferError("no protocol could be started: " + "; ".join(failed.values()))
        self.emit("started", protocols=self.status()["protocols"], failed=failed)

    async def stop(self) -> None:
        await self.stop_share("daemon stopped")
        for t in list(self._tasks):
            t.cancel()
        for p in list(self.offers.values()):
            if not p.future.done():
                p.future.set_result(set())
        for b in self.backends.values():
            try:
                await b.stop()
            except Exception as e:   # keep stopping the others
                log.debug("stop %s: %s", b.name, e)

    def status(self) -> dict:
        return {"alias": self.config.alias,
                "download_dir": str(self.config.download_dir),
                "accept": self.config.daemon.accept,
                "prefer": self.config.prefer,
                "protocols": {n: {"port": b.port} for n, b in self.backends.items()}}

    def select(self, paths: list[Path]) -> None:
        """Add files to send (Windows Explorer's "Send to"); the UI picks the device."""
        self.selection += [str(p) for p in paths if str(p) not in self.selection]
        self.emit("selection", count=len(self.selection))

    def clear_selection(self) -> None:
        self.selection.clear()
        self.emit("selection", count=0)

    # -- history file -----------------------------------------------------

    def _load_history(self) -> None:
        """Last HISTORY persisted events; continues their seq numbers so
        clients' `since` keeps working."""
        path = self._history_file
        if path is None or not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # torn last line after a crash
                    if isinstance(item, dict) and isinstance(item.get("seq"), int):
                        self.history.append({**item, "restored": True})
        except OSError as e:
            log.warning("history %s not readable: %s", path, e)
            return
        self._rewrite_history()

    @staticmethod
    def _stored_form(item: dict) -> dict | None:
        if item["event"] not in PERSISTED:
            return None
        drop = {"restored"} | ({"url"} if item["event"] == "share_started" else set())
        return {k: v for k, v in item.items() if k not in drop}   # url: holds the secret key

    def _rewrite_history(self) -> None:
        path = self._history_file
        lines = [json.dumps(s, default=str) + "\n"
                 for s in map(self._stored_form, self.history) if s is not None]
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, path)
            self._history_lines = len(lines)
        except OSError as e:
            log.warning("history %s not writable: %s", path, e)
            self._history_file = None

    def _persist(self, item: dict) -> None:
        stored = self._stored_form(item)
        if self._history_file is None or stored is None:
            return
        if self._history_lines >= 2 * HISTORY:
            self._rewrite_history()          # the deque already holds the item
            return
        try:
            if not self._history_lines:
                self._history_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self._history_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(stored, default=str) + "\n")
            self._history_lines += 1
        except OSError as e:
            log.warning("history %s not writable: %s", self._history_file, e)
            self._history_file = None

    # -- events -----------------------------------------------------------

    def emit(self, event: str, **data) -> None:
        item = {"seq": next(self._seq), "time": time.time(), "event": event, **data}
        if event != "progress":
            self.history.append(item)
            self._persist(item)
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:   # a stuck client; it reconnects with history
                self._subscribers.discard(q)
        if self.notifier is not None:
            self.notifier.on_event(item)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def close_subscribers(self) -> None:
        """End all event streams (None = end of stream)."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subscribers.clear()

    def _forward(self, protocol: str, event: str, d: dict, job: str | None = None) -> None:
        if event == "progress":
            key = (protocol, job, d.get("file"))
            now = time.monotonic()
            if d.get("done") != d.get("size") and \
                    now - self._last_progress.get(key, 0) < PROGRESS_INTERVAL:
                return
            self._last_progress[key] = now
        if event == "qr":
            from multidrop.backends.quickshare.qr import svg_qr
            d = {**d, "svg": svg_qr(d["url"])}
        if event == "pin" and "from" in d:
            self._pins[(protocol, d["from"])] = d["pin"]
        extra = {"job": job} if job else {}
        self.emit(event, protocol=protocol, **extra, **{k: _jsonable(v) for k, v in d.items()})

    def _event_handler(self, protocol: str):
        return lambda event, d: self._forward(protocol, event, d)

    # -- incoming offers --------------------------------------------------

    def _offer_handler(self, protocol: str):
        async def handle(offer: IncomingOffer) -> set[str]:
            return await self._handle_offer(protocol, offer)
        return handle

    async def _handle_offer(self, protocol: str, offer: IncomingOffer) -> set[str]:
        everything = {f.id for f in offer.files}
        backend = self.backends.get(protocol)
        pending = PendingOffer(secrets.token_hex(6), protocol, offer,
                               asyncio.get_running_loop().create_future(),
                               self._pins.pop((protocol, offer.sender_name), None),
                               getattr(backend, "partial_accept", "none"))
        if self.config.daemon.accept == "all":
            self.emit("offer_accepted", offer=pending.to_dict(), auto=True)
            return everything
        self.offers[pending.id] = pending
        self.emit("offer", offer=pending.to_dict())
        accepted: set[str] = set()
        try:
            accepted = await asyncio.wait_for(asyncio.shield(pending.future),
                                              self.config.daemon.ask_timeout)
        except TimeoutError:
            self.emit("offer_expired", id=pending.id)
        except asyncio.CancelledError:      # the sender gave up
            self.emit("offer_withdrawn", id=pending.id)
            raise
        finally:
            self.offers.pop(pending.id, None)
        if accepted:
            self.emit("offer_accepted", id=pending.id, offer=pending.to_dict(accepted),
                      partial=accepted != everything)
        else:
            self.emit("offer_rejected", id=pending.id, offer=pending.to_dict())
        return accepted

    def answer(self, offer_id: str, accept: bool, files: list[str] | None = None) -> bool:
        """Accept or reject; `files` (ids) accepts only those, where the
        protocol allows it (PendingOffer.partial != "none")."""
        p = self.offers.get(offer_id)
        if p is None or p.future.done():
            return False
        everything = {f.id for f in p.offer.files}
        chosen = everything if files is None else set(map(str, files))
        if not chosen <= everything:
            raise ValueError("unknown file id")
        if accept and chosen != everything and p.partial == "none":
            raise ValueError(f"{p.protocol}: only all files or none")
        p.future.set_result(chosen if accept else set())
        return True

    # -- discovery --------------------------------------------------------

    async def discover(self, timeout: float = 3.0) -> list[Device]:
        """Concurrent callers share one discovery run."""
        if self._discovery is None or self._discovery.done():
            self._discovery = asyncio.ensure_future(self._discover(timeout))
        return await asyncio.shield(self._discovery)

    async def _discover(self, timeout: float) -> list[Device]:
        async def one(b: Backend) -> list[Device]:
            try:
                return await b.discover(timeout)
            except Exception as e:
                log.warning("%s discovery failed: %s", b.name, e)
                return []
        found = [d for r in await asyncio.gather(*(one(b) for b in self.backends.values()))
                 for d in r]
        self._merge(found)
        return found

    def _merge(self, found: list[Device], now: float | None = None) -> None:
        """Phones answer only some scans (their mDNS answer comes a few seconds
        after the BLE wake-up and stops again), so a device stays listed until
        it has been missing for DEVICE_TTL instead of vanishing after one miss."""
        now = time.monotonic() if now is None else now
        for d in found:
            key = device_key(d)
            self.devices[key] = d
            self._seen[key] = now
        for key in [k for k, t in self._seen.items() if now - t > DEVICE_TTL]:
            self._seen.pop(key, None)
            self.devices.pop(key, None)

    def device_groups(self) -> list[dict]:
        """Devices by name; per name the protocols in `prefer` order."""
        rank = {p: i for i, p in enumerate(self.config.prefer)}
        groups: dict[str, list[Device]] = {}
        for d in self.devices.values():
            groups.setdefault(d.name.lower(), []).append(d)
        out = []
        for ds in groups.values():
            ds.sort(key=lambda d: rank.get(d.protocol, len(rank)))
            out.append({"name": ds[0].name, "type": ds[0].device_type,
                        "targets": [{"key": device_key(d), "protocol": d.protocol,
                                     "id": d.id, "model": d.model,
                                     "address": d.address, "port": d.port} for d in ds]})
        return sorted(out, key=lambda g: g["name"].lower())

    # -- sending ----------------------------------------------------------

    async def resolve_target(self, target: dict) -> tuple[Backend, Device | None]:
        """target: {"key": ...} from device_groups, {"protocol", "address",
        "port"}, or {"qr": true} (Quick Share QR code, device found later).
        Without "protocol" an address is tried as LocalSend, then (only
        with a port) as Quick Share, like `multidrop send --to IP[:PORT]`."""
        if target.get("qr"):
            return self._backend("quickshare"), None
        if "key" in target:
            device = self.devices.get(target["key"])
            if device is None:
                raise TransferError("unknown device, search again")
            return self._backend(device.protocol), device
        port = int(target["port"]) if target.get("port") else None
        address = str(target["address"])
        if target.get("protocol"):
            backend = self._backend(target["protocol"])
            return backend, await backend.resolve(address, port)
        errors = []
        for name in ("localsend", "quickshare") if port else ("localsend",):
            if name in self.backends:
                try:
                    return self.backends[name], await self.backends[name].resolve(address, port)
                except TransferError as e:
                    errors.append(str(e))
        raise TransferError("; ".join(errors) or f"nothing reachable at {address}")

    def _backend(self, name: str) -> Backend:
        if name not in self.backends:
            raise TransferError(f"protocol not available: {name or '?'}")
        return self.backends[name]

    def new_upload_dir(self) -> tuple[str, Path]:
        job = secrets.token_hex(6)
        d = self.outgoing_dir / job
        d.mkdir(parents=True, mode=0o700)
        return job, d

    async def send(self, target: dict, paths: list[Path], text: str | None = None,
                   pin: str | None = None, job: str | None = None,
                   cleanup: Path | None = None) -> str:
        """Start a send job in the background; returns its id."""
        try:
            backend, device = await self.resolve_target(target)
        except Exception:
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
            raise
        job = job or secrets.token_hex(6)
        info = {"id": job, "protocol": backend.name,
                "to": device.name if device else "QR",
                "items": [p.name for p in paths] + (["(text)"] if text else []),
                "state": "running"}
        self.jobs[job] = info
        task = asyncio.ensure_future(self._run_job(job, backend, device, paths, text,
                                                   pin, cleanup))
        self._tasks.add(task)
        self._job_tasks[job] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._job_tasks.pop(job, None))
        self.emit("job_started", job=job, info=dict(info))
        return job

    async def _run_job(self, job, backend, device, paths, text, pin, cleanup) -> None:
        def on_event(event, d):
            self._forward(backend.name, event, d, job)
        try:
            if device is None:
                found = await backend.send_qr(paths, on_event, text=text)
                self.jobs[job]["to"] = found.name
            else:
                kwargs = {"text": text}
                if backend.name == "localsend":
                    kwargs["pin"] = pin
                await backend.send(device, paths, on_event, **kwargs)
            self.jobs[job]["state"] = "done"
            self.emit("job_done", job=job, info=dict(self.jobs[job]))
        except (TransferError, OSError) as e:
            self.jobs[job]["state"] = "failed"
            extra = {"pin_required": True} if isinstance(e, PinRequired) else {}
            self.emit("job_failed", job=job, error=str(e), info=dict(self.jobs[job]), **extra)
        except asyncio.CancelledError:
            self.jobs[job]["state"] = "cancelled"
            self.emit("job_cancelled", job=job, info=dict(self.jobs[job]))
            raise
        finally:
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)

    def cancel_job(self, job: str) -> bool:
        task = self._job_tasks.get(job)
        if task is None or task.done():
            return False
        task.cancel()
        return True


    # -- browser download (LocalSend download API) ------------------------

    async def start_share(self, paths: list[Path], pin: str | None = "",
                          minutes: float = 15, cleanup: Path | None = None,
                          address: str | None = None, https: bool = False) -> dict:
        """Share files for download in a browser; replaces a running share."""
        from multidrop.backends.localsend.webshare import WebShare
        from multidrop.backends.quickshare.qr import svg_qr
        await self.stop_share("replaced")
        try:
            share = WebShare(paths, alias=self.config.alias, pin=pin, minutes=minutes,
                             port=self.config.localsend.share_port,
                             host=self.config.localsend.share_bind,
                             public_address=address, https=https,
                             on_event=lambda e, d: self._share_event(share, e, d))
            await share.start()
        except BaseException:
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
            raise
        self.share, self._share_cleanup = share, cleanup
        return {**share.status(), "svg": svg_qr(share.url)}

    def share_status(self) -> dict | None:
        if self.share is None:
            return None
        from multidrop.backends.quickshare.qr import svg_qr
        return {**self.share.status(), "svg": svg_qr(self.share.url)}

    async def stop_share(self, reason: str = "stopped") -> bool:
        if self.share is None:
            return False
        await self.share.stop(reason)
        return True

    def _share_event(self, share, event: str, d: dict) -> None:
        if event == "share_stopped" and share is self.share:
            self.share = None
            if self._share_cleanup:
                shutil.rmtree(self._share_cleanup, ignore_errors=True)
                self._share_cleanup = None
        if event in ("share_started", "share_moved"):
            d = {k: v for k, v in d.items() if k != "pin"}   # PIN only via API
        self.emit(event, protocol="localsend", **{k: _jsonable(v) for k, v in d.items()})


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)
