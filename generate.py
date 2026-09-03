#!/usr/bin/env python3
"""Carbone Notes publishing pipeline.

The production pipeline is fail-closed: configuration, source retrieval,
generation, metadata, HTML, duplicates and rendered artifacts are validated
before the workflow is allowed to commit a new edition.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import requests

BASE_DIR = Path(__file__).resolve().parent
POSTS_DIR = BASE_DIR / "posts"
TEMPLATE = POSTS_DIR / "post_template.html"
SITE_URL = os.environ.get("SITE_URL", "https://sudomarc.github.io/carbone-blog").rstrip("/")
AUTHOR = os.environ.get("CARBONE_AUTHOR", "Carbone Notes")
CATEGORIES = ("technology", "cybersecurity", "health", "science")
MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/auto")
FALLBACK_MODELS = [x.strip() for x in os.environ.get("OPENROUTER_FALLBACK_MODELS", "").split(",") if x.strip()]
UNSPLASH_IMAGES = {
    "cybersecurity": "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?auto=format&fit=crop&w=1200&h=675&q=82",
    "technology": "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1200&h=675&q=82",
    "health": "https://images.unsplash.com/photo-1505751172876-fa1923c5c528?auto=format&fit=crop&w=1200&h=675&q=82",
    "science": "https://images.unsplash.com/photo-1532094349884-543bc11b234d?auto=format&fit=crop&w=1200&h=675&q=82",
}
LEGACY_DATES = {
    1: "2026-04-01T09:00:00+00:00", 2: "2026-07-01T09:00:00+00:00", 3: "2026-07-01T09:00:00+00:00",
    4: "2026-07-25T10:22:43+00:00", 5: "2026-07-28T11:11:26+00:00", 6: "2026-07-31T11:25:12+00:00",
    7: "2026-08-01T10:29:17+00:00", 8: "2026-08-04T11:18:47+00:00", 9: "2026-08-07T09:54:54+00:00",
    10: "2026-08-10T10:11:22+00:00", 11: "2026-08-13T09:57:05+00:00", 12: "2026-08-16T09:20:44+00:00",
    13: "2026-08-19T09:26:32+00:00", 14: "2026-08-22T09:20:20+00:00", 15: "2026-08-25T09:28:24+00:00",
    16: "2026-08-28T20:34:52+00:00", 17: "2026-08-31T16:36:22+00:00", 18: "2026-09-01T13:51:08+00:00",
}
REDIRECT_MARKER = "CARBONE_REDIRECT"
META_RE = re.compile(r"<!-- CARBONE_META (\{.*?\}) -->", re.S)
TAG_RE = re.compile(r"</?\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>")
ALLOWED_TAGS = {"p", "h2", "h3", "ul", "ol", "li", "blockquote", "code", "pre", "strong", "em", "a", "hr"}
BANNED_PHRASES = (
    "in today's rapidly evolving world", "it's not just", "it’s not just", "imagine a world where",
    "rapidly evolving world", "in conclusion,", "to conclude,", "game-changing", "revolutionary breakthrough",
)


def log(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", flush=True)


def fail(stage: str, kind: str, message: str) -> None:
    print(f"ERROR STAGE={stage} ERROR TYPE={kind} ERROR MESSAGE={message}", file=sys.stderr, flush=True)
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


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def slug_for(number: int) -> str:
    return f"post-{number:02d}"


def display_date(value: str) -> str:
    return parse_iso(value).strftime("%B %-d, %Y")


def read_meta(text: str) -> dict[str, Any] | None:
    match = META_RE.search(text)
    if not match:
        return None
    return json.loads(match.group(1))


def is_redirect(text: str) -> bool:
    return REDIRECT_MARKER in text


def recent_metadata() -> list[dict[str, Any]]:
    result = []
    for path in sorted(POSTS_DIR.glob("post-*.html")):
        text = path.read_text(encoding="utf-8")
        if is_redirect(text):
            continue
        meta = read_meta(text)
        if meta:
            result.append(meta)
    return sorted(result, key=lambda x: parse_iso(x["published_at"]))


def migrate_legacy_posts() -> None:
    if not TEMPLATE.exists():
        fail("MIGRATE", "filesystem", f"missing template {TEMPLATE}")
    for path in sorted(POSTS_DIR.glob("post-*.html")):
        if path.name == TEMPLATE.name:
            continue
        match = re.fullmatch(r"post-(\d+)\.html", path.name)
        if not match:
            continue
        text = path.read_text(encoding="utf-8")
        if is_redirect(text) or read_meta(text):
            continue
        number = int(match.group(1))
        iso = LEGACY_DATES.get(number)
        if not iso:
            fail("MIGRATE", "metadata", f"no historical date for {path.name}")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        tag_match = re.search(r'class=["\'][^"\']*article-tag[^"\']*["\'][^>]*>(.*?)</', text, re.I | re.S)
        image_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text, re.I)
        alt_match = re.search(r'<img[^>]+alt=["\']([^"\']*)["\']', text, re.I)
        first_p = re.search(r'<div class=["\']article-body["\'][^>]*>\s*<p>(.*?)</p>', text, re.I | re.S)
        title = clean_text(title_match.group(1) if title_match else f"Carbone Notes — Post {number:02d}")
        category = clean_text(tag_match.group(1) if tag_match else "technology").lower()
        if category not in CATEGORIES:
            category = "technology"
        excerpt = clean_text(first_p.group(1) if first_p else title)[:280]
        image = image_match.group(1) if image_match else UNSPLASH_IMAGES[category]
        image_alt = clean_text(alt_match.group(1) if alt_match else f"Illustration for {title}") or f"Illustration for {title}"
        meta = {"id": number, "slug": slug_for(number), "title": title, "excerpt": excerpt, "category": category,
                "published_at": iso, "updated_at": iso, "author": AUTHOR, "image": image, "image_alt": image_alt}
        canonical = f"{SITE_URL}/posts/{path.name}"
        injection = (
            f'\n<meta name="description" content="{html.escape(excerpt, quote=True)}">'
            f'\n<meta name="author" content="{html.escape(AUTHOR, quote=True)}">'
            f'\n<link rel="canonical" href="{canonical}">'
            f'\n<meta property="article:published_time" content="{iso}">'
        )
        text = re.sub(r"<title[^>]*>.*?</title>", f"<title>{html.escape(title)} — Carbone Notes</title>", text, count=1, flags=re.I | re.S)
        text = re.sub(r"</head>", injection + "\n</head>", text, count=1, flags=re.I)
        text = text.replace("</body>", f'<!-- CARBONE_META {json.dumps(meta, ensure_ascii=False, separators=(",", ":"))} -->\n</body>', 1)
        path.write_text(text, encoding="utf-8")
        log("MIGRATE", f"normalized {path.name}")


def tokens(title: str) -> set[str]:
    stop = {"the", "a", "an", "of", "and", "to", "in", "on", "for", "with", "how", "what", "why", "is", "are", "from", "into", "over", "its", "this", "that"}
    return {x for x in re.findall(r"[a-z0-9]+", title.lower()) if x not in stop and len(x) > 2}


def similar_title(a: str, b: str) -> bool:
    na = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", a.lower())).strip()
    nb = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", b.lower())).strip()
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = tokens(a), tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.55


def title_is_duplicate(title: str, posts: list[dict[str, Any]], window_days: int = 180) -> bool:
    now = datetime.now(timezone.utc)
    return any((now - parse_iso(p["published_at"])).days <= window_days and similar_title(title, p["title"]) for p in posts)


def validate_html_body(body: str) -> None:
    lowered = body.lower()
    if any(x in lowered for x in ("<script", "<iframe", "<object", "<embed", "javascript:", "data:text/html", "onerror=", "onclick=")):
        raise ValueError("unsafe HTML detected")
    for match in TAG_RE.finditer(body):
        tag = match.group(1).lower()
        if tag not in ALLOWED_TAGS:
            raise ValueError(f"unsupported HTML tag: {tag}")
    for href in re.findall(r'<a\b[^>]+href=["\']([^"\']+)', body, re.I):
        if urlparse(href).scheme and urlparse(href).scheme not in {"http", "https", "mailto"}:
            raise ValueError(f"unsupported link scheme: {href}")


def validate_generated_post(post: dict[str, Any]) -> None:
    if post.get("category") not in CATEGORIES:
        raise ValueError("invalid category")
    for key in ("title", "excerpt", "body"):
        if not str(post.get(key, "")).strip():
            raise ValueError(f"{key} is required")
    published = parse_iso(post["published_at"])
    if published > datetime.now(timezone.utc):
        raise ValueError("publication date cannot be in the future")
    title = post["title"].strip()
    excerpt = post["excerpt"].strip()
    if len(title) < 12 or len(title) > 120:
        raise ValueError("title length is outside editorial bounds")
    if len(excerpt) < 40 or len(excerpt) > 280:
        raise ValueError("excerpt length is outside editorial bounds")
    if len(clean_text(post["body"]).split()) < 500:
        raise ValueError("body is too short")
    low = (title + " " + post["body"]).lower()
    if any(x in low for x in BANNED_PHRASES):
        raise ValueError("generic AI phrasing detected")
    validate_html_body(post["body"])


def parse_llm_output(raw: str) -> dict[str, Any]:
    text = raw.strip()
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^(CATEGORY|TITLE|EXCERPT|SOURCE_INDEX|BODY):\s*(.*)$", line, re.I)
        if match:
            current = match.group(1).upper()
            sections[current] = match.group(2).strip()
        elif current:
            sections[current] += "\n" + line
    try:
        source_index = int(sections.get("SOURCE_INDEX", "0"))
    except ValueError:
        source_index = 0
    return {"category": sections.get("CATEGORY", "").strip().lower(), "title": clean_text(sections.get("TITLE", "")),
            "excerpt": clean_text(sections.get("EXCERPT", "")), "body": sections.get("BODY", "").strip(), "source_index": source_index}


def fetch_latest_news(api_key: str) -> list[dict[str, str]]:
    topic = os.environ.get("CARBONE_TOPIC", "").strip().lower() or CATEGORIES[datetime.now(timezone.utc).day % len(CATEGORIES)]
    if topic not in CATEGORIES:
        topic = "technology"
    response = requests.get("https://newsapi.org/v2/everything", params={"q": topic, "language": "en", "sortBy": "publishedAt", "pageSize": 8, "apiKey": api_key}, timeout=15)
    log("SOURCE STATUS", str(response.status_code))
    if response.status_code != 200:
        fail("SOURCE FETCH", "http", f"NewsAPI returned HTTP {response.status_code}")
    data = response.json()
    if data.get("status") != "ok":
        fail("SOURCE FETCH", "api", json.dumps(data)[:500])
    articles = []
    for article in data.get("articles", []):
        title = (article.get("title") or "").strip()
        url = (article.get("url") or "").strip()
        if not title or not url or urlparse(url).scheme not in {"http", "https"}:
            continue
        articles.append({"title": title, "description": (article.get("description") or "").strip(), "url": url,
                         "source": (article.get("source") or {}).get("name", "Unknown"), "publishedAt": article.get("publishedAt", "")})
    if not articles:
        fail("SOURCE FETCH", "empty", "NewsAPI returned no usable articles")
    return articles


def openrouter_call(api_key: str, model: str, prompt: str) -> str:
    response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "HTTP-Referer": SITE_URL, "X-Title": "Carbone Notes"},
                             json={"model": model, "messages": [{"role": "system", "content": "You are the editor of an independent publication. Write careful human prose, not marketing copy."}, {"role": "user", "content": prompt}], "temperature": 0.45}, timeout=60)
    log("GENERATION", f"model={model} status={response.status_code}")
    if response.status_code in {401, 403}:
        fail("GENERATION", "authentication", f"OpenRouter returned HTTP {response.status_code}")
    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:250]}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenRouter response missing usable message content") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("OpenRouter returned empty content")
    return content


def build_generation_prompt(articles: list[dict[str, str]], recent: list[dict[str, Any]]) -> str:
    previous = "\n".join(f"- {p['title']}" for p in recent[-20:]) or "- none"
    sources = "\n".join(f"[{i}] {a['title']} — {a['description']} (source: {a['source']}; published: {a['publishedAt']}; url: {a['url']})" for i, a in enumerate(articles, 1))
    return f"""Write one publishable Carbone Notes article from one source below. Never invent a quote, statistic, source, event or URL.

