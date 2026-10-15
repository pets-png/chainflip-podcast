"""Shared paths, config loading, JSON state, and the Claude/Discord/gh helpers."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
BUILD = ROOT / "build"          # scratch output (audio, caches); gitignored
CATALOG_PATH = DATA / "catalog.json"
EPISODES_PATH = DATA / "episodes.json"
LORE_PATH = DATA / "lore.json"

CATEGORIES = [
    "lending", "bitcoin", "solana", "ethereum", "stablecoins_tron",
    "flip_staking", "security", "liquidity_lp", "integrations", "multichain",
    "regulation_macro", "releases_protocol", "markets_data", "education_basics",
    "company",
]


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def load_cast() -> dict:
    return yaml.safe_load((ROOT / "cast.yaml").read_text())


def save_cast(cast: dict) -> None:
    # Rewrites only voice_id lines so comments and folded text survive.
    text = (ROOT / "cast.yaml").read_text().splitlines(keepends=True)
    ids = {c["id"]: c.get("voice_id", "") for c in cast["hosts"] + cast["guests"]}
    current = None
    for i, line in enumerate(text):
        stripped = line.strip()
        if stripped.startswith("- id:"):
            current = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("voice_id:") and current in ids:
            indent = line[: len(line) - len(line.lstrip())]
            text[i] = f'{indent}voice_id: "{ids[current]}"\n'
    (ROOT / "cast.yaml").write_text("".join(text))


def characters(cast: dict) -> dict:
    return {c["id"]: c for c in cast["hosts"] + cast["guests"]}


def read_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ── Claude ────────────────────────────────────────────────────────────────────

_client = None


def claude() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=5)
    return _client


def claude_json(system: str, user: str, schema: dict, effort: str = "high", max_tokens: int = 64000) -> dict:
    """One structured-output call. Streams so long scripts don't hit HTTP timeouts."""
    model = load_config()["claude"]["model"]
    with claude().beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Claude refused: {msg.stop_details}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens; output truncated")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


# ── Discord ───────────────────────────────────────────────────────────────────

def discord(content: str, file_path: Path | None = None) -> None:
    url = os.environ.get("DISCORD_WEBHOOK")
    if not url:
        print("[discord] DISCORD_WEBHOOK not set; message was:\n" + content)
        return
    user = os.environ.get("DISCORD_USER_ID")
    if user:
        content = f"<@{user}> {content}"
    payload = {"content": content[:1990], "allowed_mentions": {"users": [user] if user else []}}
    try:
        # Webhook uploads cap out around 10 MB on unboosted servers; fall back to text-only.
        if file_path and file_path.stat().st_size < 9.5 * 1024 * 1024:
            with open(file_path, "rb") as f:
                r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                  files={"file": (file_path.name, f, "audio/mpeg")}, timeout=120)
            if r.ok:
                return
            print(f"[discord] upload failed ({r.status_code}); sending text only")
        requests.post(url, json=payload, timeout=30).raise_for_status()
    except Exception as e:  # notifications must never fail the run
        print(f"[discord] failed (non-fatal): {e}")


# ── GitHub CLI ────────────────────────────────────────────────────────────────

def gh(*args: str, check: bool = True) -> str:
    repo = load_config()["github"]["repo"]
    proc = subprocess.run(["gh", *args, "--repo", repo], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()
