"""Client for the multidropd API (used by the CLI): on its Unix socket, on
Windows over 127.0.0.1 with the web UI token from the daemon's url file.

`DaemonClient.connect()` returns None when no daemon answers, so callers can
fall back to running the backends themselves.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp

from multidrop.config import WINDOWS
from multidrop.core import TransferError

from .api import TOKEN_HEADER


def _endpoint(sock: Path) -> Path:
    """File whose existence means a daemon may be running."""
    return sock.parent / "url" if WINDOWS else sock


class DaemonClient:
    def __init__(self, sock: Path):
        self.sock = sock
        timeout = aiohttp.ClientTimeout(total=60)
        if WINDOWS:
            base, _, token = _endpoint(sock).read_text().strip().partition("/#t=")
            self._base = base.rstrip("/")
            self._session = aiohttp.ClientSession(headers={TOKEN_HEADER: token},
                                                  timeout=timeout)
        else:
            self._base = "http://localhost"
            self._session = aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=str(sock)), timeout=timeout)

    @classmethod
    async def connect(cls, sock: Path) -> DaemonClient | None:
        try:
            client = cls(sock)
        except OSError:         # no socket / url file: no daemon
            return None
        try:
            await client.get("/api/status")
        except (aiohttp.ClientError, OSError):
            await client.close()
            return None
        return client

    async def close(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> DaemonClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def request(self, method: str, path: str, body: dict | None = None):
        async with self._session.request(method, self._base + path, json=body) as r:
            try:
                data = await r.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                data = None
            if r.status >= 400:
                msg = data.get("error") if isinstance(data, dict) else None
                raise TransferError(msg or f"multidropd: HTTP {r.status}")
            return data

    async def get(self, path: str):
        return await self.request("GET", path)

    async def post(self, path: str, body: dict | None = None):
        return await self.request("POST", path, body or {})

    async def delete(self, path: str):
        return await self.request("DELETE", path)

    async def last_seq(self) -> int:
        return max((e["seq"] for e in await self.get("/api/history")), default=0)

    async def events(self, since: int) -> AsyncIterator[dict]:
        """Events with seq > since, until the daemon ends the stream."""
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with self._session.get(f"{self._base}/api/events?since={since}",
                                     timeout=timeout) as r:
            if r.status != 200:
                raise TransferError(f"multidropd events: HTTP {r.status}")
            buf = b""
            # Not `async for line in r.content`: QR events (SVG) can exceed
            # aiohttp's line limit.
            async for chunk in r.content.iter_any():
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if line.startswith(b"data: "):
                        yield json.loads(line[6:])
