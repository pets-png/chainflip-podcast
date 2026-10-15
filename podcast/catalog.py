"""The catalog of blog posts that are actually live on chainflip.io.

The live sitemap is the source of truth for "published": Framer drafts that
never get published never appear there, so they never become episodes. Posts
that disappear from the sitemap are marked not live and are skipped too.
"""
from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from .common import CATALOG_PATH, CATEGORIES, claude_json, iso, now_utc, read_json, write_json

SITEMAP = "https://chainflip.io/sitemap.xml"
BLOG_PREFIX = "https://chainflip.io/blog/"
UA = {"User-Agent": "ChainflipPodcastBot/1.0 (+https://github.com/pets-png/chainflip-podcast)"}
DATE_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{1,2}), (20\d\d)\b")


def live_blog_urls() -> list[str]:
    xml = requests.get(SITEMAP, headers=UA, timeout=30).text
    urls = [html.unescape(u) for u in re.findall(r"<loc>([^<]+)</loc>", xml)]
    return sorted({u.rstrip("/") for u in urls if u.startswith(BLOG_PREFIX) and len(u) > len(BLOG_PREFIX)})


def _meta(soup, key):
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return html.unescape(tag["content"]).strip() if tag and tag.get("content") else ""


def scrape_post(url: str) -> dict | None:
    """Fetch a live post. Returns None if it isn't actually reachable."""
    r = requests.get(url, headers=UA, timeout=30)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    title = re.sub(r"\s*\|\s*Chainflip\s*$", "", _meta(soup, "og:title") or (soup.title.string or ""))
    content = soup.find(attrs={"data-framer-name": "Content"})
    if not content:
        return None

    blocks, skip = [], False
    for el in content.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        if el.name == "p" and el.find_parent("li"):
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            # The Resources block is links only; the show notes carry those instead.
            skip = text.lower().startswith("resources")
            if not skip:
                blocks.append(f"\n## {text}")
        elif not skip:
            blocks.append(("- " if el.name == "li" else "") + text)

    body = "\n".join(blocks).strip()
    m = DATE_RE.search(soup.get_text(" ", strip=True))
    published = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat() if m else None
    return {
        "url": url,
        "slug": url.rsplit("/", 1)[1],
        "title": title.strip(),
        "description": _meta(soup, "description") or _meta(soup, "og:description"),
        "image": _meta(soup, "og:image"),
        "published": published,
        "headings": [b[4:] for b in blocks if b.startswith("\n## ")],
        "words": len(body.split()),
        "body": body,
    }


CLASSIFY_SYSTEM = """You organise the Chainflip blog back catalog for a daily podcast.

For every post you get, assign:
- cluster: a short kebab-case key naming the specific subject. Two posts share a
  cluster when an episode about one would mostly repeat the other (e.g. two
  explainers of BTC loan liquidation risk, or two Tron launch recaps). Different
  angles on a broad area (lending risk vs lending tax treatment) get different
  clusters. Reuse an existing cluster key when a post fits it.
- cluster_label: a few words describing the cluster for humans.
- category: the closest beat from the enum.
- assets: tickers the post is centrally about (e.g. BTC, SOL, ETH, USDT, TRX, FLIP, BNB). Empty if none.
- complexity: 1 (beginner explainer) to 5 (dense mechanics, math or architecture).
- podcast_fit: 1 to 5. How good a casual conversation episode this makes. Job
  posts, thin announcements and pure link roundups score 1-2. Stale one-off
  news that only mattered that week scores 2-3. Evergreen explainers, stories,
  strategies and opinionated pieces score 4-5.
- evergreen: true if it still makes sense to listen to months later."""


def _classify_schema():
    item = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "cluster": {"type": "string"},
            "cluster_label": {"type": "string"},
            "category": {"type": "string", "enum": CATEGORIES},
            "assets": {"type": "array", "items": {"type": "string"}},
            "complexity": {"type": "integer"},
            "podcast_fit": {"type": "integer"},
            "evergreen": {"type": "boolean"},
        },
        "required": ["slug", "cluster", "cluster_label", "category", "assets", "complexity", "podcast_fit", "evergreen"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"posts": {"type": "array", "items": item}},
            "required": ["posts"], "additionalProperties": False}


def classify(posts: list[dict], existing: dict) -> dict:
    clusters = sorted({(p["cls"]["cluster"], p["cls"]["cluster_label"]) for p in existing.values() if p.get("cls")})
    known = "\n".join(f"- {c}: {label}" for c, label in clusters) or "(none yet)"
    listing = "\n\n".join(
        f"slug: {p['slug']}\ntitle: {p['title']}\ndescription: {p['description']}\nsections: {' | '.join(p['headings'][:12])}"
        for p in posts
    )
    out = claude_json(
        CLASSIFY_SYSTEM,
        f"Existing clusters:\n{known}\n\nClassify these {len(posts)} posts. Return one entry per slug.\n\n{listing}",
        _classify_schema(),
        effort="medium",
    )
    return {p["slug"]: {k: v for k, v in p.items() if k != "slug"} for p in out["posts"]}


def refresh(verbose: bool = True) -> dict:
    """Sync the catalog with the live sitemap and classify anything new."""
    catalog = read_json(CATALOG_PATH, {})
    bootstrap = not catalog
    live = set(live_blog_urls())
    now = now_utc()

    for slug, p in catalog.items():
        was = p.get("live", True)
        p["live"] = p["url"] in live
        if was and not p["live"]:
            p["unpublished_at"] = iso(now)
            if verbose:
                print(f"[catalog] no longer live: {slug}")

    new_urls = [u for u in live if u.rsplit("/", 1)[1] not in catalog]
    if verbose:
        print(f"[catalog] {len(live)} live posts, {len(new_urls)} new")
    with ThreadPoolExecutor(8) as pool:
        scraped = [p for p in pool.map(lambda u: _safe_scrape(u), new_urls) if p]

    for p in scraped:
        p.pop("body")  # re-fetched fresh at script time; keeps the repo small
        first_seen = now
        if bootstrap and p["published"]:
            # Posts that were already live before the podcast existed are old enough.
            first_seen = min(now, datetime.fromisoformat(p["published"]).replace(tzinfo=timezone.utc) + timedelta(hours=12))
        catalog[p["slug"]] = {**p, "first_seen": iso(first_seen), "live": True}

    todo = [p for p in catalog.values() if p["live"] and not p.get("cls")]
    for i in range(0, len(todo), 120):
        batch = todo[i:i + 120]
        if verbose:
            print(f"[catalog] classifying {len(batch)} posts")
        result = classify(batch, catalog)
        for p in batch:
            if p["slug"] in result:
                p["cls"] = result[p["slug"]]

    write_json(CATALOG_PATH, dict(sorted(catalog.items())))
    return catalog


def _safe_scrape(url):
    try:
        return scrape_post(url)
    except Exception as e:
        print(f"[catalog] scrape failed for {url}: {e}")
        return None
