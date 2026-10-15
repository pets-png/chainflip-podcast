"""Turn a blog post into a conversation script for the recurring cast."""
from __future__ import annotations

import re

from .common import claude_json, load_config

STYLE = """## How the show sounds
A loose, funny, conversational show, not a news read. Think two friends with
mics who happen to know a lot about crypto:
- Jules goes on tangents: oddly specific personal stories, weird analogies,
  half-remembered jobs, small-town characters. Every tangent loops back to the
  point and usually makes it stick better than the explanation did.
- Nadia keeps it accurate and moving, teases Jules, and lands the clean
  explanation. When a guest is on, they bring the expertise and their own quirks.
- Real crosstalk: quick reactions ("Wait.", "No.", "Mm-hm.", "Okay, okay."),
  people finishing each other's thoughts, a joke that gets called back later.
  At least one line in six is under six words.
- Comedy comes from the characters and their stories, never at the listener's
  or any real person's expense. Clean language.
- Teach one thing properly. The listener should leave able to explain the
  article's main idea to a friend.

## Structure
1. Cold open (2-5 lines): a bit of banter or a story that leads into today's topic.
2. Intro: one host welcomes people to the show by name and says what today is
   about in one sentence. Introduce the guest if there is one (they're a
   regular; treat them like an old friend coming back).
3. The conversation: walk through the article's ideas in a natural order. Use
   the article's facts, numbers and examples. Disagree a little where it's real.
4. Wrap: the one-line takeaway, then "the full article is linked in the show
   notes", a quick "not financial advice", and the sign-off "Stay native."

## Accuracy
- Every fact, number, date and product claim must come from the article. Do
  not invent statistics, prices, partnerships or features. If the article
  gives a dated number, say it's from the article or "at the time of writing".
- Characters are fictional. Never say they work at Chainflip or any real
  company, and never quote real people as if they're on the show.
- Never mention THORChain.
- Never use em dashes or en dashes. Use commas, periods, or "and".

## Writing for voice (this is read by a TTS model)
- Write numbers, symbols and abbreviations the way people say them:
  "$2.73M" -> "two point seven three million dollars", "40%" -> "forty
  percent", "LTV" -> "loan-to-value", "BTC" -> "bitcoin" (or "B T C" now and
  then), "chainflip.io" -> "chainflip dot io". No URLs, markdown or emoji.
- Delivery directions go in square brackets before the words they affect,
  written like a note to a voice actor: "[laughing] No way." "[deadpan] Sure,
  Jules." "[leaning in, conspiratorial]". Sounds and pauses too: [laughs],
  [chuckles], [sighs], [short pause], [inhales]. Use them on roughly one line
  in three, never more than two per line.
- For a single word's delivery you may wrap it: <emphasis>…</emphasis>,
  <whisper>…</whisper>, <slow>…</slow>, <fast>…</fast>. Sparingly.
- Keep each line under 60 words; split long thoughts into back-and-forth."""

FORMAT_NOTES = {
    "hosts_only": "Hosts only today: Jules and Nadia. A tight, punchy episode.",
    "guest": "Jules and Nadia with today's guest. The guest is the expert on this beat; give them the meatiest explanations and at least a third of the lines.",
    "deep_dive": "Deep dive episode. Take the time to walk through the mechanics step by step, with worked examples from the article, a section on risks or trade-offs, and a recap. If there's a guest, they drive the technical parts.",
}


def _cast_block(cast: dict) -> str:
    rows = []
    for c in cast["hosts"]:
        rows.append(f"- {c['name']} (id `{c['id']}`, host): {c['personality'].strip()}")
    for c in cast["guests"]:
        rows.append(f"- {c['name']} (id `{c['id']}`, recurring guest on {', '.join(c['beats'])}): {c['personality'].strip()}")
    return "\n".join(rows)


def _schema(speakers: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "episode_title": {"type": "string"},
            "summary": {"type": "string"},
            "hook": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "chapters": {"type": "array", "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "line": {"type": "integer"}},
                "required": ["title", "line"], "additionalProperties": False}},
            "lines": {"type": "array", "items": {
                "type": "object",
                "properties": {"speaker": {"type": "string", "enum": speakers}, "text": {"type": "string"}},
                "required": ["speaker", "text"], "additionalProperties": False}},
            "lore": {"type": "array", "items": {
                "type": "object",
                "properties": {"character": {"type": "string", "enum": speakers}, "note": {"type": "string"}},
                "required": ["character", "note"], "additionalProperties": False}},
        },
        "required": ["episode_title", "summary", "hook", "key_points", "keywords", "chapters", "lines", "lore"],
        "additionalProperties": False,
    }


