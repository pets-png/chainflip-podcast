"""Daily flow: publish what's cleared review, then prepare the next episode.

An episode is born "pending": audio sits in a draft GitHub Release, a review
issue is opened, and Discord gets a preview. After `review_hours` it goes live
unless someone vetoed it. Comment `/veto` (or add the `veto` label, or close
the issue) to kill it; comment `/approve` to publish it on the next run.
"""
from __future__ import annotations

import json
import re

import requests

from . import feed
from .catalog import refresh, scrape_post
from .common import (BUILD, DATA, EPISODES_PATH, LORE_PATH, characters, discord, gh, iso, load_cast,
                     load_config, now_utc, parse_iso, read_json, write_json)
from .picker import aired, blog_recent_clusters, choose
from .scriptwriter import spoken_words, write_script
from .voices import render

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}


def _episodes() -> list[dict]:
    return read_json(EPISODES_PATH, [])


def ensure_labels() -> None:
    for name, color, desc in (("episode-pending", "46DA93", "Podcast episode waiting for review"),
                              ("veto", "D73A4A", "Do not publish this episode")):
        gh("label", "create", name, "--color", color, "--description", desc, "--force", check=False)


def generate(force_slug: str | None = None, dry_run: bool = False) -> dict:
    cfg = load_config()
    cast = load_cast()
    catalog = refresh()
    episodes = _episodes()
    history = aired(episodes)
    number = len(history) + 1

    choice = choose(catalog, episodes, cfg, force_slug)
    post, fmt, guest = choice["post"], choice["format"], choice["guest"]
    for s in choice["skipped"]:
        print(f"[pick] skipped {s['slug']}: overlaps {s['overlaps_with']}")
    print(f"[pick] Ep {number}: {post['slug']} ({fmt}{', guest ' + guest if guest else ''})")

    fresh = scrape_post(post["url"])
    if not fresh:
        raise RuntimeError(f"{post['url']} is not reachable any more")
    lore = read_json(LORE_PATH, [])
    script = write_script(post, fresh["body"], fmt, guest, cast, number, lore, [e["title"] for e in history])
    words = spoken_words(script)
    print(f"[script] {script['episode_title']!r}: {len(script['lines'])} lines, {words} words")

    # Release tags use a running take number so a vetoed episode never collides
    # with the one that replaces it (public episode numbers can be reused).
    tag = f"take-{len(episodes) + 1:03d}"
    write_json(DATA / "scripts" / f"{tag}.json", {"post": post["url"], "format": fmt, "guest": guest, **script})

    ep = {
        "number": number, "tag": tag, "status": "pending", "slug": post["slug"],
        "post_url": post["url"], "post_title": post["title"], "category": post["cls"]["category"],
        "cluster": post["cls"]["cluster"], "format": fmt, "guest": guest,
        "title": script["episode_title"], "summary": script["summary"], "hook": script["hook"],
        "key_points": script["key_points"], "keywords": script["keywords"], "lore": script["lore"],
        "words": words, "created_at": iso(now_utc()),
    }

    mp3 = BUILD / f"{tag}-{post['slug'][:60]}.mp3"
    BUILD.mkdir(exist_ok=True)
    show = cfg["show"]
    audio = render(script, mp3, {"title": ep["title"], "artist": show["author"], "album": show["title"], "track": number})
    ep.update(duration=audio["duration"], chapters=audio["chapters"], audio_bytes=audio["bytes"], audio_file=mp3.name)
    ep["description_html"] = feed.show_notes(ep)
    print(f"[audio] {mp3} ({audio['duration'] // 60}m{audio['duration'] % 60:02d}s)")

    if dry_run:
        print("[dry-run] not creating release, issue or Discord ping")
        return ep

    ensure_labels()
    gh("release", "create", tag, str(mp3), "--draft", "--title", f"Episode {number}: {ep['title']}",
       "--notes", f"Based on {post['url']}")
    release_url = json.loads(gh("release", "view", tag, "--json", "url"))["url"]
    hours = cfg["schedule"]["review_hours"]
    guest_name = characters(cast)[guest]["name"] if guest else None
    script_md = "\n".join(f"**{characters(cast)[l['speaker']]['name']}:** {l['text']}" for l in script["lines"])
    body = f"""**{ep['title']}**
Format: {fmt}{f' with {guest_name}' if guest_name else ''} · {audio['duration'] // 60} min · {words} words
Based on: {post['url']}
Audio (draft release): {release_url}

Goes live automatically after {hours}h. Comment `/veto` to kill it, `/approve` to publish on the next run.

> {ep['hook']}

<details><summary>Full script</summary>

{script_md}
</details>
"""
    issue_url = gh("issue", "create", "--title", f"Episode {number} review: {ep['title']}",
                   "--label", "episode-pending", "--body", body)
    ep["issue"] = int(issue_url.rstrip("/").rsplit("/", 1)[1])
    ep["release_url"] = release_url

    episodes.append(ep)
    write_json(EPISODES_PATH, episodes)
    discord(f"🎙️ **Episode {number} ready for review:** {ep['title']}\n"
            f"{fmt.replace('_', ' ')}{f' with {guest_name}' if guest_name else ''}, {audio['duration'] // 60} min, "
            f"based on <{post['url']}>\nGoes live in {hours}h unless vetoed: {issue_url}", mp3)
    return ep


