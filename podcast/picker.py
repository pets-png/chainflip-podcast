"""Choose the next blog post, the episode format, and the guest.

Rules, in order:
1. Only posts that are live on chainflip.io, have been live for a day, and
   make a decent conversation (podcast_fit).
2. Back catalog, not the blog's latest: a post must be min_post_age_days old,
   and topics the blog covered in the last blog_quiet_days are off limits, so
   the podcast and blog never push the same thing on the same day. Among
   eligible posts, the most recent goes first.
3. No topic cluster repeats within the first N episodes (then a cooldown),
   and no category within a short gap. When a post crosses over, it's
   skipped and an older one is used instead.
4. A Claude judge double-checks the shortlist against what's already aired,
   since two posts can overlap without sharing a cluster key.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .common import claude_json, load_cast, now_utc, parse_iso

ACTIVE = ("pending", "published")
ASSET_GUESTS = {"BTC": "hank", "SOL": "kai", "ETH": "elena", "USDT": "mei", "USDC": "mei", "TRX": "mei", "FLIP": "oscar"}


def aired(episodes: list[dict]) -> list[dict]:
    return sorted((e for e in episodes if e["status"] in ACTIVE), key=lambda e: e["number"])


def _live_since(post: dict) -> float:
    """When the post went live: the later of its page date and when we first saw it."""
    seen = parse_iso(post["first_seen"]).timestamp()
    if post.get("published"):
        dated = datetime.fromisoformat(post["published"]).replace(tzinfo=timezone.utc).timestamp()
        return max(seen, dated)
    return seen


def blog_recent_clusters(catalog: dict, days: int) -> set[str]:
    """Topics the blog itself put out in the last few days; the podcast stays off them."""
    cutoff = now_utc().timestamp() - days * 86400
    return {p["cls"]["cluster"] for p in catalog.values()
            if p["live"] and p.get("cls") and _live_since(p) >= cutoff}


def candidates(catalog: dict, episodes: list[dict], cfg: dict, strict_category: bool = True) -> list[dict]:
    sel, sched = cfg["selection"], cfg["schedule"]
    used = {e["slug"] for e in episodes}
    history = aired(episodes)
    n_next = len(history) + 1
    if n_next <= sel["unique_first_n"]:
        blocked_clusters = {e["cluster"] for e in history}
    else:
        blocked_clusters = {e["cluster"] for e in history[-sel["cluster_cooldown"]:]}
    recent_categories = {e["category"] for e in history[-(sel["category_gap"] - 1):]} if strict_category else set()
    blocked_clusters |= blog_recent_clusters(catalog, sched["blog_quiet_days"])
    now = now_utc().timestamp()
    cutoff = now - max(sched["min_live_hours"] * 3600, sched["min_post_age_days"] * 86400)

    out = []
    for p in catalog.values():
        c = p.get("cls")
        if not (p["live"] and c) or p["slug"] in used:
            continue
        if c["podcast_fit"] < sel["min_podcast_fit"] or c["category"] == "company":
            continue
        haystack = " ".join([p["title"], p["description"], *p["headings"]]).lower()
        if any(term in haystack for term in sel["exclude_terms"]):
            continue
        if _live_since(p) > cutoff:
            continue
        if not c["evergreen"] and now - _live_since(p) > sel["stale_after_days"] * 86400:
            continue
        if c["cluster"] in blocked_clusters or c["category"] in recent_categories:
            continue
        out.append(p)
    out.sort(key=lambda p: (p.get("published") or "", p["first_seen"]), reverse=True)
    return out


def _deep_dive_due(history: list[dict], cfg: dict) -> tuple[bool, bool]:
    dd = cfg["formats"]["deep_dive"]
    last = max((e["number"] for e in history if e["format"] == "deep_dive"), default=0)
    span = dd["max_gap_episodes"] - dd["min_gap_episodes"] + 1
    # Deterministic jitter so deep dives don't land on a fixed rhythm.
    jitter = int(hashlib.sha1(str(last).encode()).hexdigest(), 16) % span
    gap = len(history) + 1 - last
    return gap >= dd["min_gap_episodes"] + jitter, gap >= dd["max_gap_episodes"]


def pick_guest(post: dict, cast: dict) -> str | None:
    c = post["cls"]
    for g in cast["guests"]:
        if c["category"] in g["beats"]:
            return g["id"]
    for asset in c["assets"]:
        if asset.upper() in ASSET_GUESTS:
            return ASSET_GUESTS[asset.upper()]
    return None


JUDGE_SYSTEM = """You keep a daily crypto podcast from repeating itself.
You get the episodes that already aired and a shortlist of candidate blog posts
in order of preference. Pick the FIRST candidate whose core subject would not
substantially repeat an aired episode. Overlap means the same main question,
same advice, or the same story told again. Two episodes centred on the same
specific product or feature (e.g. Boost, Lending 2.0, a given wallet
integration) also overlap, even from different angles. A different question in a
broad area is fine (e.g. BTC loan liquidation risk vs. tax treatment of BTC loans).
If every candidate overlaps, return null."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": ["string", "null"]},
        "skipped": {"type": "array", "items": {
            "type": "object",
            "properties": {"slug": {"type": "string"}, "overlaps_with": {"type": "string"}},
            "required": ["slug", "overlaps_with"], "additionalProperties": False}},
    },
    "required": ["choice", "skipped"],
    "additionalProperties": False,
}


