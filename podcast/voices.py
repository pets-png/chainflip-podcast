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


def _render_line(text: str, ref: str, cfg: dict, cache: Path) -> Path:
    # One voice per request: drama-3 lets voices bleed into each other in multi-speaker requests.
    key = hashlib.sha1(json.dumps([cfg["model"], text, ref]).encode()).hexdigest()[:16]
    out = cache / f"{key}.wav"
    if out.exists():
        return out
    try:
        audio = _tts(text, ref, cfg["model"])
    except RuntimeError as e:
        print(f"[tts] {cfg['model']} failed on a line ({e}); trying {cfg['fallback_model']}")
        audio = _tts(text, ref, cfg["fallback_model"])
    out.write_bytes(audio)
    return out


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def _pcm(path: Path, max_pause: float | None = None) -> Path:
    """The concat demuxer needs identical formats, so normalise every piece first.

    With max_pause, also trim the silence a TTS clip starts and ends with and cap pauses inside it.
    """
    out = path.with_name(f"{path.stem}.{'tight' if max_pause else 'pcm'}.wav")
    if not out.exists():
        af = []
        if max_pause:
            edge = "silenceremove=start_periods=1:start_threshold=-42dB:start_silence={}"
            af = ["-af", ",".join([edge.format(0.03), "areverse", edge.format(0.05), "areverse",
                                   f"silenceremove=stop_periods=-1:stop_duration={max_pause}"
                                   f":stop_threshold=-42dB:stop_silence={max_pause}"])]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), *af, "-ar", "44100", "-ac", "1",
                        "-c:a", "pcm_s16le", str(out)], check=True)
    return out


def _silence(ms: int) -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / f"silence_{ms}.wav"
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", f"{ms / 1000}", "-c:a", "pcm_s16le", str(out)], check=True)
    return out


def _concat(parts: list[Path], out: Path, gaps_ms: list[int]) -> None:
    """Join PCM parts; gaps_ms[i] is the silence before parts[i + 1]."""
    listing = out.parent / f"{out.stem}_list.txt"
    seq = []
    for i, p in enumerate(parts):
        if i and gaps_ms[i - 1]:
            seq.append(_silence(gaps_ms[i - 1]))
        seq.append(p)
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
    cache = BUILD / "tts_cache"
    cache.mkdir(parents=True, exist_ok=True)
    print(f"[tts] {len(lines)} lines via {cfg['model']}, one voice per request")
    with ThreadPoolExecutor(4) as pool:
        raw_lines = list(pool.map(lambda l: _render_line(l["text"], voices[l["speaker"]], cfg, cache), lines))
    parts = [_pcm(p, cfg["max_pause_s"]) for p in raw_lines]

    # Drama-3 reads slower than real banter: join lines with short gaps, then speed the speech up (pitch kept).
    gaps = [cfg["chapter_gap_ms"] if i in chapter_starts else cfg["line_gap_ms"] for i in range(1, len(parts))]
    starts, t = {0: 0.0}, 0.0
    for i, p in enumerate(parts[:-1]):
        t += duration(p) + gaps[i] / 1000
        starts[i + 1] = t
    speed = cfg["speed"]
    joined, speech = out_mp3.with_suffix(".joined.wav"), out_mp3.with_suffix(".speech.wav")
    _concat(parts, joined, gaps)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined), "-af", f"atempo={speed}",
                    "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(speech)], check=True)
    joined.unlink()

    intro, outro = ROOT / "assets" / "intro.mp3", ROOT / "assets" / "outro.mp3"
    pieces = ([_pcm(intro)] if intro.exists() else []) + [speech] + ([_pcm(outro)] if outro.exists() else [])
    offset = duration(pieces[0]) + cfg["chapter_gap_ms"] / 1000 if intro.exists() else 0.0
    chapters = [{"title": c["title"], "start": round(offset + starts.get(c["line"], 0.0) / speed, 1)}
                for c in script["chapters"]]
    chapters[0]["start"] = 0.0

    raw = out_mp3.with_suffix(".raw.wav")
    _concat(pieces, raw, [cfg["chapter_gap_ms"]] * (len(pieces) - 1))
    speech.unlink()
    meta = sum((["-metadata", f"{k}={v}"] for k, v in tags.items()), [])
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                    "-af", f"loudnorm=I={cfg['loudness_lufs']}:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "1",
                    "-codec:a", "libmp3lame", "-b:a", f"{cfg['bitrate_kbps']}k", *meta, str(out_mp3)], check=True)
    raw.unlink()
    return {"duration": round(duration(out_mp3)), "chapters": chapters, "bytes": out_mp3.stat().st_size}
