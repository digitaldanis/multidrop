"""AirDrop Ask details on loopback: sizes and the modern Ask fields when
sending, unknown sizes and text when receiving."""

import gzip
import plistlib

import aiohttp

from multidrop.backends.airdrop import AirDropBackend, client, text_file_name
from multidrop.backends.airdrop.server import AirDropServer
from multidrop.backends.localsend.identity import Identity
from multidrop.daemon.service import PendingOffer

from test_airdrop_loopback import REG, Events, odc


def test_build_ask(tmp_path):
    photo = tmp_path / "IMG_1.jpg"
    photo.write_bytes(b"\xff\xd8" + bytes(3000))
    album = tmp_path / "Album"
    (album / "sub").mkdir(parents=True)
    (album / "a.txt").write_bytes(b"12345")
    (album / "sub" / "b.txt").write_bytes(b"123")
    ask = client.build_ask("PC", "a1b2c3d4e5f6", "MacBookPro18,3", [photo, album], [],
                           "UUID-1")
    assert ask["TransferID"] == {"id": "UUID-1"} and ask["TransferType"] == {"files": {}}
    assert ask["Items"] == [] and ask["BundleID"] == "com.apple.finder"
    assert [(f["FileName"], f["FileSize"], f["FileType"], f["FileIsDirectory"])
            for f in ask["Files"]] == [("IMG_1.jpg", 3002, "public.jpeg", False),
                                       ("Album", 8, "public.folder", True)]
    plistlib.dumps(ask, fmt=plistlib.FMT_BINARY)          # serialisable as sent
    links = client.build_ask("PC", "x", "m", [], ["https://example.org/"], "UUID-2")
    assert links["TransferType"] == {"links": {}} and links["Items"] == ["https://example.org/"]
    assert "Files" not in links and links["BundleID"] == "com.apple.Safari"


def test_text_file_name():
    assert text_file_name("Shopping list\nmilk\nbread") == "Shopping list.txt"
    assert text_file_name("\n\n") == "Text.txt"
    assert text_file_name("../../etc/passwd") == "etcpasswd.txt"
    long = text_file_name("This is a very long sentence that has more than forty characters")
    assert long == "This is a very long sentence that has.txt"


async def _pair(make_config):
    a_cfg, b_cfg = make_config("Linux-PC"), make_config("iPhone")
    for cfg in (a_cfg, b_cfg):
        cfg.airdrop.bind = "127.0.0.1"
        cfg.airdrop.port = 0
        cfg.airdrop.mdns = False
    return AirDropBackend(a_cfg), AirDropBackend(b_cfg)


async def test_sizes_in_offer_and_text_as_message(make_config, tmp_path):
    a, b = await _pair(make_config)
    offers, events = [], Events()

    async def remember(offer):
        offers.append(offer)
        return {f.id for f in offer.files}
    await b.start(remember, events)
    try:
        doc = tmp_path / "Letter.pdf"
        doc.write_bytes(b"%PDF" * 1000)
        device = await a.resolve("127.0.0.1", b.port)
        await a.send(device, [doc])
        assert [(f.name, f.size, f.size_known) for f in offers[0].files] == \
            [("Letter.pdf", 4000, True)]

        await a.send(device, [], text="Shopping list\nmilk")
        f = offers[1].files[0]
        assert (f.name, f.mime, f.size) == ("Shopping list.txt", "public.plain-text", 18)
        msg = events.of("message")[-1]
        assert msg["text"] == "Shopping list\nmilk" and msg["kind"] == "text"
        assert (b.config.download_dir / "Shopping list.txt").read_text() == "Shopping list\nmilk"

        await a.send(device, [], text="https://example.org/x")      # a link, no file
        assert events.of("message")[-1] == {"from": "Linux-PC", "text": "https://example.org/x",
                                            "kind": "url"}
    finally:
        await a.stop()
        await b.stop()


async def test_ask_without_file_size(tmp_path):
    """Older senders (opendrop) send no FileSize: unknown, not 0 B."""
    ident = Identity.load_or_create(tmp_path / "id", common_name="t")
    offers, events = [], Events()

    async def remember(offer):
        offers.append(offer)
        return {f.id for f in offer.files}
    server = AirDropServer(name="PC", model="MacBookPro18,3", download_dir=tmp_path / "in",
                           host="127.0.0.1", port=0, ssl_context=ident.server_ssl_context(),
                           on_offer=remember, on_event=events)
    await server.start()
    base = f"https://127.0.0.1:{server.port}"
    try:
        async with aiohttp.ClientSession() as http:
            ask = {"SenderComputerName": "Mac", "Files": [
                {"FileName": "note.txt", "FileType": "public.plain-text"},
                {"FileName": "photo.jpg", "FileType": "public.jpeg", "FileSize": "nonsense"}]}
            async with http.post(base + "/Ask", ssl=False,
                                 data=plistlib.dumps(ask, fmt=plistlib.FMT_BINARY)) as r:
                assert r.status == 200
            body = gzip.compress(odc([("./note.txt", REG, b"hello"), ("./photo.jpg", REG, b"x")]))
            async with http.post(base + "/Upload", data=body, ssl=False,
                                 headers={"Content-Type": "application/x-cpio"}) as r:
                assert r.status == 200
    finally:
        await server.stop()
    assert [(f.size, f.size_known) for f in offers[0].files] == [(0, False), (0, False)]
    shown = PendingOffer("x", "airdrop", offers[0], None).to_dict()
    assert shown["size_unknown"] and [f["size"] for f in shown["files"]] == [None, None]
    assert not events.of("message")          # two files: not a text message
    assert (tmp_path / "in" / "note.txt").read_bytes() == b"hello"