def judge(shortlist: list[dict], history: list[dict], cfg: dict) -> tuple[dict | None, list]:
    if not history:
        return shortlist[0], []
    window = history if len(history) < cfg["selection"]["unique_first_n"] else history[-cfg["selection"]["cluster_cooldown"]:]
    past = "\n".join(f"- Ep {e['number']}: {e['title']} [{e['cluster']}] {e.get('summary', '')}" for e in window)
    cands = "\n".join(
        f"{i + 1}. slug={p['slug']} | {p['title']} | {p['cls']['cluster_label']} | {p['description']}"
        for i, p in enumerate(shortlist)
    )
    out = claude_json(JUDGE_SYSTEM, f"Aired episodes:\n{past}\n\nCandidates:\n{cands}", JUDGE_SCHEMA, effort="medium", max_tokens=8000)
    by_slug = {p["slug"]: p for p in shortlist}
    return by_slug.get(out["choice"]), out["skipped"]


def choose(catalog: dict, episodes: list[dict], cfg: dict, force_slug: str | None = None) -> dict:
    cast = load_cast()
    history = aired(episodes)

    if force_slug:
        post = catalog[force_slug]
        skipped = []
    else:
        pool = candidates(catalog, episodes, cfg) or candidates(catalog, episodes, cfg, strict_category=False)
        if not pool:
            raise RuntimeError("No eligible blog posts left under the current rules")
        due, overdue = _deep_dive_due(history, cfg)
        if overdue:
            # Pull complex posts to the front so the deep dive actually happens.
            min_c = cfg["formats"]["deep_dive"]["min_complexity"]
            pool.sort(key=lambda p: p["cls"]["complexity"] < min_c)
        post, skipped = None, []
        for i in range(0, min(len(pool), 32), 8):
            post, s = judge(pool[i:i + 8], history, cfg)
            skipped += s
            if post:
                break
        post = post or pool[0]

    due, _ = _deep_dive_due(history, cfg)
    fmt_cfg = cfg["formats"]
    if due and post["cls"]["complexity"] >= fmt_cfg["deep_dive"]["min_complexity"]:
        fmt = "deep_dive"
    else:
        recent = [e for e in history if e["format"] != "deep_dive"][-10:]
        share = sum(e["format"] == "guest" for e in recent) / len(recent) if recent else 0
        fmt = "guest" if share < fmt_cfg["guest_share"] else "hosts_only"

    guest = pick_guest(post, cast) if fmt in ("guest", "deep_dive") else None
    if fmt == "guest" and not guest:
        fmt = "hosts_only"
    return {"post": post, "format": fmt, "guest": guest, "skipped": skipped}
