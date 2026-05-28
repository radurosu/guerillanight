#!/usr/bin/env python3
"""
Multi-model playlist generator in Radio Guerrilla's overnight style.

Reads the knowledge base, computes a style profile, asks an LLM to generate
a 6-hour playlist that matches — but doesn't repeat — the station's DNA.

Usage:
    python3 generate_playlist.py              # interactive model selection
    python3 generate_playlist.py --model claude
    python3 generate_playlist.py --model gpt
    python3 generate_playlist.py --model gemini

Env (.env file supported):
    ANTHROPIC_API_KEY   — for Claude
    OPENAI_API_KEY      — for GPT
    GEMINI_API_KEY      — for Gemini
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROMANIA_TZ = ZoneInfo("Europe/Bucharest")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "guerrilla_knowledge.json")
OUTPUT_DIR = os.path.join(DATA_DIR, "playlists")

MODELS = {
    "claude": {
        "name": "Claude (Anthropic)",
        "env_key": "ANTHROPIC_API_KEY",
        "model_id": "claude-sonnet-4-20250514",
    },
    "gpt": {
        "name": "GPT (OpenAI)",
        "env_key": "OPENAI_API_KEY",
        "model_id": "gpt-4o",
    },
    "gemini": {
        "name": "Gemini (Google)",
        "env_key": "GEMINI_API_KEY",
        "model_id": "gemini-2.5-flash",
    },
    "grok": {
        "name": "Grok (xAI)",
        "env_key": "XAI_API_KEY",
        "model_id": "grok-3",
    },
}


# ── Style profile ────────────────────────────────────────────────────────────

def compute_style_profile(tracks: list[dict]) -> dict:
    """Distill the knowledge base into a style profile for the LLM."""
    total = len(tracks)
    enriched = [t for t in tracks if t.get("genres")]

    # Genre distribution
    genre_counts = Counter()
    for t in enriched:
        for g in t["genres"]:
            genre_counts[g] += 1
    top_genres = genre_counts.most_common(30)
    genre_pcts = [(g, round(c / len(enriched) * 100, 1)) for g, c in top_genres]

    # Artist frequency — who appears most
    artist_counts = Counter(t["artist"] for t in tracks)
    top_artists = artist_counts.most_common(30)

    # Unique artists per night
    nights = {}
    for t in tracks:
        nights.setdefault(t["date"], set()).add(t["artist"])
    avg_unique_artists = round(sum(len(v) for v in nights.values()) / max(len(nights), 1), 1)
    avg_tracks_per_night = round(total / max(len(nights), 1), 1)

    # Popularity distribution (from Last.fm listeners)
    listeners = [t["metadata"]["lastfm_listeners"] for t in tracks
                 if t.get("metadata", {}).get("lastfm_listeners")]
    pop_buckets = {"underground (<50k)": 0, "indie (50k-500k)": 0,
                   "known (500k-2M)": 0, "mainstream (>2M)": 0}
    for l in listeners:
        if l < 50_000:
            pop_buckets["underground (<50k)"] += 1
        elif l < 500_000:
            pop_buckets["indie (50k-500k)"] += 1
        elif l < 2_000_000:
            pop_buckets["known (500k-2M)"] += 1
        else:
            pop_buckets["mainstream (>2M)"] += 1
    if listeners:
        pop_pcts = {k: round(v / len(listeners) * 100, 1) for k, v in pop_buckets.items()}
    else:
        pop_pcts = pop_buckets

    # Geographic mix (artist country from metadata)
    countries = Counter()
    for t in enriched:
        c = t.get("metadata", {}).get("artist_country", "")
        if c:
            countries[c] += 1

    # Romanian artist ratio
    romanian_artists = set()
    all_artists = set()
    romanian_keywords = ["romania", "romanian", "ro"]
    for t in tracks:
        all_artists.add(t["artist"])
        genres = t.get("genres", [])
        if any(kw in " ".join(genres).lower() for kw in romanian_keywords):
            romanian_artists.add(t["artist"])

    # Sample tracks — 3 tracks from each night to show sequencing
    sample_sequences = []
    for date in sorted(nights.keys())[:3]:
        night_tracks = sorted([t for t in tracks if t["date"] == date], key=lambda t: t["time"])
        sample = []
        for t in night_tracks[:5]:
            genre_str = ", ".join(t.get("genres", [])[:3]) if t.get("genres") else "unknown"
            sample.append(f"  {t['time']} {t['artist']} — {t['title']} [{genre_str}]")
        sample_sequences.append({"date": date, "tracks": sample})

    # All unique artists (for "don't just repeat these")
    all_artist_list = sorted(all_artists)

    return {
        "total_tracks": total,
        "total_nights": len(nights),
        "avg_tracks_per_night": avg_tracks_per_night,
        "avg_unique_artists": avg_unique_artists,
        "genre_distribution": genre_pcts,
        "popularity_distribution": pop_pcts,
        "top_recurring_artists": [(a, c) for a, c in top_artists[:20]],
        "romanian_artist_pct": round(len(romanian_artists) / max(len(all_artists), 1) * 100, 1),
        "sample_sequences": sample_sequences,
        "known_artists": all_artist_list,
    }


def build_prompt(profile: dict) -> str:
    # Extract top recurring artists for the anchor list
    anchors = [a for a, _ in profile["top_recurring_artists"][:15]]
    anchor_str = ", ".join(anchors)

    # Build known artists list for freshness control
    known_sample = ", ".join(profile["known_artists"][:100])

    # Sample sequences for taste reference
    seq_lines = []
    for seq in profile["sample_sequences"]:
        seq_lines.append(f"\n  Night of {seq['date']}:")
        for t in seq["tracks"]:
            seq_lines.append(f"    {t}")
    sample_text = "\n".join(seq_lines)

    return f"""You are the AI music director for Radio Guerrilla Overnight (20:00–02:00).
