"""The browser edition in pages/: everything the page, the manifest and the
service worker refer to is there, and CI publishes it."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "pages"


def test_manifest_icons_exist():
    manifest = json.loads((PAGES / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["start_url"] == manifest["scope"] == "./"
    assert manifest["display"] == "standalone"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])
    for icon in manifest["icons"]:
        assert (PAGES / icon["src"]).is_file(), icon["src"]


def test_page_links_exist():
    html = (PAGES / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:href|src)="([^"#:]+)"', html):
        assert ref == "./" or (PAGES / ref).is_file(), ref


def test_service_worker_shell_exists():
    sw = (PAGES / "sw.js").read_text(encoding="utf-8")
    shell = json.loads(re.search(r"const SHELL = (\[.*?\]);", sw).group(1))
    for path in shell:
        assert path == "./" or (PAGES / path).is_file(), path


def test_ci_publishes_every_page_file():
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    for needed in ("pages/index.html", "pages/*.js", "pages/style.css", "pages/manifest.webmanifest", "pages/icons"):
        assert needed in workflow, needed