def _review_state(issue: int) -> str:
    data = json.loads(gh("issue", "view", str(issue), "--json", "state,labels,comments"))
    if any(l["name"] == "veto" for l in data["labels"]):
        return "veto"
    verdict = None
    for c in data["comments"]:
        if c.get("authorAssociation") in TRUSTED:
            text = c["body"].strip().lower()
            if text.startswith("/veto"):
                verdict = "veto"
            elif text.startswith("/approve"):
                verdict = "approve"
    if verdict:
        return verdict
    return "veto" if data["state"] == "CLOSED" else "waiting"


def _still_live(url: str, live: set[str]) -> bool:
    try:
        return url in live and requests.get(url, timeout=30).status_code == 200
    except requests.RequestException:
        return False


def publish_due() -> list[dict]:
    cfg = load_config()
    episodes = _episodes()
    pending = [e for e in episodes if e["status"] == "pending"]
    if not pending:
        return []
    catalog = refresh()
    live = {p["url"] for p in catalog.values() if p["live"]}
    blog_now = blog_recent_clusters(catalog, cfg["schedule"]["blog_quiet_days"])
    repo = cfg["github"]["repo"]
    lore = read_json(LORE_PATH, [])
    done = []
    for ep in sorted(pending, key=lambda e: e["number"]):
        state = _review_state(ep["issue"])
        age_h = (now_utc() - parse_iso(ep["created_at"])).total_seconds() / 3600
        if state == "veto" or not _still_live(ep["post_url"], live):
            reason = "vetoed" if state == "veto" else "blog post is no longer live"
            print(f"[publish] Ep {ep['number']} dropped: {reason}")
            ep["status"] = "vetoed"
            ep["dropped_reason"] = reason
            gh("release", "delete", ep["tag"], "--yes", check=False)
            gh("issue", "close", str(ep["issue"]), "--comment", f"Dropped: {reason}.", check=False)
            continue
        if state != "approve" and age_h < cfg["schedule"]["review_hours"]:
            print(f"[publish] Ep {ep['number']} waiting ({age_h:.1f}h old)")
            continue
        if ep["cluster"] in blog_now:
            # The blog just published on this topic; give each its own day.
            ep["held"] = ep.get("held", 0) + 1
            print(f"[publish] Ep {ep['number']} held: the blog covered '{ep['cluster']}' in the last few days")
            continue
        gh("release", "edit", ep["tag"], "--draft=false", "--latest=false")
        ep["audio_url"] = f"https://github.com/{repo}/releases/download/{ep['tag']}/{ep['audio_file']}"
        ep["status"] = "published"
        ep["published_at"] = iso(now_utc())
        lore += [{"episode": ep["number"], **l} for l in ep.get("lore", [])]
        gh("issue", "close", str(ep["issue"]), "--comment", "Published to the feed. 🎉", check=False)
        print(f"[publish] Ep {ep['number']} is live")
        done.append(ep)

    # Episodes are renumbered so vetoes never leave gaps in the public numbering.
    renumber(episodes)
    write_json(EPISODES_PATH, episodes)
    write_json(LORE_PATH, lore)
    feed.build(episodes)
    if done:
        discord("✅ Now live on the feed: " + ", ".join(f"Ep {e['number']} {e['title']}" for e in done))
    return done


def renumber(episodes: list[dict]) -> None:
    # Published numbers are frozen; only still-pending episodes shift down after a veto.
    n = max((e["number"] for e in episodes if e["status"] == "published"), default=0)
    for e in sorted((e for e in episodes if e["status"] == "pending"), key=lambda e: e["number"]):
        n += 1
        e["number"] = n


def daily() -> None:
    publish_due()
    if not any(e["status"] == "pending" for e in _episodes()):
        generate()
