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
REDIRECT_MARKER = "CARBONE_REDIRECT"

def fail(msg: str) -> None:
    print(f"VALIDATION ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)

def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()

def read_meta(text: str) -> dict | None:
    match = META_RE.search(text)
    return json.loads(match.group(1)) if match else None

def is_redirect(text: str) -> bool:
    return REDIRECT_MARKER in text

def validate_post(path: Path) -> tuple[int, dict]:
    text = path.read_text(encoding="utf-8")
    if not text.lstrip().lower().startswith("<!doctype html>"):
        fail(f"{path}: missing doctype")
    if not re.search(r'''<html[^>]+lang=["'][a-z-]+["']''', text, re.I):
        fail(f"{path}: missing lang")
    if len(re.findall(r"<h1\b", text, re.I)) != 1:
        fail(f"{path}: expected exactly one h1")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not title_match or not clean(title_match.group(1)):
        fail(f"{path}: missing title")
    if re.search(r"javascript:|<iframe|<object|<embed|\son\w+\s*=", text, re.I):
        fail(f"{path}: unsafe markup detected")
    match = POST_RE.match(path.name)
    if not match:
        fail(f"{path}: invalid article filename")
    number = int(match.group(1))
    meta = read_meta(text)
    if meta is None:
        fail(f"{path}: canonical article is missing CARBONE_META")
    if int(meta.get("id", -1)) != number:
        fail(f"{path}: metadata id mismatch")
    if meta.get("slug") != path.stem:
        fail(f"{path}: slug mismatch")
    if meta.get("category") not in {"technology", "cybersecurity", "health", "science"}:
        fail(f"{path}: invalid category")
    if meta.get("author") == "":
        fail(f"{path}: missing author")
    try:
        published = datetime.fromisoformat(str(meta["published_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        fail(f"{path}: invalid published_at")
    if published.tzinfo is None or published > datetime.now(timezone.utc):
        fail(f"{path}: invalid/future publication timestamp")
    canonical = f"{SITE_URL}/posts/{path.name}"
    if not re.search(rf'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']{re.escape(canonical)}["\']', text, re.I):
        fail(f"{path}: canonical URL mismatch")
    return number, meta

def main() -> None:
    candidates = sorted(POSTS.glob("post-*.html"), key=lambda p: int(POST_RE.match(p.name).group(1)) if POST_RE.match(p.name) else 10**9)
    canonical = []
    redirects = []
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        if is_redirect(text):
            if not re.search(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']noindex,?\s*follow["\']', text, re.I):
                fail(f"{path}: redirect must be noindex")
            redirects.append(path)
            continue
        canonical.append(validate_post(path))
    if not canonical:
        fail("no canonical articles found")
    ids = [number for number, _ in canonical]
    if len(ids) != len(set(ids)):
        fail("duplicate article ids")
    titles = [meta["title"].strip().lower() for _, meta in canonical]
    if len(titles) != len(set(titles)):
        fail("duplicate canonical article titles")
    rss_path = ROOT / "feed.xml"
    sitemap_path = ROOT / "sitemap.xml"
    try:
        feed = ET.parse(rss_path).getroot()
        sitemap = ET.parse(sitemap_path).getroot()
    except ET.ParseError as exc:
        fail(f"invalid XML: {exc}")
    items = feed.findall("./channel/item")
    expected_urls = {f"{SITE_URL}/posts/post-{n:02d}.html" for n in ids}
    rss_urls = {item.findtext("link") for item in items}
    if rss_urls != expected_urls or len(items) != len(expected_urls):
        fail("RSS inventory does not match canonical article inventory")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_urls = {el.findtext("s:loc", namespaces=ns) for el in sitemap.findall("s:url", ns)}
    if not expected_urls.issubset(sitemap_urls):
        fail("sitemap is missing canonical article URLs")
    for page in ("index.html", "archive.html", "topics.html", "about.html"):
        path = ROOT / page
        text = path.read_text(encoding="utf-8")
        if re.search(r"lorem ipsum|placeholder text|TODO", text, re.I):
            fail(f"{page}: placeholder text detected")
        if re.search(r"javascript:|<iframe|<object|<embed|\son\w+\s*=", text, re.I):
            fail(f"{page}: unsafe markup detected")
    print(f"VERIFY OK: canonical_posts={len(canonical)} redirects={len(redirects)} RSS={len(items)} sitemap={len(sitemap_urls)}")

if __name__ == "__main__":
    main()
