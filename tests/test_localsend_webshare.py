"""Browser download (LocalSend download API) on 127.0.0.1."""

import asyncio
import errno
import html
import io
import zipfile

import aiohttp
import pytest

from conftest import WINDOWS
from multidrop.backends.localsend.webshare import MAX_FAILS_PER_IP, MAX_FAILS_TOTAL, WebShare

# Markup in a file name must be escaped; Windows forbids < and > in names.
NAME = "x&y.txt" if WINDOWS else "<i>x&y.txt"


@pytest.fixture
async def share(tmp_path):
    shares = []

    async def make(pin="1234", minutes=5):
        (tmp_path / "Album").mkdir(exist_ok=True)
        (tmp_path / "Album" / "a.jpg").write_bytes(b"A" * 100_000)
        (tmp_path / NAME).write_text("hello")
        events = []
        s = WebShare([tmp_path / "Album", tmp_path / NAME], alias="PC",
                     pin=pin, minutes=minutes, host="127.0.0.1", port=0,
                     on_event=lambda e, d: events.append((e, d)))
        await s.start()
        shares.append(s)
        return s, events, f"http://127.0.0.1:{s.port}"

    yield make
    for s in shares:
        await s.stop()


async def test_key_pin_and_download(share):
    s, events, base = await share()
    assert s.url == f"{base}/?k={s.key}"
    async with aiohttp.ClientSession() as http:
        for path in ("/", "/zip", "/api/localsend/v2/download"):
            async with http.get(base + path) as r:
                assert r.status == 404                    # no key, nothing visible
        async with http.get(s.url) as r:
            page = await r.text()
            assert r.status == 200 and 'name="pin"' in page and "a.jpg" not in page
        async with http.post(f"{base}/unlock?k={s.key}", data={"pin": "0000"}) as r:
            assert r.status == 401 and "Wrong PIN" in await r.text()
        async with http.post(f"{base}/unlock?k={s.key}", data={"pin": "1234"},
                             allow_redirects=False) as r:
            assert r.status == 303
            location = r.headers["Location"]
        async with http.get(base + location) as r:
            page = await r.text()
        assert "Album/a.jpg" in page and html.escape(NAME) in page and "<i>x" not in page
        links = [l.split('"')[0].replace("&amp;", "&") for l in page.split('href="')[1:]]
        file_link = next(l for l in links if "download?" in l)
        async with http.get(base + file_link) as r:
            body = await r.read()
            assert r.headers["Content-Disposition"].startswith("attachment;")
        assert body in (b"A" * 100_000, b"hello")
        zip_link = next(l for l in links if l.startswith("/zip"))
        async with http.get(base + zip_link) as r:
            zf = zipfile.ZipFile(io.BytesIO(await r.read()))
        assert sorted(zf.namelist()) == sorted([NAME, "Album/a.jpg"])
        assert zf.read("Album/a.jpg") == b"A" * 100_000
        # A made-up session gets nothing.
        async with http.get(f"{base}/zip?k={s.key}&s=guess") as r:
            assert r.status == 403
    names = [e for e, _ in events]
    assert names[0] == "share_started" and "share_wrong_pin" in names
    assert names.count("share_download") == 2


async def test_spec_api_and_rate_limit(share):
    s, events, base = await share(pin="987654")
    prep = f"{base}/api/localsend/v2/prepare-download?k={s.key}"
    async with aiohttp.ClientSession() as http:
        async with http.post(prep) as r:
            assert r.status == 401 and await r.text() == "PIN required"
        async with http.post(prep + "&pin=987654") as r:
            data = await r.json()
        assert data["info"]["download"] is True and len(data["files"]) == 2
        sid = data["sessionId"]
        async with http.post(prep + f"&sessionId={sid}") as r:   # page reload
            assert r.status == 200 and (await r.json())["sessionId"] == sid
        fid, meta = next((k, v) for k, v in data["files"].items() if v["fileName"] == "Album/a.jpg")
        async with http.get(f"{base}/api/localsend/v2/download?k={s.key}&sessionId={sid}"
                            f"&fileId={fid}", headers={"Range": "bytes=10-19"}) as r:
            assert r.status == 206 and await r.read() == b"A" * 10      # resumable
        async with http.get(f"{base}/api/localsend/v2/download?k={s.key}&sessionId=nope"
                            f"&fileId={fid}") as r:
            assert r.status == 403
        for _ in range(MAX_FAILS_PER_IP):
            async with http.post(prep + "&pin=111111") as r:
                assert r.status == 401
        async with http.post(prep + "&pin=987654") as r:
            assert r.status == 429                      # locked out, even with the right PIN


