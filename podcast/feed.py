"""Show notes, the RSS feed Spotify reads, and a small landing page."""
from __future__ import annotations

import html
import urllib.parse
from email.utils import format_datetime

from .common import DOCS, characters, load_cast, load_config, parse_iso


def utm(url: str, campaign: str, content: str) -> str:
    cfg = load_config()["utm"]
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not (host == "chainflip.io" or host.endswith(".chainflip.io")):
        return url
    q = urllib.parse.parse_qsl(parts.query) + [
        ("utm_source", cfg["source"]), ("utm_medium", cfg["medium"]),
        ("utm_campaign", campaign), ("utm_content", content)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def timestamp(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def show_notes(ep: dict) -> str:
    """HTML episode description, mirroring the resource block on blog posts."""
    cfg = load_config()
    e = html.escape
    campaign, content = ep["slug"], f"ep-{ep['number']}"
    link = lambda u: e(utm(u, campaign, content))
    out = [f"<p>{e(ep['hook'])}</p>",
           f'<p>Read the full article: <a href="{link(ep["post_url"])}">{e(ep["post_title"])}</a></p>']
    if ep.get("guest"):
        g = characters(load_cast())[ep["guest"]]
        out.append(f"<p>With {e(g['name'])}, our regular on {e(', '.join(b.replace('_', ' ') for b in g['beats']))}.</p>")
    out.append("<p><strong>In this episode</strong></p><ul>" + "".join(f"<li>{e(k)}</li>" for k in ep["key_points"]) + "</ul>")
    if ep.get("chapters"):
        out.append("<p><strong>Chapters</strong><br>" + "<br>".join(f"{timestamp(c['start'])} {e(c['title'])}" for c in ep["chapters"]) + "</p>")
    for heading, key in (("Resources", "resources"), ("Earn with Chainflip", "earn")):
        out.append(f"<p><strong>{heading}</strong></p><ul>" + "".join(
            f'<li><a href="{link(u)}">{e(n)}</a> - {e(d)}</li>' for n, u, d in cfg["links"][key]) + "</ul>")
    out.append("<p><strong>Find us</strong></p><ul>" + "".join(
        f'<li><a href="{e(u)}">{e(n)}</a></li>' for n, u in cfg["links"]["social"]) + "</ul>")
    out.append("<p>Hosts and guests are fictional characters voiced with AI. This show is for education "
               "and entertainment only and is not financial advice.</p>")
    return "".join(out)


def _cdata(s: str) -> str:
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build(episodes: list[dict]) -> None:
    cfg = load_config()
    show = cfg["show"]
    e = html.escape
    published = sorted((x for x in episodes if x["status"] == "published"), key=lambda x: x["number"], reverse=True)
    cover = f"{show['site_url']}/cover.jpg"

    cats = []
    for c in show["categories"]:
        if len(c) == 1:
            cats.append(f'<itunes:category text="{e(c[0])}"/>')
        else:
            cats.append(f'<itunes:category text="{e(c[0])}"><itunes:category text="{e(c[1])}"/></itunes:category>')

    items = []
    for ep in published:
        notes = ep["description_html"]
        items.append(f"""    <item>
      <title>{e(ep['title'])}</title>
      <link>{e(utm(ep['post_url'], ep['slug'], f"ep-{ep['number']}"))}</link>
      <guid isPermaLink="false">chainflip-podcast-{ep['tag']}</guid>
      <pubDate>{format_datetime(parse_iso(ep['published_at']))}</pubDate>
      <description>{_cdata(notes)}</description>
      <content:encoded>{_cdata(notes)}</content:encoded>
      <enclosure url="{e(ep['audio_url'])}" length="{ep['audio_bytes']}" type="audio/mpeg"/>
      <itunes:title>{e(ep['title'])}</itunes:title>
      <itunes:episode>{ep['number']}</itunes:episode>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:duration>{ep['duration']}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
      <itunes:keywords>{e(', '.join(ep.get('keywords', [])))}</itunes:keywords>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{e(show['title'])}</title>
    <link>{e(show['link'])}</link>
    <atom:link href="{e(show['site_url'])}/feed.xml" rel="self" type="application/rss+xml"/>
    <language>{show['language']}</language>
    <copyright>© Chainflip Labs</copyright>
    <description>{_cdata(show['description'])}</description>
    <itunes:summary>{_cdata(show['description'])}</itunes:summary>
    <itunes:author>{e(show['author'])}</itunes:author>
    <itunes:owner><itunes:name>{e(show['owner_name'])}</itunes:name><itunes:email>{e(show['owner_email'])}</itunes:email></itunes:owner>
    <itunes:image href="{e(cover)}"/>
    <image><url>{e(cover)}</url><title>{e(show['title'])}</title><link>{e(show['link'])}</link></image>
    {''.join(cats)}
    <itunes:explicit>{'true' if show['explicit'] else 'false'}</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <itunes:keywords>{e(show['keywords'])}</itunes:keywords>
    <podcast:locked owner="{e(show['owner_email'])}">yes</podcast:locked>
{chr(10).join(items)}
  </channel>
</rss>
"""
    DOCS.mkdir(exist_ok=True)
    (DOCS / "feed.xml").write_text(feed)
    (DOCS / ".nojekyll").write_text("")
    _landing(published, show, cover)


def _landing(published: list[dict], show: dict, cover: str) -> None:
    e = html.escape
    rows = "".join(
        f"""<article><h2>{ep['number']}. {e(ep['title'])}</h2><p class="meta">{parse_iso(ep['published_at']):%b %d, %Y} · {ep['duration'] // 60} min</p>
<p>{e(ep['hook'])}</p><audio controls preload="none" src="{e(ep['audio_url'])}"></audio>
<p><a href="{e(utm(ep['post_url'], ep['slug'], 'site'))}">Read the article: {e(ep['post_title'])}</a></p></article>"""
        for ep in published)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(show['title'])}</title><meta name="description" content="{e(show['description'][:300])}">
<link rel="alternate" type="application/rss+xml" title="{e(show['title'])}" href="feed.xml">
<style>body{{margin:0;background:#0b0b0c;color:#ececec;font:16px/1.55 system-ui,sans-serif}}main{{max-width:760px;margin:auto;padding:32px 16px}}
header{{display:flex;gap:20px;align-items:center;flex-wrap:wrap}}header img{{width:160px;border-radius:12px}}a{{color:#46da93}}
article{{border-top:1px solid #222;padding:18px 0}}h2{{font-size:19px;margin:0 0 4px}}.meta{{color:#8a8a8a;margin:0 0 8px;font-size:14px}}audio{{width:100%}}</style></head>
<body><main><header><img src="cover.jpg" alt=""><div><h1>{e(show['title'])}</h1><p>{e(show['description'])}</p>
<p><a href="feed.xml">RSS feed</a> · <a href="{e(show['link'])}">Chainflip blog</a></p></div></header>{rows or '<p>First episode coming soon.</p>'}</main></body></html>
"""
    (DOCS / "index.html").write_text(page)
