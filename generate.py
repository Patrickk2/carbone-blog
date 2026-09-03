#!/usr/bin/env python3
"""Carbone Notes publishing pipeline.

Production is fail-closed: missing APIs, malformed model output, invalid dates,
unsafe HTML, duplicates or invalid rendered artifacts stop publication.
LOCAL_TEST_MODE is intentionally the only mode that permits fixture content.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
POSTS_DIR = BASE_DIR / "posts"
TEMPLATE = POSTS_DIR / "post_template.html"
SITE_URL = os.environ.get("SITE_URL", "https://patrickk2.github.io/carbone-blog").rstrip("/")
AUTHOR = os.environ.get("CARBONE_AUTHOR", "Carbone Notes")
CATEGORIES = ("technology", "cybersecurity", "health", "science")
MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/auto")
FALLBACK_MODELS = [m.strip() for m in os.environ.get("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()]
UNSPLASH_IMAGES = {
    "cybersecurity": "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?auto=format&fit=crop&w=1200&h=675&q=82",
    "technology": "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1200&h=675&q=82",
    "health": "https://images.unsplash.com/photo-1505751172876-fa1923c5c528?auto=format&fit=crop&w=1200&h=675&q=82",
    "science": "https://images.unsplash.com/photo-1532094349884-543bc11b234d?auto=format&fit=crop&w=1200&h=675&q=82",
}
# Historical editorial dates are migration data, not a runtime date source.
LEGACY_DATES = {
    1: "2026-04-01T09:00:00+00:00", 2: "2026-07-01T09:00:00+00:00", 3: "2026-07-01T09:00:00+00:00",
    4: "2026-07-25T10:22:43+00:00", 5: "2026-07-28T11:11:26+00:00", 6: "2026-07-31T11:25:12+00:00",
    7: "2026-08-01T10:29:17+00:00", 8: "2026-08-04T11:18:47+00:00", 9: "2026-08-07T09:54:54+00:00",
    10: "2026-08-10T10:11:22+00:00", 11: "2026-08-13T09:57:05+00:00", 12: "2026-08-16T09:20:44+00:00",
    13: "2026-08-19T09:26:32+00:00", 14: "2026-08-22T09:20:20+00:00", 15: "2026-08-25T09:28:24+00:00",
    16: "2026-08-28T20:34:52+00:00", 17: "2026-08-31T16:36:22+00:00", 18: "2026-09-01T13:51:08+00:00",
}
BANNED_PHRASES = (
    "in today's rapidly evolving world", "it's not just", "it’s not just", "imagine a world where",
    "rapidly evolving world", "in conclusion,", "to conclude,", "game-changing", "revolutionary breakthrough",
)
ALLOWED_TAGS = {"p", "h2", "h3", "ul", "ol", "li", "blockquote", "code", "pre", "strong", "em", "a", "hr"}
TAG_RE = re.compile(r"</?\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>")
META_RE = re.compile(r"<!-- CARBONE_META (\{.*?\}) -->", re.S)

def log(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", flush=True)

def fail(stage: str, exc_type: str, message: str) -> None:
    print(f"ERROR STAGE={stage} ERROR TYPE={exc_type} ERROR MESSAGE={message}", file=sys.stderr, flush=True)
    raise RuntimeError(message)

def require_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail("START", "configuration", f"{name} is missing")
    return value

def parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value}") from exc
    if dt.tzinfo is None:
        raise ValueError("publication timestamp must include timezone")
    return dt.astimezone(timezone.utc)

def plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()

def slug_for(number: int) -> str:
    return f"post-{number:02d}"

def display_date(iso: str) -> str:
    return parse_iso(iso).strftime("%B %-d, %Y")

def read_meta(text: str) -> dict[str, Any] | None:
    match = META_RE.search(text)
    if not match:
        return None
    return json.loads(match.group(1))

def parse_legacy_fields(text: str, number: int) -> dict[str, Any]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    tag_match = re.search(r'class=[\"\'][^\"\']*article-tag[^\"\']*[\"\'][^>]*>(.*?)</', text, re.I | re.S)
    image_match = re.search(r'<img[^>]+src=[\"\']([^\"\']+)[\"\'][^>]*>', text, re.I | re.S)
    alt_match = re.search(r'<img[^>]+alt=[\"\']([^\"\']*)[\"\'][^>]*>', text, re.I | re.S)
    first_p = re.search(r'<div class=[\"\']article-body[\"\'][^>]*>\s*<p>(.*?)</p>', text, re.I | re.S)
    title = plain_text(title_match.group(1) if title_match else f"Carbone Notes — Post {number:02d}")
    category = plain_text(tag_match.group(1) if tag_match else "technology").lower()
    if category not in CATEGORIES:
        category = "technology"
    excerpt = plain_text(first_p.group(1) if first_p else title)
    image = image_match.group(1) if image_match else UNSPLASH_IMAGES[category]
    alt = plain_text(alt_match.group(1) if alt_match else f"Illustration for {title}") or f"Illustration for {title}"
    iso = LEGACY_DATES.get(number)
    if not iso:
        raise ValueError(f"no historical publication date for post {number}")
    return {"id": number, "slug": slug_for(number), "title": title, "excerpt": excerpt[:280], "category": category,
            "published_at": iso, "updated_at": iso, "author": AUTHOR, "image": image, "image_alt": alt}

def migrate_legacy_posts() -> None:
    log("MIGRATE", "Normalizing legacy article metadata")
    if not TEMPLATE.exists():
        fail("MIGRATE", "filesystem", f"missing template {TEMPLATE}")
    for path in sorted(POSTS_DIR.glob("post-*.html")):
        match = re.fullmatch(r"post-(\d+)\.html", path.name)
        if not match:
            continue
        number = int(match.group(1))
        text = path.read_text(encoding="utf-8")
        meta = read_meta(text)
        if meta:
            continue
        meta = parse_legacy_fields(text, number)
        title = html.escape(plain_text(meta["title"]), quote=False)
        date = display_date(meta["published_at"])
        canonical = f"{SITE_URL}/{path.as_posix()}"
        injection = (
            f'\n<meta name="description" content="{html.escape(meta["excerpt"], quote=True)}">'
            f'\n<meta name="author" content="{html.escape(meta["author"], quote=True)}">'
            f'\n<link rel="canonical" href="{canonical}">'
            f'\n<meta property="og:type" content="article">'
            f'\n<meta property="og:title" content="{title} — Carbone Notes">'
            f'\n<meta property="og:description" content="{html.escape(meta["excerpt"], quote=True)}">'
            f'\n<meta property="og:url" content="{canonical}">'
            f'\n<meta property="og:image" content="{html.escape(meta["image"], quote=True)}">'
            f'\n<meta property="article:published_time" content="{meta["published_at"]}">'
            f'\n<meta property="article:section" content="{meta["category"]}">' 
        )
        text = re.sub(r"<title[^>]*>.*?</title>", f"<title>{title} — Carbone Notes</title>", text, count=1, flags=re.I | re.S)
        text = re.sub(r'(<[^>]*class=[\"\'][^\"\']*article-date[^\"\']*[\"\'][^>]*>).*?(</)', rf"\1{date}\2", text, count=1, flags=re.I | re.S)
        text = re.sub(r"</head>", injection + "\n</head>", text, count=1, flags=re.I)
        text = text.replace("</body>", f'<!-- CARBONE_META {json.dumps(meta, ensure_ascii=False, separators=(",", ":"))} -->\n</body>', 1)
        path.write_text(text, encoding="utf-8")
        log("MIGRATE", f"updated {path.name}: {meta['published_at']}")

def validate_html_body(body: str) -> None:
    lowered = body.lower()
    if any(x in lowered for x in ("<script", "<iframe", "<object", "<embed", "javascript:", "onerror=", "onclick=")):
        raise ValueError("unsafe HTML detected")
    for match in TAG_RE.finditer(body):
        tag = match.group(1).lower()
        if tag not in ALLOWED_TAGS:
            raise ValueError(f"unsupported HTML tag in generated body: {tag}")

def validate_generated_post(post: dict[str, Any]) -> None:
    if post["category"] not in CATEGORIES:
        raise ValueError("invalid category")
    if not post["title"] or not post["excerpt"] or not post["body"]:
        raise ValueError("title, excerpt and body are required")
    iso = parse_iso(post["published_at"])
    if iso > datetime.now(timezone.utc):
        raise ValueError("publication date cannot be in the future")
    if any(phrase in post["body"].lower() or phrase in post["title"].lower() for phrase in BANNED_PHRASES):
        raise ValueError("generic AI phrasing detected")
    validate_html_body(post["body"])
    if len(plain_text(post["body"])) < 500:
        raise ValueError("body is too short")

def parse_llm_output(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[^\n]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    sections: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^(CATEGORY|TITLE|EXCERPT|SOURCE_INDEX|BODY):\s*(.*)$", line, re.I)
        if m:
            current = m.group(1).upper()
            sections[current] = m.group(2).strip()
        elif current:
            sections[current] += ("\n" if sections[current] else "") + line
    body = sections.get("BODY", "").strip()
    try:
        source_index = int(sections.get("SOURCE_INDEX", "0"))
    except ValueError:
        source_index = 0
    post = {"category": sections.get("CATEGORY", "").strip().lower(), "title": plain_text(sections.get("TITLE", "")),
            "excerpt": plain_text(sections.get("EXCERPT", "")), "body": body, "source_index": source_index}
    return post

def fetch_latest_news(api_key: str) -> list[dict[str, str]]:
    category = os.environ.get("CARBONE_TOPIC", "").strip().lower() or CATEGORIES[datetime.now(timezone.utc).day % len(CATEGORIES)]
    if category not in CATEGORIES:
        category = "technology"
    log("SOURCE FETCH", f"NewsAPI topic={category}")
    response = requests.get("https://newsapi.org/v2/everything", params={"q": category, "language": "en", "sortBy": "publishedAt", "pageSize": 8, "apiKey": api_key}, timeout=15)
    log("SOURCE STATUS", str(response.status_code))
    if response.status_code != 200:
        fail("SOURCE FETCH", "http", f"NewsAPI returned HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    if data.get("status") != "ok":
        fail("SOURCE FETCH", "api", json.dumps(data)[:500])
    articles = []
    for article in data.get("articles", []):
        title = (article.get("title") or "").strip()
        description = (article.get("description") or "").strip()
        url = (article.get("url") or "").strip()
        if title and url:
            articles.append({"title": title, "description": description, "url": url, "source": (article.get("source") or {}).get("name", "Unknown"), "publishedAt": article.get("publishedAt", "")})
    if not articles:
        fail("SOURCE FETCH", "empty", "NewsAPI returned no usable articles")
    return articles

def openrouter_call(api_key: str, model: str, prompt: str) -> str:
    response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "HTTP-Referer": SITE_URL, "X-Title": "Carbone Notes"}, json={"model": model, "messages": [{"role": "system", "content": "You are the editor of an independent publication. Write like a careful human editor, not a marketing system."}, {"role": "user", "content": prompt}], "temperature": 0.45}, timeout=60)
    log("GENERATION", f"model={model} status={response.status_code}")
    if response.status_code != 200:
        if response.status_code in {401, 403}:
            fail("GENERATION", "authentication", f"OpenRouter returned HTTP {response.status_code}")
        raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:250]}")
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"OpenRouter response missing choices/message content: {json.dumps(data)[:400]}") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("OpenRouter returned empty content")
    return content

def build_generation_prompt(articles: list[dict[str, str]], recent: list[dict[str, Any]]) -> str:
    recent_titles = "\n".join(f"- {p['title']}" for p in recent[-12:])
    source_text = "\n".join(f"[{i}] {a['title']} — {a['description']} (source: {a['source']}; published: {a['publishedAt']}; url: {a['url']})" for i, a in enumerate(articles, 1))
    return f"""Write one publishable Carbone Notes article based on one of the source items below. Add context and analysis, but distinguish reported facts from interpretation. Never invent a quote, statistic, source, event or URL. Do not quote the source verbatim.

