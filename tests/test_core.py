import hashlib
from pathlib import Path

import pytest

from multidrop.core import (ChecksumError, IncomingFileWriter, OfferFile,
                            TransferError, safe_relative_path, unique_path)


@pytest.mark.parametrize("name, expected", [
    ("photo.jpg", "photo.jpg"),
    ("dir/sub/file.txt", "dir/sub/file.txt"),
    ("../../etc/passwd", "etc/passwd"),
    ("/etc/passwd", "etc/passwd"),
    ("..\\..\\win.ini", "win.ini"),
    ("a/./b/../c", "a/b/c"),
    ("bad\x00name\n.txt", "badname.txt"),
    ("", "unnamed"),
    ("..", "unnamed"),
    ("//etc/evil", "etc/evil"),                 # "//" is a root on POSIX
    ("C:evil.txt", "C_evil.txt"),               # drive-relative on Windows
    ("D:/x/evil.txt", "D_/x/evil.txt"),
    ("\\\\server\\share\\x", "server/share/x"),     # UNC share
    ("a.txt:stream", "a.txt_stream"),           # NTFS stream
    ("CON", "_CON"),
    ("dir/nul.txt", "dir/_nul.txt"),
    ('what?<>|*".txt', "what______.txt"),
    ("name. ", "name"),
    ("...", "unnamed"),
])
def test_safe_relative_path(name, expected):
    result = safe_relative_path(name)
    assert result == Path(expected)
    assert not result.is_absolute() and not result.drive and not result.anchor
    assert ".." not in result.parts


def test_unique_path(tmp_path):
    p = tmp_path / "a.txt"
    assert unique_path(p) == p
    p.write_text("x")
    assert unique_path(p) == tmp_path / "a (1).txt"
    (tmp_path / "a (1).txt").write_text("x")
    assert unique_path(p) == tmp_path / "a (2).txt"


def test_writer_ok_and_no_overwrite(tmp_path):
    data = b"hello world"
    f = OfferFile("1", "x.bin", len(data), sha256=hashlib.sha256(data).hexdigest())
    (tmp_path / "x.bin").write_bytes(b"old")
    w = IncomingFileWriter(tmp_path, f)
    w.write(data)
    path = w.finish()
    assert path == tmp_path / "x (1).bin"
    assert path.read_bytes() == data
    assert (tmp_path / "x.bin").read_bytes() == b"old"
    assert not list(tmp_path.glob("*.part"))


def test_writer_rejects_extra_data(tmp_path):
    w = IncomingFileWriter(tmp_path, OfferFile("1", "x", 3))
    with pytest.raises(TransferError):
        w.write(b"abcd")
    w.abort()
    assert list(tmp_path.iterdir()) == []


def test_writer_rejects_short_and_bad_checksum(tmp_path):
    w = IncomingFileWriter(tmp_path, OfferFile("1", "x", 5))
    w.write(b"abc")
    with pytest.raises(TransferError):
        w.finish()
    w = IncomingFileWriter(tmp_path, OfferFile("2", "y", 3, sha256="00" * 32))
    w.write(b"abc")
    with pytest.raises(ChecksumError):
        w.finish()
    assert list(tmp_path.iterdir()) == []


def test_writer_stays_in_download_dir(tmp_path):
    """A peer naming an absolute path must not write outside the folder."""
    downloads, outside = tmp_path / "downloads", tmp_path / "outside"
    downloads.mkdir()
    w = IncomingFileWriter(downloads, OfferFile("1", f"/{outside}/x.desktop", 2))
    w.write(b"hi")
    path = w.finish()
    assert path.is_relative_to(downloads) and not outside.exists()
