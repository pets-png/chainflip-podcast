"""Fish Audio: casting original voices, rendering scripts, and mixing the episode."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .common import BUILD, ROOT, characters, load_cast, load_config, save_cast

API = "https://api.fish.audio"


def _headers(model: str | None = None) -> dict:
    key = os.environ.get("FISH_API_KEY")
    if not key:
        raise RuntimeError("FISH_API_KEY is not set")
    h = {"Authorization": f"Bearer {key}"}
    if model:
        h["model"] = model
    return h


# ── Casting ───────────────────────────────────────────────────────────────────

def audition_line(name: str) -> str:
    return f"Hey, it's {name}. Okay, honestly? I did not think we'd get into this today. [laughs] But here we are, so let's explain it properly."[:150]


def audition(char_id: str, n: int = 4, seed: int | None = None) -> Path:
    """Generate n candidate voices for a character from its voice_brief."""
    char = characters(load_cast())[char_id]
    body = {"instruction": char["voice_brief"].strip(), "reference_text": audition_line(char["name"]), "language": "en", "n": n}
    if seed is not None:
        body["seed"] = seed
    r = requests.post(f"{API}/v1/voice-design", headers={**_headers("voice-design-1"), "Content-Type": "application/json"},
                      json=body, timeout=180)
    r.raise_for_status()
    out = ROOT / "casting" / char_id
    out.mkdir(parents=True, exist_ok=True)
    meta = []
    for c in r.json()["candidates"]:
        (out / f"candidate_{c['index']}.wav").write_bytes(base64.b64decode(c["audio_base64"]))
        meta.append({k: v for k, v in c.items() if k != "audio_base64"})
    (out / "candidates.json").write_text(json.dumps({"request": body, "candidates": meta}, indent=2))
    return out


def pick_voice(char_id: str, index: int) -> str:
    """Turn an audition candidate into a persistent private Fish voice model."""
    cast = load_cast()
    char = characters(cast)[char_id]
    folder = ROOT / "casting" / char_id
    info = json.loads((folder / "candidates.json").read_text())
    cand = next(c for c in info["candidates"] if c["index"] == index)
    data = {"type": "tts", "title": f"Cross-Chain Daily - {char['name']}", "train_mode": "fast",
            "visibility": "private", "texts": info["request"]["reference_text"], "enhance_audio_quality": "false"}
    sig = cand.get("signature") or cand.get("voice_design_signature")
    if sig:
        data["voice_design_signatures"] = sig
    with open(folder / f"candidate_{index}.wav", "rb") as f:
        r = requests.post(f"{API}/model", headers=_headers(), data=data,
                          files={"voices": (f"{char_id}.wav", f, "audio/wav")}, timeout=180)
    r.raise_for_status()
    voice_id = r.json()["_id"]
    char["voice_id"] = voice_id
    save_cast(cast)
    return voice_id


# ── TTS ───────────────────────────────────────────────────────────────────────

def _tts(text: str, reference_id, model: str) -> bytes:
    body = {"text": text, "reference_id": reference_id, "format": "wav", "sample_rate": 44100, "latency": "normal"}
    for attempt in range(6):
        r = requests.post(f"{API}/v1/tts", headers={**_headers(model), "Content-Type": "application/json"},
                          json=body, timeout=600)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(60, 4 * 2 ** attempt))
            continue
        raise RuntimeError(f"Fish TTS {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Fish TTS kept failing after retries")


def _scenes(lines: list[dict], chapter_starts: set[int], max_chars: int, max_turns: int) -> list[list[int]]:
    scenes, cur, chars = [], [], 0
    for i, line in enumerate(lines):
        if cur and (i in chapter_starts or chars + len(line["text"]) > max_chars or len(cur) >= max_turns):
            scenes.append(cur)
            cur, chars = [], 0
        cur.append(i)
        chars += len(line["text"])
    if cur:
        scenes.append(cur)
    return scenes


def _render_scene(idx: list[int], lines: list[dict], voices: dict, cfg: dict, cache: Path) -> Path:
    speakers = list(dict.fromkeys(lines[i]["speaker"] for i in idx))
    if len(speakers) == 1:
        text, ref = " ".join(lines[i]["text"] for i in idx), voices[speakers[0]]
    else:
        text = "".join(f"<|speaker:{speakers.index(lines[i]['speaker'])}|>{lines[i]['text']}" for i in idx)
        ref = [voices[s] for s in speakers]
    key = hashlib.sha1(json.dumps([cfg["model"], text, ref]).encode()).hexdigest()[:16]
    out = cache / f"{key}.wav"
    if out.exists():
        return out
    try:
        audio = _tts(text, ref, cfg["model"])
    except RuntimeError as e:
        print(f"[tts] {cfg['model']} failed on a scene ({e}); trying {cfg['fallback_model']}")
        try:
            audio = _tts(text, ref, cfg["fallback_model"])
        except RuntimeError:
            if len(idx) == 1:
                raise
            # Last resort: one request per line, stitched.
            parts = [_render_scene([i], lines, voices, cfg, cache) for i in idx]
            _concat(parts, out, gap_ms=180)
            return out
    out.write_bytes(audio)
    return out


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def _pcm(path: Path) -> Path:
    """The concat demuxer needs identical formats, so normalise every piece first."""
    out = path.with_name(f"{path.stem}.pcm.wav")
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-ar", "44100", "-ac", "1",
                        "-c:a", "pcm_s16le", str(out)], check=True)
    return out


def _concat(parts: list[Path], out: Path, gap_ms: int) -> None:
    silence = out.parent / f"silence_{gap_ms}.wav"
    if not silence.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", f"{gap_ms / 1000}", str(silence)], check=True)
    listing = out.parent / f"{out.stem}_list.txt"
    seq = []
    for i, p in enumerate(parts):
        if i:
            seq.append(silence)
        seq.append(_pcm(p))
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in seq))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-ar", "44100", "-ac", "1", str(out)], check=True)
    listing.unlink()


def render(script: dict, out_mp3: Path, tags: dict) -> dict:
    """Render a script to a loudness-normalised MP3. Returns duration and chapter times."""
    cfg = load_config()["tts"]
    voices = {cid: c.get("voice_id") for cid, c in characters(load_cast()).items()}
    missing = sorted({l["speaker"] for l in script["lines"] if not voices.get(l["speaker"])})
    if missing:
        raise RuntimeError(f"No Fish voice_id for: {', '.join(missing)}. Run `python run.py cast <id>` first.")

    lines = script["lines"]
    chapter_starts = {c["line"] for c in script["chapters"]}
    scenes = _scenes(lines, chapter_starts, cfg["scene_max_chars"], cfg["scene_max_turns"])
    cache = BUILD / "tts_cache"
    cache.mkdir(parents=True, exist_ok=True)
    print(f"[tts] {len(lines)} lines in {len(scenes)} scenes via {cfg['model']}")
    with ThreadPoolExecutor(4) as pool:
        parts = list(pool.map(lambda s: _render_scene(s, lines, voices, cfg, cache), scenes))

    intro, outro = ROOT / "assets" / "intro.mp3", ROOT / "assets" / "outro.mp3"
    pieces = ([intro] if intro.exists() else []) + parts + ([outro] if outro.exists() else [])
    offset = duration(intro) + 0.35 if intro.exists() else 0.0

    # Chapter times from scene boundaries (scenes always break at chapter starts).
    starts, t = {}, offset
    for scene, part in zip(scenes, parts):
        starts.setdefault(scene[0], t)
        t += duration(part) + 0.35
    chapters = [{"title": c["title"], "start": round(starts.get(c["line"], offset), 1)} for c in script["chapters"]]
    chapters[0]["start"] = 0.0

    raw = out_mp3.with_suffix(".raw.wav")
    _concat(pieces, raw, gap_ms=350)
    meta = sum((["-metadata", f"{k}={v}"] for k, v in tags.items()), [])
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                    "-af", f"loudnorm=I={cfg['loudness_lufs']}:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "1",
                    "-codec:a", "libmp3lame", "-b:a", f"{cfg['bitrate_kbps']}k", *meta, str(out_mp3)], check=True)
    raw.unlink()
    return {"duration": round(duration(out_mp3)), "chapters": chapters, "bytes": out_mp3.stat().st_size}
