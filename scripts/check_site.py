#!/usr/bin/env python3
"""Static checks for the salassaavedra.org site.

Usage:
  python3 scripts/check_site.py
  python3 scripts/check_site.py --base-url https://example.netlify.app
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".html", ".css", ".js", ".xml", ".txt"}
SKIP_DIRS = {".git", "scripts"}
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
PAGE_FOR_PRETTY = {
    "/": "index.html",
    "/research": "research.html",
    "/publications": "publications.html",
    "/projects": "projects.html",
    "/teaching": "teaching.html",
    "/talks": "talks.html",
    "/contact": "contact.html",
}
EMAIL_FILE = ROOT / "assets" / "site-email.js"
PLACEHOLDER = "REPLACE-ME@spicelab.cl"


class RefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: v for k, v in attrs if v is not None}
        for key in ("href", "src"):
            if key in attr_map:
                self.refs.append((key, attr_map[key]))


def iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def local_target(page: Path, ref: str) -> Path | None:
    raw = ref.strip()
    if not raw or raw.startswith(("#", "mailto:", "tel:", "data:", "javascript:")):
        return None
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return None
    path = unquote(parsed.path)
    if not path:
        return None
    if path.startswith("/"):
        rel = path.lstrip("/")
        if rel in {"", "index.html"}:
            return ROOT / "index.html"
        candidate = ROOT / rel
        if rel in {v for v in PAGE_FOR_PRETTY.values()} or candidate.suffix:
            return candidate
        mapped = PAGE_FOR_PRETTY.get("/" + rel)
        if mapped:
            return ROOT / mapped
        return candidate
    return (page.parent / path).resolve()


def check_text(errors: list[str]) -> None:
    princeton_email = re.compile(r"@" + "princeton.edu", re.I)
    placeholder_hits: list[str] = []
    for path in iter_text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        if princeton_email.search(text) or "ms3407" in text.lower():
            errors.append(f"Princeton email still present in {rel}")
        if "msalassaavedra.e@gmail.com" in text:
            errors.append(f"old Gmail address still present in {rel}")
        in_review = re.compile(r"\bin\s+" + "review" + r"\b", re.I)
        if in_review.search(text):
            errors.append(f"'In review' still present in {rel}")
        if PLACEHOLDER in text:
            placeholder_hits.append(rel)
    if placeholder_hits != ["assets/site-email.js"]:
        errors.append(
            "placeholder "
            + PLACEHOLDER
            + " should appear only in assets/site-email.js, found: "
            + (", ".join(placeholder_hits) or "(nowhere)")
        )
    email_js = EMAIL_FILE.read_text(encoding="utf-8")
    if f'window.SITE_EMAIL = "{PLACEHOLDER}"' not in email_js:
        errors.append("SITE_EMAIL constant missing from assets/site-email.js")
    contact = (ROOT / "contact.html").read_text(encoding="utf-8")
    if 'link.href = "mailto:" + window.SITE_EMAIL' not in contact:
        errors.append("contact.html does not build a mailto: from SITE_EMAIL")
    if "x.com/M_SalasSaavedra" not in (ROOT / "index.html").read_text(encoding="utf-8"):
        errors.append("homepage JSON-LD/page is missing the X profile")


def check_refs(errors: list[str]) -> None:
    for page in sorted(ROOT.glob("*.html")):
        parser = RefParser()
        parser.feed(page.read_text(encoding="utf-8"))
        for _kind, ref in parser.refs:
            target = local_target(page, ref)
            if target is None:
                continue
            try:
                target.relative_to(ROOT)
            except ValueError:
                errors.append(f"{page.name} links outside the site: {ref}")
                continue
            if not target.is_file():
                errors.append(f"{page.name} -> missing {ref}")


def check_sitemap(errors: list[str]) -> list[str]:
    sitemap = ROOT / "sitemap.xml"
    tree = ET.parse(sitemap)
    locs = [node.text.strip() for node in tree.findall(".//sm:loc", SITEMAP_NS) if node.text]
    if not locs:
        errors.append("sitemap.xml has no <loc> entries")
        return []
    for loc in locs:
        parsed = urlparse(loc)
        if parsed.scheme != "https" or parsed.netloc != "salassaavedra.org":
            errors.append(f"sitemap URL is not a canonical https://salassaavedra.org path: {loc}")
            continue
        if parsed.path.endswith(".html"):
            errors.append(f"sitemap URL still uses a .html path: {loc}")
        mapped = PAGE_FOR_PRETTY.get(parsed.path)
        if not mapped:
            errors.append(f"sitemap URL has no local page: {loc}")
            continue
        if not (ROOT / mapped).is_file():
            errors.append(f"sitemap URL {loc} does not match a file ({mapped})")
    return locs


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def check_remote(base_url: str, locs: list[str], errors: list[str]) -> None:
    base = base_url.rstrip("/")
    opener = build_opener(_NoRedirect)

    def fetch(url: str) -> tuple[int, str]:
        request = Request(url, method="GET")
        try:
            with opener.open(request, timeout=30) as response:
                return response.status, response.geturl()
        except HTTPError as exc:
            location = exc.headers.get("Location", "") if exc.headers else ""
            return exc.code, location or url
        except Exception as exc:  # noqa: BLE001
            errors.append(f"request failed for {url}: {exc}")
            return 0, url

    for loc in locs:
        path = urlparse(loc).path or "/"
        url = base + "/" if path == "/" else base + path
        status, _final = fetch(url)
        if status != 200:
            errors.append(f"preview {url} returned {status}")
        html_url = url.rstrip("/") + ".html" if path != "/" else base + "/index.html"
        status_html, location = fetch(html_url)
        expected = "/" if path == "/" else path
        if status_html not in {301, 302}:
            errors.append(f"preview {html_url} returned {status_html}, expected a redirect to {expected}")
        elif expected != "/" and expected not in location:
            errors.append(f"preview {html_url} redirected to {location}, expected {expected}")
        elif expected == "/" and location not in {"/", base, base + "/"} and not location.rstrip("/").endswith(urlparse(base).netloc):
            errors.append(f"preview {html_url} redirected to {location}, expected /")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", help="Deploy preview origin, no trailing path")
    args = parser.parse_args()
    errors: list[str] = []
    check_text(errors)
    check_refs(errors)
    locs = check_sitemap(errors)
    if args.base_url:
        check_remote(args.base_url, locs, errors)
    if errors:
        print(f"{len(errors)} check(s) failed:")
        for item in errors:
            print(f"  - {item}")
        return 1
    print(f"OK: text, internal files, and {len(locs)} sitemap URLs")
    if args.base_url:
        print(f"OK: preview {args.base_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