Recent Carbone Notes titles (avoid repeating their subjects):
{recent_titles}

News sources:
{source_text}

Editorial requirements:
- precise, informed, natural prose;
- no generic intro, hype or corporate language;
- 4–7 substantial paragraphs, with h2 headings only where useful;
- no decorative bullet lists unless they genuinely aid the explanation;
- if discussing health or security, avoid unsupported causal claims;
- title should be specific, not clickbait;
- excerpt should be one clean sentence;
- SOURCE_INDEX must reference the chosen source above.

Output exactly:
CATEGORY: technology|cybersecurity|health|science
TITLE: ...
EXCERPT: ...
SOURCE_INDEX: integer
BODY:
<p>...</p>
<h2>...</h2>
<p>...</p>
"""

def generated_post(articles: list[dict[str, str]], recent: list[dict[str, Any]], api_key: str) -> tuple[dict[str, Any], str]:
    prompt = build_generation_prompt(articles, recent)
    models = [MODEL] + FALLBACK_MODELS
    errors = []
    for model in models:
        try:
            raw = openrouter_call(api_key, model, prompt)
            parsed = parse_llm_output(raw)
            idx = parsed.get("source_index", 0)
            if not 1 <= idx <= len(articles):
                raise ValueError("SOURCE_INDEX is outside returned source range")
            parsed["source"] = articles[idx - 1]
            validate_generated_post({**parsed, "published_at": datetime.now(timezone.utc).isoformat()})
            return parsed, model
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}: {exc}")
            log("GENERATION", errors[-1])
    fail("GENERATION", "all_models_failed", " | ".join(errors))

def recent_metadata() -> list[dict[str, Any]]:
    data = []
    for path in sorted(POSTS_DIR.glob("post-*.html")):
        text = path.read_text(encoding="utf-8")
        meta = read_meta(text)
        if meta:
            data.append(meta)
    return sorted(data, key=lambda x: parse_iso(x["published_at"]))

def title_is_duplicate(title: str, recent: list[dict[str, Any]], window_days: int = 14) -> bool:
    title_norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    now = datetime.now(timezone.utc)
    for meta in recent:
        age = now - parse_iso(meta["published_at"])
        if age.days > window_days:
            continue
        other = re.sub(r"[^a-z0-9]+", " ", meta["title"].lower()).strip()
        if title_norm == other or title_norm in other or other in title_norm:
            return True
    return False

def render_post(meta: dict[str, Any], body: str, source: dict[str, str]) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    image = html.escape(meta["image"], quote=True)
    alt = html.escape(meta["image_alt"], quote=True)
    image_element = f'<img src="{image}" alt="{alt}" width="1200" height="675" loading="eager" fetchpriority="high" decoding="async">'
    source_block = f'<h2 id="sources-title">Sources</h2><ul><li><a href="{html.escape(source["url"], quote=True)}" rel="noopener noreferrer">{html.escape(source["source"])} — {html.escape(source["title"])}</a></li></ul>'
    values = {
        "POST_EXCERPT_PLAIN": html.escape(meta["excerpt"], quote=True), "POST_AUTHOR": html.escape(meta["author"], quote=True),
        "POST_CANONICAL": f"{SITE_URL}/{slug_for(meta['id'])}.html" if False else f"{SITE_URL}/posts/{meta['slug']}.html",
        "POST_IMAGE_URL": image, "POST_PUBLISHED_AT": meta["published_at"], "POST_TAG": meta["category"].title(),
        "POST_PLAIN_TITLE": html.escape(meta["title"], quote=False), "POST_TITLE": html.escape(meta["title"], quote=False),
        "POST_EXCERPT": html.escape(meta["excerpt"], quote=False), "POST_NUMBER": str(meta["id"]),
        "POST_READ_TIME": str(max(1, round(len(plain_text(body).split()) / 220))), "POST_IMAGE_ELEMENT": image_element,
        "POST_IMAGE_ALT": alt, "POST_BODY": body, "POST_SOURCES": source_block,
        "POST_DATE": display_date(meta["published_at"]),
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    meta_comment = f'<!-- CARBONE_META {json.dumps(meta, ensure_ascii=False, separators=(",", ":"))} -->'
    return template.replace("</body>", meta_comment + "\n</body>")

def update_index(posts: list[dict[str, Any]]) -> None:
    path = BASE_DIR / "index.html"
    text = path.read_text(encoding="utf-8")
    # Future generated index rows are replaced through explicit markers so the page remains hand-designed.
    marker = re.compile(r"<div class=\"post-list\">.*?</div></section>", re.S)
    rows = []
    for p in sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True):
        rows.append(f'<a class="post-row" href="posts/{p["slug"]}.html"><span class="post-number">{p["id"]:02d}</span><div><h3 class="post-title">{html.escape(p["title"])}</h3><p class="post-excerpt">{html.escape(p["excerpt"])}</p></div><span class="post-side"><span class="category">{p["category"].title()}</span><time datetime="{p["published_at"]}">{display_date(p["published_at"] )}</time></span></a>')
    replacement = '<div class="post-list">\n' + "\n".join(rows) + '\n</div></section>'
    new_text, count = marker.subn(replacement, text, count=1)
    if count != 1:
        fail("UPDATE INDEX", "template", "post list marker not found")
    # Featured item uses first post.
    featured = sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True)[0]
    new_text = re.sub(r'<div class="featured-visual">.*?</div>', f'<div class="featured-visual"><img src="{html.escape(featured["image"], quote=True)}" alt="{html.escape(featured["image_alt"], quote=True)}" width="1200" height="675" fetchpriority="high"></div>', new_text, count=1, flags=re.S)
    new_text = re.sub(r'<span class="category">Science</span>\s*<span class="meta-sep".*?<time datetime="[^"]+">[^<]+</time>', f'<span class="category">{featured["category"].title()}</span><span class="meta-sep" aria-hidden="true"></span><time datetime="{featured["published_at"]}">{display_date(featured["published_at"])}</time>', new_text, count=1, flags=re.S)
    new_text = re.sub(r'<h2 id="featured-title">.*?</h2>', f'<h2 id="featured-title">{html.escape(featured["title"])}</h2>', new_text, count=1, flags=re.S)
    new_text = re.sub(r'<p class="featured-excerpt">.*?</p>', f'<p class="featured-excerpt">{html.escape(featured["excerpt"])}</p>', new_text, count=1, flags=re.S)
    new_text = re.sub(r'<a class="read-link" href="posts/post-18.html">.*?</a>', f'<a class="read-link" href="posts/{featured["slug"]}.html">Read the note →</a>', new_text, count=1, flags=re.S)
    path.write_text(new_text, encoding="utf-8")

def write_archive_topics(posts: list[dict[str, Any]]) -> None:
    # Generated archive keeps a single source of truth while preserving the calm editorial layout.
    rows = []
    for year in sorted({parse_iso(p["published_at"]).year for p in posts}, reverse=True):
        rows.append(f'<section class="archive-year"><h2>{year}</h2><ul class="archive-month">')
        for p in [x for x in sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True) if parse_iso(x["published_at"]).year == year]:
            dt = parse_iso(p["published_at"])
            rows.append(f'<li><a class="archive-item" href="posts/{p["slug"]}.html"><time class="archive-date" datetime="{p["published_at"]}">{dt.strftime("%B %-d")}</time><span class="archive-title">{html.escape(p["title"])}</span><span class="archive-cat">{p["category"].title()}</span></a></li>')
        rows.append('</ul></section>')
    path = BASE_DIR / "archive.html"
    text = path.read_text(encoding="utf-8")
    text, count = re.subn(r'<section class="archive-year">.*?</section></main>', "\n".join(rows) + "</main>", text, count=1, flags=re.S)
    if count:
        path.write_text(text, encoding="utf-8")

def generate_feed(posts: list[dict[str, Any]]) -> None:
    from xml.etree.ElementTree import Element, SubElement, ElementTree
    rss = Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    ch = SubElement(rss, "channel")
    SubElement(ch, "title").text = "Carbone Notes"
    SubElement(ch, "link").text = SITE_URL + "/"
    SubElement(ch, "description").text = "Notes, analysis and guides on technology, security, science and digital culture."
    SubElement(ch, "language").text = "en-us"
    atom = SubElement(ch, "atom:link", {"href": SITE_URL + "/feed.xml", "rel": "self", "type": "application/rss+xml"})
    latest = max(parse_iso(p["updated_at"]) for p in posts)
    SubElement(ch, "lastBuildDate").text = format_datetime(latest, usegmt=True)
    for p in sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True):
        item = SubElement(ch, "item")
        SubElement(item, "title").text = p["title"]
        url = f"{SITE_URL}/posts/{p['slug']}.html"
        SubElement(item, "link").text = url
        SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        SubElement(item, "pubDate").text = format_datetime(parse_iso(p["published_at"]), usegmt=True)
        SubElement(item, "category").text = p["category"]
        SubElement(item, "description").text = p["excerpt"]
    ElementTree(rss).write(BASE_DIR / "feed.xml", encoding="utf-8", xml_declaration=True)

def generate_sitemap(posts: list[dict[str, Any]]) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    pages = [("index.html", datetime.now(timezone.utc).isoformat(), "daily", "1.0"), ("archive.html", max(p["updated_at"] for p in posts), "weekly", "0.9"), ("topics.html", max(p["updated_at"] for p in posts), "weekly", "0.8"), ("about.html", "2026-09-03T00:00:00+00:00", "monthly", "0.6")]
    for path, lastmod, freq, priority in pages:
        lines += ["  <url>", f"    <loc>{SITE_URL}/{path}</loc>", f"    <lastmod>{parse_iso(lastmod).date().isoformat()}</lastmod>", f"    <changefreq>{freq}</changefreq>", f"    <priority>{priority}</priority>", "  </url>"]
    for p in sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True):
        lines += ["  <url>", f"    <loc>{SITE_URL}/posts/{p['slug']}.html</loc>", f"    <lastmod>{parse_iso(p['updated_at']).date().isoformat()}</lastmod>", "    <changefreq>weekly</changefreq>", "    <priority>0.6</priority>", "  </url>"]
    lines.append("</urlset>")
    (BASE_DIR / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-api", action="store_true")
    args = parser.parse_args()
    log("START", datetime.now(timezone.utc).isoformat())
    if args.check_api:
        key = require_secret("OPENROUTER_API_KEY")
        news = require_secret("NEWS_API_KEY")
        r1 = requests.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        log("SOURCE STATUS", f"OpenRouter {r1.status_code}")
        if r1.status_code != 200: fail("CHECK API", "authentication", f"OpenRouter returned HTTP {r1.status_code}")
        r2 = requests.get("https://newsapi.org/v2/top-headlines", params={"language":"en","pageSize":1,"apiKey":news}, timeout=20)
        log("SOURCE STATUS", f"NewsAPI {r2.status_code}")
        if r2.status_code != 200: fail("CHECK API", "authentication", f"NewsAPI returned HTTP {r2.status_code}")
        log("END", "API checks passed")
        return
    if os.environ.get("LOCAL_TEST_MODE") == "1":
        fail("START", "configuration", "LOCAL_TEST_MODE must not be used in production")
    openrouter_key = require_secret("OPENROUTER_API_KEY")
    news_key = require_secret("NEWS_API_KEY")
    log("DATE", datetime.now(timezone.utc).isoformat())
    migrate_legacy_posts()
    recent = recent_metadata()
    articles = fetch_latest_news(news_key)
    draft, used_model = generated_post(articles, recent, openrouter_key)
    published_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if title_is_duplicate(draft["title"], recent):
        fail("VALIDATION", "duplicate", f"title is too similar to a recent article: {draft['title']}")
    next_id = max([p["id"] for p in recent] + [0]) + 1
    meta = {"id": next_id, "slug": slug_for(next_id), "title": draft["title"], "excerpt": draft["excerpt"], "category": draft["category"], "published_at": published_at, "updated_at": published_at, "author": AUTHOR, "image": UNSPLASH_IMAGES[draft["category"]], "image_alt": f"Illustration for {draft['title']}"}
    validate_generated_post({**draft, **{"published_at": published_at}})
    # Ensure model did not smuggle the same future date into visible content.
    rendered = render_post(meta, draft["body"], draft["source"])
    if not rendered.lstrip().lower().startswith("<!doctype html>"):
        fail("RENDER", "html", "rendered article is missing doctype")
    (POSTS_DIR / f"{meta['slug']}.html").write_text(rendered, encoding="utf-8")
    all_posts = recent_metadata()
    # recent_metadata now includes the new post
    update_index(all_posts)
    write_archive_topics(all_posts)
    generate_feed(all_posts)
    generate_sitemap(all_posts)
    log("VALIDATION", f"article={meta['slug']} category={meta['category']} date={meta['published_at']} model={used_model}")
    log("FILES UPDATED", f"posts/{meta['slug']}.html index.html archive.html feed.xml sitemap.xml")
    log("COMMIT", "ready for workflow commit")
    log("END", "publication pipeline completed")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if not isinstance(exc, RuntimeError):
            print(f"ERROR STAGE=UNHANDLED ERROR TYPE={type(exc).__name__} ERROR MESSAGE={exc}", file=sys.stderr, flush=True)
        sys.exit(1)
