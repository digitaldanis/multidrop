"""The website (tools/build_site.py, site/strings.toml, site/guides.toml)."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_language_has_every_text():
    strings = tomllib.loads((ROOT / "site" / "strings.toml").read_text(encoding="utf-8"))
    en = strings["en"]
    for lang, t in strings.items():
        assert set(en) - {"dir"} <= set(t), lang
        assert len(t["features"]) == len(en["features"]), lang
        assert len(t["faq"]) == len(en["faq"]) and all(len(qa) == 2 for qa in t["faq"]), lang


def test_build_writes_pages_with_valid_structured_data(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "tools" / "build_site.py"), str(tmp_path)],
                   check=True, capture_output=True)
    guides = tomllib.loads((ROOT / "site" / "guides.toml").read_text(encoding="utf-8"))["guide"]
    slugs = {g["slug"] for g in guides}
    pages = sorted(tmp_path.rglob("index.html"))
    assert len(pages) >= 18 + len(guides)
    for page in pages:
        text = page.read_text(encoding="utf-8")
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
        types = {json.loads(b)["@type"] for b in blocks}
        if page.parent.name in slugs:
            assert {"TechArticle", "BreadcrumbList"} <= types
        else:
            assert types == {"SoftwareApplication", "FAQPage"}
            assert 'hreflang="x-default"' in text
        assert all(f"/{slug}/" in text for slug in slugs - {page.parent.name})
    assert 'dir="rtl"' in (tmp_path / "ar" / "index.html").read_text(encoding="utf-8")
    sitemap = (tmp_path / "sitemap.xml").read_text()
    assert sitemap.count("<loc>") == len(pages)
    llms = (tmp_path / "llms.txt").read_text()
    assert "AirDrop" in llms and all(f"/{slug}/" in llms for slug in slugs)


def test_guides_are_complete():
    guides = tomllib.loads((ROOT / "site" / "guides.toml").read_text(encoding="utf-8"))["guide"]
    assert len({g["slug"] for g in guides}) == len(guides)
    for g in guides:
        assert {"slug", "card", "teaser", "platform", "title", "description", "h1", "lead", "sections",
                "faq"} <= set(g), g.get("slug")
        assert g["platform"] in {"linux", "windows", "both"}, g["slug"]
        assert re.fullmatch(r"[a-z0-9-]+", g["slug"])
        assert len(g["description"]) <= 170, g["slug"]


def test_readme_language_block_is_current():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    strings = tomllib.loads((ROOT / "site" / "strings.toml").read_text(encoding="utf-8"))
    for lang, t in strings.items():
        if lang != "en":
            assert t["summary"] in readme, f"README.md is stale for {lang}: tools/build_site.py --readme"
