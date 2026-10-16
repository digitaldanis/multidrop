"""Exercise the Java Android engine against MultiDrop's actual Python backend."""
import asyncio
import os
from pathlib import Path
import tempfile

import aiohttp
from multidrop.backends.localsend.client import fetch_info, send_files
from multidrop.backends.localsend.server import LocalSendServer
from multidrop.backends.localsend.protocol import device_info

ANDROID = Path(__file__).resolve().parents[1]


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        downloads = root / "desktop"
        downloads.mkdir()

        async def accept(offer):
            return {f.id for f in offer.files}

        server = LocalSendServer(
            info=lambda: device_info("Desktop test", "desktop-test", server.port, False),
            download_dir=downloads, host="127.0.0.1", port=0,
            ssl_context=None, pin=None, on_offer=accept,
            on_event=lambda *_: None, on_peer=lambda *_: None,
        )
        await server.start()
        proc = None
        try:
            java = str(Path(os.environ["JAVA_HOME"]) / "bin/java")
            cp = f"{ANDROID}/build/tests:{ANDROID}/dependencies/json-20240303.jar"
            proc = await asyncio.create_subprocess_exec(
                java, "-cp", cp, "de.digitaldanis.multidrop.InteropHarness",
                str(root / "android"), str(server.port),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            port = int(await asyncio.wait_for(proc.stdout.readline(), 10))
            assert await asyncio.wait_for(proc.stdout.readline(), 10) == b"SENT\n"
            assert (downloads / "from-android.txt").read_text() == "Android → Desktop ✓"
            source = root / "from-desktop.bin"
            source.write_bytes(bytes(range(256)) * 4096)
            async with aiohttp.ClientSession() as http:
                peer = await fetch_info(http, "127.0.0.1", port)
                await send_files(http, server.info(), peer, [source], lambda *_: None)
            assert (root / "android/from-desktop.bin").read_bytes() == source.read_bytes()
            print("PASS: Android Java → desktop Python and desktop Python → Android Java (1 MiB, SHA-256)")
        finally:
            if proc:
                proc.stdin.write(b"\n")
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), 10)
                errors = await proc.stderr.read()
                if proc.returncode:
                    raise RuntimeError(errors.decode())
            await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