async def test_too_many_wrong_pins_stop_the_share(share, monkeypatch):
    """Guessing from many addresses: the share stops after MAX_FAILS_TOTAL."""
    s, events, base = await share(pin="987654")
    guesses = iter(range(MAX_FAILS_TOTAL))
    monkeypatch.setattr(s, "_client", lambda request: f"10.0.0.{next(guesses, 0) // 2}")
    prep = f"{base}/api/localsend/v2/prepare-download?k={s.key}&pin=111111"
    async with aiohttp.ClientSession() as http:
        for _ in range(MAX_FAILS_TOTAL):
            async with http.post(prep) as r:
                assert r.status == 401
    await asyncio.wait_for(s.stopped.wait(), 5)
    assert events[-1] == ("share_stopped", {"reason": "too many wrong PINs", "downloads": 0})


async def test_no_pin_and_expiry(share):
    s, events, base = await share(pin=None, minutes=0.01)
    async with aiohttp.ClientSession() as http:
        async with http.get(s.url) as r:
            assert "a.jpg" in await r.text()            # straight to the list
    await asyncio.wait_for(s.stopped.wait(), 5)
    assert events[-1] == ("share_stopped", {"reason": "expired", "downloads": 0})
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.ClientConnectionError):
            await http.get(s.url)


async def test_generated_pin(share):
    s, _, _ = await share(pin="")
    assert len(s.pin) == 4 and s.pin.isdigit()


async def test_https_share(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    s = WebShare([tmp_path / "a.txt"], alias="PC", pin=None, minutes=5,
                 host="127.0.0.1", port=0, https=True)
    await s.start()
    try:
        assert s.url.startswith(f"https://127.0.0.1:{s.port}/")
        async with aiohttp.ClientSession() as http:
            async with http.get(s.url, ssl=False) as r:
                assert r.status == 200 and "a.txt" in await r.text()
            with pytest.raises(aiohttp.ClientError):    # self-signed: not trusted
                await http.get(s.url)
        # Apple's TLS rules; iOS silently drops connections that break them.
        import ssl

        from cryptography import x509
        pem = await asyncio.to_thread(ssl.get_server_certificate, ("127.0.0.1", s.port))
        cert = x509.load_pem_x509_certificate(pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku
        assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days <= 825
    finally:
        await s.stop()


async def test_share_follows_the_network(tmp_path, monkeypatch):
    """Another Wi-Fi: the link and its certificate move to the new address."""
    from multidrop.backends.localsend import webshare
    address = ["127.0.0.1"]
    monkeypatch.setattr(webshare, "_current_address", lambda: address[0])
    monkeypatch.setattr(webshare, "_throwaway_tls",
                        lambda a, real=webshare._throwaway_tls: real("127.0.0.1"))
    (tmp_path / "a.txt").write_text("hello")
    events = []
    s = WebShare([tmp_path / "a.txt"], alias="PC", pin=None, minutes=5, port=0,
                 https=True, on_event=lambda e, d: events.append((e, d)))
    await s.start()
    try:
        port = s.port
        follow, s._follow = s._follow, None
        follow.cancel()
        address[0] = "192.0.2.7"                       # e.g. now on the home Wi-Fi
        task = asyncio.ensure_future(s._follow_network(interval=0.01))
        while not any(e == "share_moved" for e, _ in events):
            await asyncio.sleep(0.01)
        task.cancel()
        assert s.url.startswith(f"https://192.0.2.7:{port}/") and s.port == port
        async with aiohttp.ClientSession() as http:   # still serving after the move
            async with http.get(f"https://127.0.0.1:{port}/?k={s.key}", ssl=False) as r:
                assert r.status == 200
    finally:
        await s.stop()


async def test_share_stops_if_it_cannot_move(tmp_path, monkeypatch):
    """The port is taken while moving to the new address: stop, don't pretend."""
    from multidrop.backends.localsend import webshare
    address = ["127.0.0.1"]
    monkeypatch.setattr(webshare, "_current_address", lambda: address[0])
    monkeypatch.setattr(webshare, "_throwaway_tls",
                        lambda a, real=webshare._throwaway_tls: real("127.0.0.1"))
    (tmp_path / "a.txt").write_text("hello")
    events = []
    s = WebShare([tmp_path / "a.txt"], alias="PC", pin=None, minutes=5, port=0,
                 https=True, on_event=lambda e, d: events.append((e, d)))
    await s.start()
    s._follow.cancel()

    async def taken(port):
        raise OSError(errno.EADDRINUSE, "in use")
    monkeypatch.setattr(s, "_listen", taken)
    address[0] = "192.0.2.7"
    await asyncio.wait_for(s._follow_network(interval=0.01), 5)
    await asyncio.wait_for(s.stopped.wait(), 5)
    assert ("share_stopped", {"reason": "network changed", "downloads": 0}) in events
    assert not any(e == "share_moved" for e, _ in events)
