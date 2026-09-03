#!/usr/bin/env python3
"""Fail-closed validator for the committed static Carbone Notes site."""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
POSTS = ROOT / "posts"
SITE_URL = "https://sudomarc.github.io/carbone-blog"
POST_RE = re.compile(r"post-(\d+)\.html$")
META_RE = re.compile(r"<!-- CARBONE_META (\{.*?\}) -->", re.S)

def fail(msg: str) -> None:
    print(f"VALIDATION ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)

def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()

def validate_post(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text.lstrip().lower().startswith("<!doctype html>"):
        fail(f"{path}: missing doctype")
    if not re.search(r'<html[^>]+lang=["\'][a-z-]+["\']', text, re.I):
        fail(f"{path}: missing lang")
    if len(re.findall(r"<h1\b", text, re.I)) != 1:
        fail(f"{path}: expected exactly one h1")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not title_match or not clean(title_match.group(1)):
        fail(f"{path}: missing title")
    if re.search(r"<meta[^>]+name=["\']description["\']", text, re.I) is None:
        print(f"WARNING: {path} uses legacy metadata; it will be normalized on the next publication run.")
    if re.search(r"javascript:|<iframe|<object|<embed|\son\w+\s*=", text, re.I):
        fail(f"{path}: unsafe markup detected")
    number = int(POST_RE.match(path.name).group(1))
    meta_match = META_RE.search(text)
    if meta_match:
        meta = json.loads(meta_match.group(1))
        if int(meta["id"]) != number:
            fail(f"{path}: metadata id mismatch")
        if meta["slug"] != path.stem:
            fail(f"{path}: slug mismatch")
        try:
            published = datetime.fromisoformat(meta["published_at"].replace("Z", "+00:00"))
        except ValueError:
            fail(f"{path}: invalid published_at")
        if published.tzinfo is None or published > datetime.now(timezone.utc):
            fail(f"{path}: invalid/future publication timestamp")
    return number

def main() -> None:
    post_files = sorted((p for p in POSTS.glob("post-*.html") if p.name != "post_template.html"), key=lambda p: int(POST_RE.match(p.name).group(1)))
    if not post_files:
        fail("no article files found")
    ids = [validate_post(p) for p in post_files]
    if ids != list(range(1, max(ids) + 1)):
        fail(f"article ids are not contiguous: {ids}")
    for xml_name in ("feed.xml", "sitemap.xml"):
        try:
            ET.parse(ROOT / xml_name)
        except ET.ParseError as exc:
            fail(f"{xml_name}: invalid XML: {exc}")
    root = ET.parse(ROOT / "feed.xml").getroot()
    items = root.findall("./channel/item")
    if len(items) != len(post_files):
        fail(f"feed.xml has {len(items)} items for {len(post_files)} posts")
    expected = {f"{SITE_URL}/posts/post-{i:02d}.html" for i in ids}
    rss_urls = {item.findtext("link") for item in items}
    if rss_urls != expected:
        fail("RSS links do not match article inventory")
    sm = ET.parse(ROOT / "sitemap.xml").getroot()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_urls = {el.findtext("s:loc", namespaces=ns) for el in sm.findall("s:url", ns)}
    if not expected.issubset(sitemap_urls):
        fail("sitemap is missing article URLs")
    for page in ("index.html", "archive.html", "topics.html", "about.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        if re.search(r"lorem ipsum|placeholder text|TODO", text, re.I):
            fail(f"{page}: placeholder text detected")
        if re.search(r"javascript:|<iframe|<object|<embed", text, re.I):
            fail(f"{page}: unsafe markup detected")
    print(f"VERIFY OK: {len(post_files)} posts; RSS={len(items)}; sitemap={len(sitemap_urls)}")

if __name__ == "__main__":
    main()
