"""Build the screen mirroring engine folder for the Windows installer:

    build/mirror/doubletake.exe
    build/mirror/gstreamer/bin/            gst-launch/inspect + the DLLs they and the plugins need
    build/mirror/gstreamer/lib/gstreamer-1.0/   only the plugins doubletake's pipelines use
    build/mirror/gstreamer/libexec/gstreamer-1.0/gst-plugin-scanner.exe

A full GStreamer runtime is about 1 GB; this copy keeps what doubletake's
Windows pipelines name (internal/airplay/capture*.go, audio.go) plus the DLLs
those plugins import, found by reading their PE import tables. Afterwards
every required element is checked from the copy alone, with a fresh registry
and no access to the full installation.

    python packaging/windows/make_mirror_pack.py --gstreamer C:\\gst --doubletake build\\doubletake --out build\\mirror

Needs `pefile` (pip install pefile). Windows only.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pefile

# Every element doubletake can put into a pipeline on Windows. A missing one
# fails the build; OPTIONAL ones are copied when the runtime has them.
REQUIRED = [
    # capture and shared video path
    "d3d11screencapturesrc", "d3d11convert", "d3d11download", "queue", "videoconvert",
    "videoscale", "videorate", "fdsink", "fakesink", "videotestsrc", "timeoverlay",
    "h264parse", "h265parse", "rtph264pay", "rtph265pay", "rtponviftimestamp", "rtpstreampay",
    # encoders that need no particular GPU
    "mfh264enc", "openh264enc", "x264enc",
    # audio
    "wasapi2src", "wasapisrc", "audiotestsrc", "audioconvert", "audioresample",
    "audiobuffersplit", "rtpL16pay",
]
OPTIONAL = ["nvh264enc", "nvh265enc", "qsvh264enc", "amfh264enc", "x265enc"]
TOOLS = ["gst-launch-1.0.exe", "gst-inspect-1.0.exe"]


def inspect(gst_bin: Path, element: str, env=None) -> str | None:
    r = subprocess.run([str(gst_bin / "gst-inspect-1.0.exe"), element],
                       capture_output=True, text=True, env=env)
    return r.stdout if r.returncode == 0 else None


def plugin_file(gst_bin: Path, element: str) -> Path | None:
    out = inspect(gst_bin, element)
    if out is None:
        return None
    m = re.search(r"^\s*Filename\s+(.+?)\s*$", out, re.M)
    return Path(m.group(1)) if m else None    # None: built into libgstreamer (queue, fdsink …)


def imports(path: Path) -> set[str]:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    names = set()
    for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
        for entry in getattr(pe, attr, []):
            names.add(entry.dll.decode().lower())
    pe.close()
    return names


def dll_closure(roots: list[Path], gst_bin: Path) -> set[Path]:
    """GStreamer's own DLLs (from its bin folder) that the roots need,
    transitively. System DLLs (kernel32, d3d11, mfplat …) are not copied."""
    available = {p.name.lower(): p for p in gst_bin.glob("*.dll")}
    needed: set[Path] = set()
    todo = list(roots)
    while todo:
        for name in imports(todo.pop()):
            dll = available.get(name)
            if dll is not None and dll not in needed:
                needed.add(dll)
                todo.append(dll)
    return needed


def build(gst: Path, doubletake: Path, out: Path) -> None:
    gst_bin = gst / "bin"
    plugins: set[Path] = set()
    missing = []
    for element in REQUIRED + OPTIONAL:
        if inspect(gst_bin, element) is None:
            if element in REQUIRED:
                missing.append(element)
            else:
                print(f"optional element {element} not in this runtime")
            continue
        plugin = plugin_file(gst_bin, element)
        if plugin is not None:
            plugins.add(plugin)
    if missing:
        sys.exit(f"GStreamer runtime lacks {', '.join(missing)}")

    scanner = gst / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"
    tools = [gst_bin / t for t in TOOLS]
    dlls = dll_closure([*tools, scanner, *plugins], gst_bin)

    if out.exists():
        shutil.rmtree(out)
    dest = out / "gstreamer"
    for sub in ("bin", "lib/gstreamer-1.0", "libexec/gstreamer-1.0"):
        (dest / sub).mkdir(parents=True)
    for f in [*tools, *dlls]:
        shutil.copy2(f, dest / "bin" / f.name)
    for f in plugins:
        shutil.copy2(f, dest / "lib" / "gstreamer-1.0" / f.name)
    shutil.copy2(scanner, dest / "libexec" / "gstreamer-1.0" / scanner.name)
    for exe in ("doubletake.exe", "doubletake-ctl.exe"):
        shutil.copy2(doubletake / exe, out / exe)
    for licence in ("LICENSE", "COPYING.GPL"):
        if (doubletake / licence).exists():
            shutil.copy2(doubletake / licence, out / f"doubletake-{licence}.txt")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{len(plugins)} plugins, {len(dlls)} DLLs, {size / 2**20:.0f} MiB in {out}")
    verify(dest, REQUIRED)


def verify(dest: Path, elements: list[str]) -> None:
    """Every required element must load from the copy alone."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GST_", "GSTREAMER_")) and k.upper() != "PATH"}
        env["PATH"] = os.pathsep.join([str(dest / "bin"), os.environ.get("SystemRoot", r"C:\Windows")
                                       + r"\System32"])
        env["GST_PLUGIN_SYSTEM_PATH"] = str(dest / "lib" / "gstreamer-1.0")
        env["GST_PLUGIN_SCANNER"] = str(dest / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe")
        env["GST_REGISTRY"] = str(Path(tmp) / "registry.bin")
        broken = [e for e in elements if inspect(dest / "bin", e, env) is None]
        if broken:
            sys.exit(f"the trimmed GStreamer cannot load {', '.join(broken)}")
        # Live like doubletake's sources: rtponviftimestamp needs a running clock.
        pipeline = ["--quiet", "videotestsrc", "is-live=true", "num-buffers=5",
                    "!", "video/x-raw,width=320,height=240",
                    "!", "videoconvert", "!", "x264enc", "!", "h264parse", "!", "rtph264pay",
                    "!", "rtponviftimestamp", "ntp-offset=-1", "!", "rtpstreampay", "!", "fakesink"]
        r = subprocess.run([str(dest / "bin" / "gst-launch-1.0.exe"), *pipeline],
                           capture_output=True, text=True, env=env, timeout=60)
        if r.returncode != 0:
            sys.exit(f"encoding pipeline fails in the trimmed GStreamer: {r.stderr}")
    print(f"verified: {len(elements)} elements load from the copy alone")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gstreamer", type=Path, required=True, help="GStreamer MSVC runtime root")
    p.add_argument("--doubletake", type=Path, required=True,
                   help="folder with doubletake.exe and doubletake-ctl.exe")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    build(a.gstreamer, a.doubletake, a.out)


if __name__ == "__main__":
    main()