Curate exactly 6 hours of seamless playlist (35-40 tracks) that perfectly matches
this signature style:

## Signature Artists (heavy rotation)
{anchor_str}

## Genre DNA
- 40% alternative/indie rock + pop
- 25% electronic/trip-hop/chillout
- 15% folk/singer-songwriter/acoustic
- 10% classic rock/blues/soul
- 10% romanian/local artists (rock, hip-hop, folk, electronic)

## Vibe
Eclectic, moody, reflective late-night journey. Mix big international names with
deep cuts and Romanian/Moldovan flavor. Include occasional high-energy bursts but
keep overall nocturnal flow. The station is known for surprising juxtapositions —
Metallica into Karen Souza, Aphex Twin into Leonard Cohen.

## Structure
- 20:00–22:00: Mid-tempo rock/pop/indie to ease into the night
- 22:00–01:00: Build subtle energy — electronic, trip-hop, deeper alternative
- 01:00–02:00: Descend into atmospheric/folk/soul, wind down toward dawn

## Constraints
- No two tracks by the same artist
- No two tracks of the same genre back-to-back
- Mix popularity: ~30% well-known tracks, ~40% mid-tier, ~30% deep cuts
- Real tracks only — every entry must be a real, existing song
- At least 50% of artists should NOT be from this already-played list:
  {known_sample}
  Introduce new artists that FIT the style. Keep it fresh for repeat listeners.

## Taste Reference (actual recent nights)
{sample_text}

## Output Format

Return ONLY a JSON array, no other text. Each entry:
```json
{{
  "time": "HH:MM",
  "artist": "Artist Name",
  "title": "Track Title",
  "genre_tags": ["tag1", "tag2"],
  "approx_duration_ms": 240000,
  "reason_for_placement": "brief reason this track fits here"
}}
```

