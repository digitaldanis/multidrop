"""Command line: multidrop devices | send | receive | share | info | ui.

When multidropd runs, devices/send/receive/share are done through its Unix
socket (multidrop.cli_daemon); `--direct` or no daemon: in this process."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import signal
import sys
import time
from pathlib import Path

from multidrop import __version__
from multidrop.backends import PROTOCOLS, create_backends
from multidrop.config import WINDOWS, Config
from multidrop.core import Backend, Device, IncomingOffer, TransferError



def _human(n: int | None) -> str:
    if n is None:
        return "size unknown"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _load_config(args) -> Config:
    cfg = Config.load(Path(args.config) if args.config else None)
    if args.alias:
        cfg.alias = args.alias
    if args.port:
        cfg.localsend.port = args.port
    if args.multicast_port:
        cfg.localsend.multicast_port = args.multicast_port
    if args.http:
        cfg.localsend.https = False
    if args.multicast_interface:
        cfg.localsend.multicast_interface = args.multicast_interface
    if args.qs_port is not None:
        cfg.quickshare.port = args.qs_port
    if args.mdns_address:
        cfg.quickshare.mdns_addresses = [args.mdns_address]
    if args.no_ble:
        cfg.quickshare.ble = False
    if getattr(args, "dir", None):
        cfg.download_dir = Path(args.dir).expanduser()
    if getattr(args, "pin", None) and args.command == "receive":
        cfg.localsend.pin = args.pin
    return cfg


def _backends(cfg: Config, via: str | None) -> list[Backend]:
    return create_backends(cfg, via)


async def _stop_all(backends: list[Backend]) -> None:
    for b in backends:
        try:
            await b.stop()
        except Exception as e:  # cleanup must not hide the real result
            logging.getLogger(__name__).debug("stop %s: %s", b.name, e)


class _Printer:
    """Turns backend events into terminal lines, throttling progress."""

    def __init__(self):
        self._last_progress = 0.0

    def __call__(self, event: str, d: dict) -> None:
        if event == "progress":
            now = time.monotonic()
            if d["done"] != d["size"] and now - self._last_progress < 1.0:
                return
            self._last_progress = now
            pct = 100 * d["done"] // d["size"] if d["size"] else 100
            print(f"  {d['file']}: {pct}% ({_human(d['done'])}/{_human(d['size'])})",
                  flush=True)
        elif event == "pin":
            who = d.get("from") or d.get("to")
            print(f"Quick Share code {d['pin']} ({who}): must match the code on the other device",
                  flush=True)
        elif event == "file_received":
            print(f"received {d['file']} -> {d['path']}", flush=True)
        elif event == "file_sent":
            print(f"sent {d['file']}", flush=True)
        elif event == "message":
            kind = d.get("kind", "text")
            print(f"{kind} from {d['from']}:\n{d['text']}", flush=True)
        elif event == "waiting":
            print(f"waiting for {d['to']} to accept ...", flush=True)
        elif event == "done":
            print(f"done ({d['files']} item(s))", flush=True)
        elif event in ("rejected", "cancelled", "file_failed", "session_done", "failed"):
            print(f"{event.replace('_', ' ')}: "
                  + ", ".join(f"{k}={v}" for k, v in d.items()), flush=True)


async def _discover_all(backends: list[Backend], timeout: float) -> list[Device]:
    async def one(b: Backend) -> list[Device]:
        try:
            return await b.discover(timeout)
        except Exception as e:
            print(f"warning: {b.name} discovery failed: {e}", file=sys.stderr)
            return []
    results = await asyncio.gather(*(one(b) for b in backends))
    return [d for r in results for d in r]


def _table(devices: list[Device]) -> str:
    rows = [("PROTOCOL", "NAME", "ADDRESS", "TYPE")] + [
        (d.protocol, d.name, f"{d.address}:{d.port}", d.device_type or "-")
        for d in devices]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    return "\n".join("  ".join(r[i].ljust(widths[i]) for i in range(3)) + "  " + r[3]
                     for r in rows)


async def _cmd_devices(args, cfg: Config) -> int:
    backends = _backends(cfg, args.via)
    try:
        devices = await _discover_all(backends, args.timeout)
    finally:
        await _stop_all(backends)
    return _print_devices(args, devices)


def _print_devices(args, devices: list[Device]) -> int:
    if args.json:
        print(json.dumps([{"protocol": d.protocol, "id": d.id, "name": d.name,
                           "address": d.address, "port": d.port,
                           "model": d.model, "type": d.device_type}
                          for d in devices], indent=2))
    elif not devices:
        print("no devices found")
    else:
        print(_table(devices))
    return 0


def _parse_address(target: str) -> tuple[str, int | None] | None:
    """'1.2.3.4', '1.2.3.4:5', '[fe80::1]:5' -> (host, port); else None."""
    host, port = target, None
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else None
    elif target.count(":") == 1:
        host, p = target.split(":")
        port = int(p)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host, port


def _pick(devices: list[Device], target: str | None,
          prefer=PROTOCOLS) -> Device:
    if target:
        exact = [d for d in devices if d.name.lower() == target.lower()]
        partial = [d for d in devices if target.lower() in d.name.lower()]
        devices = exact or partial
        if not devices:
            raise TransferError(f"no device matching '{target}'")
    if not devices:
        raise TransferError("no devices found; use --to ADDRESS[:PORT]")
    # One physical device may show up once per protocol.
    names = {d.name.lower() for d in devices}
    if len(names) == 1:
        # Reachable over several protocols: config "prefer" decides.
        return min(devices, key=lambda d: prefer.index(d.protocol)
                   if d.protocol in prefer else len(prefer))
    listing = "\n".join(f"  {d.label()}" for d in devices)
    raise TransferError(f"several devices found, pick one with --to:\n{listing}")


async def _resolve_address(backends: list[Backend], host: str, port: int | None,
                           via: str | None) -> tuple[Backend, Device]:
    errors = []
    for b in backends:
        try:
            if b.name == "localsend":
                return b, await b.resolve(host, port)
            if b.name == "quickshare" and (port or via == "quickshare"):
                return b, await b.resolve(host, port)
            if b.name == "airdrop" and via == "airdrop":
                return b, await b.resolve(host, port)
        except TransferError as e:
            errors.append(str(e))
    raise TransferError("; ".join(errors) or f"nothing reachable at {host}")


async def _cmd_send(args, cfg: Config) -> int:
    paths = [Path(p) for p in args.files]
    if not paths and args.text is None:
        raise TransferError("nothing to send: give files or --text")
    if args.qr:
        return await _send_qr(args, cfg, paths)
    backends = _backends(cfg, args.via)
    printer = _Printer()
    try:
        addr = _parse_address(args.to) if args.to else None
        if addr:
            backend, device = await _resolve_address(backends, *addr, args.via)
        else:
            device = _pick(await _discover_all(backends, args.timeout), args.to, cfg.prefer)
            backend = next(b for b in backends if b.name == device.protocol)
        print(f"sending to {device.label()}", flush=True)
        kwargs = {"text": args.text}
        if device.protocol == "localsend":
            kwargs["pin"] = args.pin
        await backend.send(device, paths, printer, **kwargs)
    finally:
        await _stop_all(backends)
    return 0


async def _send_qr(args, cfg: Config, paths: list[Path]) -> int:
    if args.to or args.via == "localsend":
        raise TransferError("--qr is Quick Share only and finds the receiver itself")
    from multidrop.backends.quickshare import QuickShareBackend
    backend = QuickShareBackend(cfg)
    printer = _Printer()

    def on_event(event: str, d: dict) -> None:
        if event == "qr":
            from multidrop.backends.quickshare.qr import terminal_qr
            print(terminal_qr(d["url"]), flush=True)
            print(f"Scan with the phone camera (Android: Quick Share, Samsung: QR scanner)\n"
                  f"{d['url']}\nwaiting up to {args.timeout_qr:.0f} s ...", flush=True)
        elif event == "qr_scanned":
            print(f"scanned by {d['device']}", flush=True)
        else:
            printer(event, d)

    await backend.send_qr(paths, on_event, text=args.text, timeout=args.timeout_qr)
    return 0


def _describe_offer(sender: str, address: str, protocol: str,
                    files: list[tuple[str, int | None, str | None]]) -> None:
    """files: (name, size or None if unknown, text) per item."""
    sizes = [size for _, size, _ in files]
    total = None if None in sizes else sum(sizes)
    print(f"\n{sender} ({address}, {protocol}) "
          f"wants to send {len(files)} item(s), {_human(total)}:")
    for name, size, text in files:
        print(f"  {name}  {_human(size)}" if text is None else f"  [text] {text[:60]}")


def _yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def _choose(names: list[str], partial: str) -> list[int]:
    """Prompt (blocking); returns the indices of the items to accept, empty
    = reject. `partial` is Backend.partial_accept."""
    can_pick = partial != "none" and len(names) > 1
    answer = input("accept? [y/N/s=select] " if can_pick else "accept? [y/N] ").strip().lower()
    if can_pick and answer in ("s", "select", "a", "auswahl"):
        if partial == "discard":
            print("note: Quick Share transfers everything, unselected files are discarded")
        return [i for i, n in enumerate(names) if _yes(input(f"  {n}? [y/N] "))]
    return list(range(len(names))) if _yes(answer) else []


def _asker(partial: str):
    async def ask(offer: IncomingOffer) -> set[str]:
        _describe_offer(offer.sender_name, offer.sender_address, offer.protocol,
                        [(f.name, f.size if f.size_known else None, f.text)
                         for f in offer.files])
        chosen = await asyncio.to_thread(
            _choose, [f.name if f.text is None else "[text]" for f in offer.files], partial)
        return {offer.files[i].id for i in chosen}
    return ask


async def _accept_all(offer: IncomingOffer) -> set[str]:
    print(f"accepting {len(offer.files)} item(s) from {offer.sender_name} "
          f"({offer.sender_address}, {offer.protocol})", flush=True)
    return {f.id for f in offer.files}


async def _cmd_receive(args, cfg: Config) -> int:
    if args.accept == "ask" and not sys.stdin.isatty():
        print("stdin is not a terminal; use --accept all to accept without asking",
              file=sys.stderr)
        return 2
    stop = asyncio.Event()
    printer = _Printer()

    def on_event(event: str, d: dict) -> None:
        printer(event, d)
        if args.once and event in ("session_done", "message", "cancelled"):
            stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:     # Windows: Ctrl+C raises KeyboardInterrupt
            pass
    backends = _backends(cfg, args.via)
    try:
        started = []
        for b in backends:
            try:
                await b.start(_asker(b.partial_accept) if args.accept == "ask"
                              else _accept_all, on_event)
            except TransferError as e:
                if args.via:
                    raise
                print(f"warning: {e}", file=sys.stderr)
                continue
            started.append(b)
            if b.name == "airdrop":
                print(f"AirDrop: visible as '{cfg.alias}' on {cfg.airdrop.interface} "
                      f"port {b.port}", flush=True)
            elif b.name == "localsend":
                scheme = "https" if cfg.localsend.https else "http"
                print(f"LocalSend: visible as '{cfg.alias}' on {scheme} port {b.port}",
                      flush=True)
            else:
                mdns = "mDNS on" if cfg.quickshare.mdns else "no mDNS"
                print(f"Quick Share: visible as '{cfg.alias}' on tcp port {b.port} ({mdns})",
                      flush=True)
        if not started:
            raise TransferError("no protocol could be started")
        print(f"saving to {cfg.download_dir}", flush=True)
        await stop.wait()
    finally:
        await _stop_all(backends)
    return 0


async def _cmd_info(args, cfg: Config) -> int:
    from multidrop.backends.localsend import LocalSendBackend
    ls = LocalSendBackend(cfg)
    print(f"multidrop {__version__}")
    print(f"alias:        {cfg.alias}")
    print(f"download dir: {cfg.download_dir}")
    print(f"prefer:       {' > '.join(cfg.prefer)}")
    print(f"localsend:    {'on' if cfg.localsend.enabled else 'off'}, port {cfg.localsend.port}, "
          f"{'https' if cfg.localsend.https else 'http'}, fingerprint {ls.fingerprint}")
    qs = cfg.quickshare
    print(f"quickshare:   {'on' if qs.enabled else 'off'}, port {qs.port or 'random'}, "
          f"mDNS {'on' if qs.mdns else 'off'}, BLE wake-up {'on' if qs.ble else 'off'}")
    return 0


async def _cmd_share(args, cfg: Config) -> int:
    from multidrop.backends.localsend.webshare import WebShare
    from multidrop.backends.quickshare.qr import terminal_qr
    stop = asyncio.Event()

    def on_event(event: str, d: dict) -> None:
        if event == "share_download":
            print(f"download by {d['from']}: {d['file']}", flush=True)
        elif event == "share_visit":
            print(f"opened by {d['from']}", flush=True)
        elif event == "share_wrong_pin":
            print(f"wrong PIN from {d['from']}", flush=True)
        elif event == "share_stopped":
            print(f"share stopped ({d['reason']}, {d['downloads']} download(s))", flush=True)
            stop.set()

    share = WebShare([Path(p) for p in args.files], alias=cfg.alias,
                     pin=None if args.no_pin else (args.pin or ""), minutes=args.minutes,
                     port=cfg.localsend.share_port if args.share_port is None else args.share_port,
                     host=cfg.localsend.share_bind,
                     public_address=args.address, on_event=on_event)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:     # Windows: Ctrl+C raises KeyboardInterrupt
            pass
    await share.start()
    try:
        print(terminal_qr(share.url), flush=True)
        print(f"Open on the phone (same Wi-Fi): {share.url}", flush=True)
        if share.pin:
            print(f"PIN: {share.pin}", flush=True)
        print(f"{len(share.files)} file(s); stops in {args.minutes:g} min or with Ctrl+C",
              flush=True)
        await stop.wait()
    finally:
        await share.stop()
    return 0


async def _cmd_ui(args, cfg: Config) -> int:
    from multidrop.daemon import socket_path
    url_file = socket_path(cfg).parent / "url"
    try:
        url = url_file.read_text().strip()
    except FileNotFoundError:
        print("multidropd is not running (start it with `multidropd` or "
              "`systemctl --user start multidropd`)", file=sys.stderr)
        return 1
    print(url)
    if args.open:
        from multidrop.daemon import open_ui
        open_ui(url)
    return 0


_DIRECT = {"devices": _cmd_devices, "send": _cmd_send, "receive": _cmd_receive,
           "info": _cmd_info, "ui": _cmd_ui, "share": _cmd_share}


async def _dispatch(args, cfg: Config) -> int:
    """devices/send/receive/share go through multidropd when it runs (it
    holds the ports), otherwise the protocols are started in this process."""
    if args.command == "airdrop":
        from multidrop.airdrop_radio import command
        return await command(args, cfg)
    if args.command in ("devices", "send", "receive", "share") and not args.direct:
        from multidrop import cli_daemon
        from multidrop.daemon import socket_path
        from multidrop.daemon.client import DaemonClient
        client = await DaemonClient.connect(socket_path(cfg))
        if client is not None:
            async with client:
                return await cli_daemon.run(args, cfg, client)
    return await _DIRECT[args.command](args, cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="multidrop",
                                description="Nearby file sharing for Linux.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="config file (default ~/.config/multidrop/config.toml)")
    p.add_argument("--alias", help="name shown to other devices")
    p.add_argument("--port", type=int, help="LocalSend port (default 53317)")
    p.add_argument("--multicast-port", type=int, help=argparse.SUPPRESS)
    p.add_argument("--multicast-interface", help="IPv4 address of the interface for LocalSend discovery")
    p.add_argument("--http", action="store_true", help="LocalSend without TLS")
    p.add_argument("--qs-port", type=int, help="Quick Share port (default 53318, 0 = random)")
    p.add_argument("--mdns-address", help="IPv4 address for Quick Share mDNS (default: default route)")
    p.add_argument("--no-ble", action="store_true", help="no Bluetooth wake-up for Quick Share")
    p.add_argument("--direct", action="store_true",
                   help="do not use a running multidropd, start the protocols here")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def via(sp):
        sp.add_argument("--via", choices=PROTOCOLS, help="use only this protocol")

    d = sub.add_parser("devices", help="list nearby devices")
    d.add_argument("--timeout", type=float, default=4.0)
    d.add_argument("--json", action="store_true")
    via(d)

    s = sub.add_parser("send", help="send files, folders or a text")
    s.add_argument("files", nargs="*")
    s.add_argument("--to", help="device name or IP[:PORT]")
    s.add_argument("--text", help="send this text or URL instead of / besides files")
    s.add_argument("--pin", help="PIN the LocalSend receiver asks for")
    s.add_argument("--timeout", type=float, default=4.0, help="discovery time")
    s.add_argument("--qr", action="store_true",
                   help="Quick Share: show a QR code, send to the phone that scans it")
    s.add_argument("--timeout-qr", type=float, default=120.0, help=argparse.SUPPRESS)
    via(s)

    r = sub.add_parser("receive", help="be visible and receive files")
    r.add_argument("--accept", choices=("ask", "all"), default="ask")
    r.add_argument("--pin", help="require this PIN from LocalSend senders")
    r.add_argument("--dir", help="where to save files")
    r.add_argument("--once", action="store_true", help="exit after one transfer")
    via(r)

    sub.add_parser("info", help="show configuration and identity")

    sh = sub.add_parser("share", help="offer files for download in a phone's browser "
                                       "(no app needed; LocalSend download API)")
    sh.add_argument("files", nargs="+")
    sh.add_argument("--pin", help="PIN to ask for (default: random 4 digits)")
    sh.add_argument("--no-pin", action="store_true", help="only the secret link protects")
    sh.add_argument("--minutes", type=float, default=15, help="stop after this time (default 15)")
    sh.add_argument("--share-port", type=int, help="HTTP port (default 53320)")
    sh.add_argument("--address", help="address to put into the link (default: LAN address)")

    u = sub.add_parser("ui", help="print the web UI address of the running multidropd")
    u.add_argument("--open", action="store_true", help="open it in the browser")

    ad = sub.add_parser("airdrop", help="AirDrop with the original omdrop-owl radio backend")
    ad_sub = ad.add_subparsers(dest="airdrop_command", required=True)
    ad_sub.add_parser("check", help="read-only omdrop-owl hardware and radio check")
    ar = ad_sub.add_parser("run", help="start a timed radio + web UI session (local use; Wi-Fi may reconnect)")
    ar.add_argument("--minutes", type=int, default=10, choices=range(1, 61), metavar="1..60")
    ar.add_argument("--open", action="store_true", help="open the web UI")
    ar.add_argument("--switch-channel", action="store_true",
                    help="if the Wi-Fi is not on channel 6/44/149, move to the same network's "
                         "access point on one of them for the session, and back afterwards")
    return p


def _utf8_output() -> None:
    """Windows pipes default to the ANSI code page, which can't encode the
    terminal QR code and crashes the command."""
    for stream in (sys.stdout, sys.stderr):
        if (stream is not None and hasattr(stream, "reconfigure")
                and (stream.encoding or "").lower().replace("-", "") != "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    if WINDOWS:
        _utf8_output()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = _load_config(args)
    try:
        return asyncio.run(_dispatch(args, cfg))
    except TransferError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
