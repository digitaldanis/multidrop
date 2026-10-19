"""Protocol-independent types shared by all backends.

Every backend (LocalSend, Quick Share, AirDrop) maps its protocol onto these:
it reports peers as `Device`, asks the front end about incoming transfers with
an `IncomingOffer`, and writes files with `IncomingFileWriter`, which owns the
rules for safe names, no overwrites and integrity checks.
"""

from __future__ import annotations

import abc
import hashlib
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Device:
    protocol: str          # "localsend", "quickshare", "airdrop"
    id: str                # stable per protocol (fingerprint, endpoint id, ...)
    name: str
    address: str
    port: int
    model: str | None = None
    device_type: str | None = None
    extra: dict = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.name} [{self.protocol}] {self.address}:{self.port}"


@dataclass
class OfferFile:
    id: str
    name: str              # as sent by the peer, untrusted
    size: int
    mime: str | None = None
    sha256: str | None = None
    text: str | None = None  # set when the "file" is really a text message
    size_known: bool = True  # False: the sender did not say (older AirDrop senders)


@dataclass
class IncomingOffer:
    protocol: str
    sender_name: str
    sender_address: str
    files: list[OfferFile]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


# Returns the ids of the files to accept; an empty set rejects the offer.
OfferHandler = Callable[[IncomingOffer], Awaitable[set[str]]]
# Called with (event, details) for progress and completion messages.
EventHandler = Callable[[str, dict], None]


def _noop_event(event: str, details: dict) -> None:
    pass


class TransferError(Exception):
    pass


_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
# Not allowed in Windows file names; ":" would also make "C:x" a drive path
# or "a.txt:x" an NTFS stream, so it goes everywhere.
_NOT_ON_WINDOWS = re.compile(r'[<>:"|?*]')
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                    *(f"LPT{i}" for i in range(10))}


def safe_relative_path(name: str) -> Path:
    """Turn a peer-supplied file name into a safe relative path.

    Folder transfers send names like "dir/sub/file.txt", which are kept as
    subdirectories. Absolute paths (also "//x" and Windows drives or shares),
    "..", empty parts and control characters are removed so the result can
    never leave the download directory, and every part is a valid Windows
    file name.
    """
    parts = []
    # Split by hand: PurePosixPath keeps a leading "//" as the root part.
    for part in re.split(r"[/\\]", name):
        part = _NOT_ON_WINDOWS.sub("_", _UNSAFE.sub("", part)).strip()
        part = part.rstrip(". ")            # Windows drops these; ".." becomes ""
        if not part:
            continue
        if part.split(".")[0].upper() in _WINDOWS_DEVICES:
            part = "_" + part
        parts.append(part[:255])
    if not parts:
        parts = ["unnamed"]
    return Path(*parts)


def unique_path(path: Path) -> Path:
    """Return `path`, or "name (n).ext" if it already exists."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


class IncomingFileWriter:
    """Writes one incoming file to `<dest>.part`, then renames it into place.

    Enforces the announced size and, if given, the SHA-256 checksum.
    """

    def __init__(self, download_dir: Path, offer_file: OfferFile):
        self.offer_file = offer_file
        rel = safe_relative_path(offer_file.name)
        target = download_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        self.target = unique_path(target)
        self.part = unique_path(self.target.with_name(self.target.name + ".part"))
        self._f = open(self.part, "xb")
        self._hash = hashlib.sha256()
        self.received = 0

    def write(self, chunk: bytes) -> None:
        self.received += len(chunk)
        if self.received > self.offer_file.size:
            raise TransferError("more data than announced")
        self._hash.update(chunk)
        self._f.write(chunk)

    def finish(self) -> Path:
        self._f.close()
        if self.received != self.offer_file.size:
            self.abort()
            raise TransferError(
                f"size mismatch: got {self.received}, expected {self.offer_file.size}")
        expected = self.offer_file.sha256
        if expected and self._hash.hexdigest() != expected.lower():
            self.abort()
            raise ChecksumError("sha256 mismatch")
        # The name may have been taken while we were receiving.
        self.target = unique_path(self.target)
        os.rename(self.part, self.target)
        return self.target

    def abort(self) -> None:
        if not self._f.closed:
            self._f.close()
        self.part.unlink(missing_ok=True)


class ChecksumError(TransferError):
    pass


class PinRequired(TransferError):
    """The receiver wants a (different) PIN; retry with one."""


def collect_files(paths: list[Path]) -> list[tuple[Path, str]]:
    """Expand directories. Returns (path on disk, name to send) pairs."""
    out = []
    for p in paths:
        if p.is_dir():
            base = p.parent
            for root, dirs, names in os.walk(p):
                dirs.sort()
                for n in sorted(names):
                    full = Path(root) / n
                    if full.is_file():
                        out.append((full, full.relative_to(base).as_posix()))
        elif p.is_file():
            out.append((p, p.name))
        else:
            raise TransferError(f"not a file: {p}")
    if not out:
        raise TransferError("nothing to send")
    return out


class Backend(abc.ABC):
    """One sharing protocol. The CLI (and later the daemon) drives these."""

    name: str
    # Can the user accept only some files of an incoming offer?
    # "native": the protocol lets the receiver choose, the rest is not sent;
    # "discard": everything is transferred, unchosen files are thrown away;
    # "none": all or nothing.
    partial_accept: str = "none"

    @abc.abstractmethod
    async def start(self, on_offer: OfferHandler,
                    on_event: EventHandler = _noop_event) -> None:
        """Become visible and accept incoming transfers."""

    @abc.abstractmethod
    async def stop(self) -> None:
        ...

    @abc.abstractmethod
    async def discover(self, timeout: float) -> list[Device]:
        ...

    @abc.abstractmethod
    async def send(self, device: Device, paths: list[Path],
                   on_event: EventHandler = _noop_event) -> None:
        """Send files; raises TransferError on refusal or failure."""