def write_script(post: dict, body: str, fmt: str, guest: str | None, cast: dict, number: int,
                 lore: list[dict], recent_titles: list[str]) -> dict:
    cfg = load_config()
    show = cfg["show"]["short_name"]
    lo, hi = cfg["formats"]["word_targets"][fmt]
    speakers = [c["id"] for c in cast["hosts"]] + ([guest] if guest else [])
    guest_name = next((g["name"] for g in cast["guests"] if g["id"] == guest), None)

    system = (
        f"You write episodes of \"{show}\", a daily podcast where each episode is based on one "
        f"article from the Chainflip blog.\n\n## The cast\n{_cast_block(cast)}\n\n{STYLE}"
    )
    lore_text = "\n".join(f"- Ep {l['episode']}, {l['character']}: {l['note']}" for l in lore[-30:]) or "(first episodes, no history yet)"
    user = f"""Episode {number}. Format: {fmt}. {FORMAT_NOTES[fmt]}
Speakers allowed: {', '.join(speakers)}{f' (guest: {guest_name})' if guest_name else ''}.
Length: {lo}-{hi} spoken words in `lines` (not counting [directions]).

Running history you may call back to (at most one callback, only if it fits naturally):
{lore_text}

Recent episode titles (don't reuse their framing or jokes):
{chr(10).join('- ' + t for t in recent_titles[-15:]) or '(none)'}

Return:
- episode_title: {TITLE_RULES}
- summary: one sentence on what the episode covers (used to avoid repeats).
- hook: 2-3 sentence show-notes intro that makes someone press play.
- key_points: 3-5 short bullets of what listeners learn.
  (hook and key_points are read, not spoken: normal written style with standard
  abbreviations and digits, e.g. "KYC", "BTC", "80% LTV", not the TTS spellings.)
- keywords: 5-8 search phrases this episode should rank for.
- chapters: 3-6 chapters, `line` is the 0-based index in `lines` where each starts; the first is 0.
- lines: the script.
- lore: 1-3 new small facts or running bits revealed about the characters in this episode, for future callbacks.

# Article: {post['title']}
{post['description']}

{body}"""

    script = claude_json(system, user, _schema(speakers), effort="high")
    script = _clean(script)
    script["episode_title"] = distinct_title(script["episode_title"], post, script["keywords"])
    return script


TITLE_RULES = (
    "under 70 characters, no episode number, no em dashes. It must NOT copy the article's title: "
    "the podcast and the blog should read as two different pieces so they don't compete in search. "
    "Keep the article's primary search phrase, or a close keyword variant people also search for "
    "(e.g. 'BTC loan' vs 'bitcoin-backed loan', 'cross-chain swap' vs 'swap between chains'), and lead "
    "with it, then give it a fresh, conversational angle. Example: article 'Health Factor, LTV, and "
    "Liquidation Price: A BTC Borrower's Risk Playbook' -> episode 'BTC Loan Liquidation, Explained Like "
    "You're Five (Health Factor and LTV)'."
)

_STOP = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "how", "what", "is", "your", "you", "vs"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9.]+", s.lower()) if w not in _STOP}


def title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def distinct_title(title: str, post: dict, keywords: list[str], max_similarity: float = 0.5) -> str:
    """Re-title when the episode title is too close to the blog post's title."""
    for _ in range(2):
        if title_similarity(title, post["title"]) <= max_similarity and title.lower() not in post["title"].lower():
            return title
        print(f"[title] {title!r} is too close to the article title; rewriting")
        out = claude_json(
            "You write podcast episode titles that rank on Spotify and Apple search.",
            f"Article title: {post['title']}\nRejected episode title (too similar): {title}\n"
            f"Search phrases for this episode: {', '.join(keywords)}\n\nWrite one new episode title: {TITLE_RULES}",
            {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False},
            effort="low", max_tokens=2000,
        )
        title = re.sub(r"\s*[—–]\s*", ", ", out["title"]).strip()
    return title


def _clean(script: dict) -> dict:
    dash = re.compile(r"\s*[—–]\s*")
    def fix(s: str) -> str:
        return dash.sub(", ", s)
    for line in script["lines"]:
        line["text"] = fix(line["text"]).strip()
    for k in ("episode_title", "summary", "hook"):
        script[k] = fix(script[k])
    script["key_points"] = [fix(k) for k in script["key_points"]]
    script["lines"] = [l for l in script["lines"] if l["text"]]
    n = len(script["lines"])
    chapters = sorted({min(max(c["line"], 0), n - 1): c["title"] for c in script["chapters"]}.items())
    if not chapters or chapters[0][0] != 0:
        chapters.insert(0, (0, "Intro"))
    script["chapters"] = [{"line": l, "title": fix(t)} for l, t in chapters]
    return script


def spoken_words(script: dict) -> int:
    text = " ".join(l["text"] for l in script["lines"])
    text = re.sub(r"\[[^\]]*\]|<[^>]+>", " ", text)
    return len(text.split())