Generate the full 6-hour block now."""


# ── Model calls ──────────────────────────────────────────────────────────────

def call_claude(prompt: str, api_key: str) -> str:
    import requests
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODELS["claude"]["model_id"],
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def call_gpt(prompt: str, api_key: str) -> str:
    import requests
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODELS["gpt"]["model_id"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16000,
            "temperature": 0.9,
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_gemini(prompt: str, api_key: str) -> str:
    import requests
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODELS['gemini']['model_id']}:generateContent?key={api_key}",
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 16000, "temperature": 0.9},
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def call_grok(prompt: str, api_key: str) -> str:
    import requests
    resp = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODELS["grok"]["model_id"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32000,
            "temperature": 0.9,
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


CALLERS = {
    "claude": call_claude,
    "gpt": call_gpt,
    "gemini": call_gemini,
    "grok": call_grok,
}


# ── Output ───────────────────────────────────────────────────────────────────

def parse_playlist(raw: str) -> list[dict]:
    """Extract JSON array from model response (handles markdown fences)."""
    text = raw.strip()
    if "```" in text:
        match = text.split("```")[1]
        if match.startswith("json"):
            match = match[4:]
        text = match.strip()

    try:
        playlist = json.loads(text)
        if isinstance(playlist, list):
            return playlist
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in the text
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    print("  ERROR: Could not parse playlist JSON from model response.", file=sys.stderr)
    print(f"  Raw response (first 500 chars): {raw[:500]}", file=sys.stderr)
    return []


def save_playlist(playlist: list[dict], model_name: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now(ROMANIA_TZ).strftime("%Y%m%d_%H%M")
    filename = f"playlist_{model_name}_{ts}.json"
    path = os.path.join(OUTPUT_DIR, filename)

    output = {
        "generator": model_name,
        "model_id": MODELS[model_name]["model_id"],
        "generated_at": datetime.now(ROMANIA_TZ).isoformat(),
        "track_count": len(playlist),
        "tracks": playlist,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return path


# ── Main ─────────────────────────────────────────────────────────────────────

def load_env():
    """Load .env file if present. File values don't override existing env vars."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if value and not os.environ.get(key):
                os.environ[key] = value


def select_model() -> str:
    """Interactive model selection."""
    available = []
    for key, info in MODELS.items():
        api_key = os.environ.get(info["env_key"], "")
        status = "ready" if api_key else "no key"
        available.append((key, info["name"], status))

    print("\nAvailable models:")
    for i, (key, name, status) in enumerate(available, 1):
        marker = "*" if status == "ready" else " "
        print(f"  {marker} {i}. {name} [{status}]")

    ready = [a for a in available if a[2] == "ready"]
    if not ready:
        print("\nNo API keys found. Set them in .env or environment.")
        sys.exit(1)

    while True:
        choice = input(f"\nSelect model (1-{len(available)}): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available):
                key, name, status = available[idx]
                if status != "ready":
                    print(f"  {name} has no API key configured.")
                    continue
                return key
        except ValueError:
            if choice.lower() in MODELS:
                return choice.lower()
        print("  Invalid choice.")


def main():
    load_env()

    print("=" * 55)
    print("  Guerrilla Night — Playlist Generator")
    print("=" * 55)

    # Parse --model flag
    model_key = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--model" and i < len(sys.argv) - 1:
            model_key = sys.argv[i + 1].lower()
        elif arg.startswith("--model="):
            model_key = arg.split("=", 1)[1].lower()

    if model_key and model_key not in MODELS:
        print(f"  Unknown model: {model_key}. Options: {', '.join(MODELS.keys())}")
        sys.exit(1)

    # Load knowledge base
    if not os.path.exists(DB_PATH):
        print(f"  Knowledge base not found: {DB_PATH}")
        print("  Run scrape_weekly.py first.")
        sys.exit(1)

    with open(DB_PATH, encoding="utf-8") as f:
        db = json.load(f)

    tracks = db["tracks"]
    print(f"\n  Knowledge base: {len(tracks)} tracks")

    # Compute style profile
    print("  Computing style profile...", flush=True)
    profile = compute_style_profile(tracks)

    # Select model
    if not model_key:
        model_key = select_model()

    model_info = MODELS[model_key]
    api_key = os.environ.get(model_info["env_key"], "")
    if not api_key:
        print(f"\n  {model_info['env_key']} not set.")
        sys.exit(1)

    print(f"\n  Model: {model_info['name']} ({model_info['model_id']})")

    # Build prompt and call
    prompt = build_prompt(profile)
    print(f"  Generating playlist... (this may take 30-60s)", flush=True)

    try:
        raw = CALLERS[model_key](prompt, api_key)
    except Exception as e:
        print(f"\n  API error: {e}")
        sys.exit(1)

    # Parse and save
    playlist = parse_playlist(raw)
    if not playlist:
        sys.exit(1)

    path = save_playlist(playlist, model_key)

    print(f"\n  Generated {len(playlist)} tracks.")
    print(f"  Saved to: {path}\n")

    # Preview
    for t in playlist[:10]:
        genres = ", ".join(t.get("genre_tags", t.get("genres", []))[:2])
        print(f"  {t.get('time', '??:??')}  {t['artist']} — {t['title']}  [{genres}]")
    if len(playlist) > 10:
        print(f"  ... and {len(playlist) - 10} more tracks.")

    print(f"\n  Full playlist: {path}")


if __name__ == "__main__":
    main()