Recent titles; avoid the same subject or a near-duplicate:
{previous}

Sources:
{sources}

Requirements:
- precise, informed, natural prose;
- 4–7 substantial paragraphs, using h2 headings only when helpful;
- no hype, generic intros or corporate language;
- no unsupported causal claims for health or security topics;
- one specific, non-clickbait title;
- one clean-sentence excerpt;
- SOURCE_INDEX must match the chosen source.

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
    errors = []
    for model in [MODEL] + FALLBACK_MODELS:
        try:
            parsed = parse_llm_output(openrouter_call(api_key, model, prompt))
            idx = parsed.get("source_index", 0)
            if not 1 <= idx <= len(articles):
                raise ValueError("SOURCE_INDEX is outside source range")
            parsed["source"] = articles[idx - 1]
            if title_is_duplicate(parsed["title"], recent):
                raise ValueError(f"title is too similar to existing coverage: {parsed['title']}")
            return parsed, model
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}: {exc}")
            log("GENERATION", errors[-1])
    fail("GENERATION", "all_models_failed", " | ".join(errors))


def render_post(meta: dict[str, Any], body: str, source: dict[str, str]) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    image = html.escape(meta["image"], quote=True)
    alt = html.escape(meta["image_alt"], quote=True)
    values = {
        "POST_EXCERPT_PLAIN": html.escape(meta["excerpt"], quote=True), "POST_AUTHOR": html.escape(meta["author"], quote=True),
        "POST_CANONICAL": f"{SITE_URL}/posts/{meta['slug']}.html", "POST_IMAGE_URL": image,
        "POST_PUBLISHED_AT": meta["published_at"], "POST_TAG": meta["category"].title(), "POST_PLAIN_TITLE": html.escape(meta["title"]),
        "POST_TITLE": html.escape(meta["title"]), "POST_EXCERPT": html.escape(meta["excerpt"]), "POST_NUMBER": str(meta["id"]),
        "POST_READ_TIME": str(max(1, round(len(clean_text(body).split()) / 220))),
        "POST_IMAGE_ELEMENT": f'<img src="{image}" alt="{alt}" width="1200" height="675" loading="eager" fetchpriority="high" decoding="async">',
        "POST_IMAGE_ALT": alt, "POST_BODY": body,
        "POST_SOURCES": f'<h2 id="sources-title">Sources</h2><ul><li><a href="{html.escape(source["url"], quote=True)}" rel="noopener noreferrer">{html.escape(source["source"])} — {html.escape(source["title"])}</a></li></ul>',
        "POST_DATE": display_date(meta["published_at"]),
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template.replace("</body>", f'<!-- CARBONE_META {json.dumps(meta, ensure_ascii=False, separators=(",", ":"))} -->\n</body>")


def canonical_posts(posts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    posts = posts or recent_metadata()
    selected: dict[str, dict[str, Any]] = {}
    for post in sorted(posts, key=lambda x: parse_iso(x["published_at"])):
        key = re.sub(r"[^a-z0-9]+", " ", post["title"].lower()).strip()
        selected[key] = post
    return sorted(selected.values(), key=lambda x: parse_iso(x["published_at"]))


def update_index(posts: list[dict[str, Any]]) -> None:
    path = BASE_DIR / "index.html"
    text = path.read_text(encoding="utf-8")
    posts = sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True)
    rows = [f'<a class="post-row" href="posts/{p["slug"]}.html"><span class="post-number">{p["id"]:02d}</span><div><h3 class="post-title">{html.escape(p["title"])}</h3><p class="post-excerpt">{html.escape(p["excerpt"])}</p></div><span class="post-side"><span class="category">{p["category"].title()}</span><time datetime="{p["published_at"]}">{display_date(p["published_at"])}</time></span></a>' for p in posts]
    marker = re.compile(r'<div class="post-list">.*?</div></section>', re.S)
    replacement = '<div class="post-list">\n' + "\n".join(rows) + '\n</div></section>'
    text, count = marker.subn(replacement, text, count=1)
    if count != 1:
        fail("UPDATE INDEX", "template", "post list not found")
    featured = posts[0]
    text = re.sub(r'<div class="featured-visual">.*?</div>', f'<div class="featured-visual"><img src="{html.escape(featured["image"], quote=True)}" alt="{html.escape(featured["image_alt"], quote=True)}" width="1200" height="675" fetchpriority="high"></div>', text, count=1, flags=re.S)
    text = re.sub(r'<div class="meta-line">.*?</div>', f'<div class="meta-line"><span class="category">{featured["category"].title()}</span><span class="meta-sep" aria-hidden="true"></span><time datetime="{featured["published_at"]}">{display_date(featured["published_at"])}</time></div>', text, count=1, flags=re.S)
    text = re.sub(r'<h2 id="featured-title">.*?</h2>', f'<h2 id="featured-title">{html.escape(featured["title"])}</h2>', text, count=1, flags=re.S)
    text = re.sub(r'<p class="featured-excerpt">.*?</p>', f'<p class="featured-excerpt">{html.escape(featured["excerpt"])}</p>', text, count=1, flags=re.S)
    text = re.sub(r'<a class="read-link" href="posts/post-\d+\.html">.*?</a>', f'<a class="read-link" href="posts/{featured["slug"]}.html">Read the note →</a>', text, count=1, flags=re.S)
    path.write_text(text, encoding="utf-8")


def write_archive_topics(posts: list[dict[str, Any]]) -> None:
    path = BASE_DIR / "archive.html"
    text = path.read_text(encoding="utf-8")
    rows = []
    for p in sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True):
        dt = parse_iso(p["published_at"])
        rows.append(f'<li><a class="archive-item" href="posts/{p["slug"]}.html"><time class="archive-date" datetime="{p["published_at"]}">{dt.strftime("%B %-d")}</time><span class="archive-title">{html.escape(p["title"])}</span><span class="archive-cat">{p["category"].title()}</span></a></li>')
    pattern = r'<ul class="archive-month">.*?</ul></section>'
    text, count = re.subn(pattern, '<ul class="archive-month">\n' + "\n".join(rows) + '\n</ul></section>', text, count=1, flags=re.S)
    if count != 1:
        fail("UPDATE ARCHIVE", "template", "archive list not found")
    path.write_text(text, encoding="utf-8")


def generate_feed(posts: list[dict[str, Any]]) -> None:
    from xml.etree.ElementTree import Element, SubElement, ElementTree
    rss = Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "Carbone Notes"
    SubElement(channel, "link").text = SITE_URL + "/"
    SubElement(channel, "description").text = "Notes, analysis and guides on technology, security, science and digital culture."
    SubElement(channel, "language").text = "en-us"
    SubElement(channel, "atom:link", {"href": SITE_URL + "/feed.xml", "rel": "self", "type": "application/rss+xml"})
    latest = max(parse_iso(p["updated_at"]) for p in posts)
    SubElement(channel, "lastBuildDate").text = format_datetime(latest, usegmt=True)
    for p in sorted(posts, key=lambda x: parse_iso(x["published_at"]), reverse=True):
        item = SubElement(channel, "item")
        url = f"{SITE_URL}/posts/{p['slug']}.html"
        SubElement(item, "title").text = p["title"]
        SubElement(item, "link").text = url
        SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        SubElement(item, "pubDate").text = format_datetime(parse_iso(p["published_at"]), usegmt=True)
        SubElement(item, "category").text = p["category"]
        SubElement(item, "description").text = p["excerpt"]
    ElementTree(rss).write(BASE_DIR / "feed.xml", encoding="utf-8", xml_declaration=True)


def generate_sitemap(posts: list[dict[str, Any]]) -> None:
    latest = max(p["updated_at"] for p in posts)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    pages = [("index.html", latest, "daily", "1.0"), ("archive.html", latest, "weekly", "0.9"), ("topics.html", latest, "weekly", "0.8"), ("about.html", "2026-09-03T00:00:00+00:00", "monthly", "0.6")]
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
    if os.environ.get("LOCAL_TEST_MODE") == "1":
        fail("START", "configuration", "LOCAL_TEST_MODE must not be used in production")
    openrouter_key = require_secret("OPENROUTER_API_KEY")
    news_key = require_secret("NEWS_API_KEY")
    if args.check_api:
        r1 = requests.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {openrouter_key}"}, timeout=20)
        if r1.status_code != 200:
            fail("CHECK API", "openrouter", f"OpenRouter returned HTTP {r1.status_code}")
        r2 = requests.get("https://newsapi.org/v2/top-headlines", params={"language": "en", "pageSize": 1, "apiKey": news_key}, timeout=20)
        if r2.status_code != 200:
            fail("CHECK API", "newsapi", f"NewsAPI returned HTTP {r2.status_code}")
        try:
            payload = r2.json()
        except json.JSONDecodeError as exc:
            fail("CHECK API", "newsapi", "NewsAPI returned invalid JSON")
        if payload.get("status") != "ok":
            fail("CHECK API", "newsapi", json.dumps(payload)[:500])
        log("END", "API checks passed")
        return
    migrate_legacy_posts()
    recent = canonical_posts()
    articles = fetch_latest_news(news_key)
    draft, used_model = generated_post(articles, recent, openrouter_key)
    published_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    validate_generated_post({**draft, "published_at": published_at})
    next_id = max([p["id"] for p in recent] + [0]) + 1
    meta = {"id": next_id, "slug": slug_for(next_id), "title": draft["title"], "excerpt": draft["excerpt"], "category": draft["category"],
            "published_at": published_at, "updated_at": published_at, "author": AUTHOR, "image": UNSPLASH_IMAGES[draft["category"]], "image_alt": f"Illustration for {draft['title']}"}
    rendered = render_post(meta, draft["body"], draft["source"])
    if not rendered.lstrip().lower().startswith("<!doctype html>"):
        fail("RENDER", "html", "rendered article is missing doctype")
    output = POSTS_DIR / f"{meta['slug']}.html"
    output.write_text(rendered, encoding="utf-8")
    all_posts = canonical_posts(recent_metadata())
    update_index(all_posts)
    write_archive_topics(all_posts)
    generate_feed(all_posts)
    generate_sitemap(all_posts)
    log("VALIDATION", f"article={meta['slug']} date={meta['published_at']} model={used_model}")
    log("END", "publication pipeline completed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if not isinstance(exc, RuntimeError):
            print(f"ERROR STAGE=UNHANDLED ERROR TYPE={type(exc).__name__} ERROR MESSAGE={exc}", file=sys.stderr, flush=True)
        sys.exit(1)
