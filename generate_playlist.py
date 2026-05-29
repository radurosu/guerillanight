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
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

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
    import random

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

    # Group tracks by night
    nights_sets = {}
    nights_lists = {}
    for t in tracks:
        nights_sets.setdefault(t["date"], set()).add(t["artist"])
        nights_lists.setdefault(t["date"], []).append(t)
    for d in nights_lists:
        nights_lists[d].sort(key=lambda t: t["time"])

    avg_unique_artists = round(sum(len(v) for v in nights_sets.values()) / max(len(nights_sets), 1), 1)
    avg_tracks_per_night = round(total / max(len(nights_sets), 1), 1)

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

    # Romanian artist ratio
    romanian_artists = set()
    all_artists = set()
    romanian_keywords = ["romania", "romanian", "ro"]
    for t in tracks:
        all_artists.add(t["artist"])
        genres = t.get("genres", [])
        if any(kw in " ".join(genres).lower() for kw in romanian_keywords):
            romanian_artists.add(t["artist"])

    # ── Transition analysis ─────────────────────────────────────────────────
    # Build genre transition matrix (what follows what)
    genre_trans = Counter()
    genre_from_count = Counter()
    for date, nt in nights_lists.items():
        for i in range(len(nt) - 1):
            g_a = (nt[i].get("genres") or ["unknown"])[:1][0]
            g_b = (nt[i + 1].get("genres") or ["unknown"])[:1][0]
            genre_trans[(g_a, g_b)] += 1
            genre_from_count[g_a] += 1

    # Top transition flows for the 12 most common genres
    flow_genres = [g for g, _ in top_genres[:12] if g != "unknown"]
    transition_flows = {}
    for g in flow_genres:
        total_from = genre_from_count.get(g, 0)
        if total_from < 5:
            continue
        follows = [(b, c) for (a, b), c in genre_trans.items() if a == g and b != "unknown"]
        follows.sort(key=lambda x: -x[1])
        transition_flows[g] = [(b, round(c / total_from * 100)) for b, c in follows[:4]]

    # ── Signature juxtapositions (rare cross-genre transitions) ─────────────
    juxtapositions = []
    for date, nt in nights_lists.items():
        for i in range(len(nt) - 1):
            a, b = nt[i], nt[i + 1]
            ga = (a.get("genres") or ["?"])[:1][0]
            gb = (b.get("genres") or ["?"])[:1][0]
            if ga == gb or ga == "?" or gb == "?" or ga == "unknown" or gb == "unknown":
                continue
            total_from = genre_from_count.get(ga, 1)
            prob = genre_trans.get((ga, gb), 0) / total_from
            if prob < 0.06:
                juxtapositions.append({
                    "from_artist": a["artist"],
                    "from_genre": ga,
                    "to_artist": b["artist"],
                    "to_genre": gb,
                })
    # Pick a diverse random sample
    random.shuffle(juxtapositions)
    sampled_juxtapositions = juxtapositions[:20]

    # ── Rich sequence examples (longer runs from multiple nights) ───────────
    sorted_dates = sorted(nights_lists.keys())
    # Pick 3 nights spread across the data, show 10+ consecutive tracks each
    sample_dates = []
    if len(sorted_dates) >= 3:
        sample_dates = [sorted_dates[0], sorted_dates[len(sorted_dates) // 2], sorted_dates[-1]]
    else:
        sample_dates = sorted_dates

    sample_sequences = []
    for date in sample_dates:
        nt = nights_lists[date]
        sample = []
        for t in nt[:12]:
            genre_str = ", ".join(t.get("genres", [])[:2]) if t.get("genres") else "unknown"
            pop = t.get("metadata", {}).get("lastfm_listeners", 0)
            pop_label = ("mainstream" if pop > 2_000_000 else "known" if pop > 500_000
                         else "indie" if pop > 50_000 else "deep")
            sample.append(f"  {t['time']} {t['artist']} — {t['title']} [{genre_str}] ({pop_label})")
        sample_sequences.append({"date": date, "tracks": sample})

    # All unique artists (for freshness control)
    all_artist_list = sorted(all_artists)

    return {
        "total_tracks": total,
        "total_nights": len(nights_sets),
        "avg_tracks_per_night": avg_tracks_per_night,
        "avg_unique_artists": avg_unique_artists,
        "genre_distribution": genre_pcts,
        "popularity_distribution": pop_pcts,
        "top_recurring_artists": [(a, c) for a, c in top_artists[:20]],
        "romanian_artist_pct": round(len(romanian_artists) / max(len(all_artists), 1) * 100, 1),
        "transition_flows": transition_flows,
        "juxtapositions": sampled_juxtapositions,
        "sample_sequences": sample_sequences,
        "known_artists": all_artist_list,
    }


def build_prompt(profile: dict) -> str:
    import random

    # Anchor artists
    anchors = [a for a, _ in profile["top_recurring_artists"][:15]]
    anchor_str = ", ".join(anchors)

    # Genre distribution from actual data
    genre_lines = []
    for g, pct in profile["genre_distribution"][:15]:
        genre_lines.append(f"  {g}: {pct}%")
    genre_text = "\n".join(genre_lines)

    # Popularity distribution from actual data
    pop_lines = [f"  {k}: {v}%" for k, v in profile["popularity_distribution"].items()]
    pop_text = "\n".join(pop_lines)

    # Transition flows
    flow_lines = []
    for genre, follows in profile["transition_flows"].items():
        targets = ", ".join(f"{b} ({p}%)" for b, p in follows)
        flow_lines.append(f"  {genre} → {targets}")
    flow_text = "\n".join(flow_lines)

    # Juxtaposition examples
    jux_lines = []
    for j in profile["juxtapositions"]:
        jux_lines.append(f"  {j['from_artist']} [{j['from_genre']}] → {j['to_artist']} [{j['to_genre']}]")
    jux_text = "\n".join(jux_lines)

    # Rich sequence examples (10+ tracks per night)
    seq_lines = []
    for seq in profile["sample_sequences"]:
        seq_lines.append(f"\n  Night of {seq['date']}:")
        for t in seq["tracks"]:
            seq_lines.append(f"    {t}")
    sample_text = "\n".join(seq_lines)

    # Known artists for freshness control — representative sample, NOT alphabetical.
    # (The old [:100] slice was just the A-names, giving a skewed view of the universe.)
    recurring = [a for a, _ in profile["top_recurring_artists"]]
    rest = [a for a in profile["known_artists"] if a not in recurring]
    random.shuffle(rest)
    known_sample = ", ".join(recurring + rest[:max(0, 100 - len(recurring))])

    return f"""You are the AI music director for Radio Guerrilla's overnight block.
Curate a 6-hour playlist of EXACTLY 37 tracks (no more, no fewer) starting at 20:00.

Your PRIMARY job is SEQUENCING — each track must be a response to the one before it.
Not similarity. Response. Contrast, complement, surprise — but always emotionally coherent.

## The Guerrilla Signature

This station's magic is the juxtaposition. They play 95% cross-genre transitions —
almost never the same genre twice in a row. The flow is conversational: a jazz ballad
answers a post-punk dirge, trip-hop dissolves into classic rock, Romanian hip-hop
follows blues. Every transition should make the listener think "I would never have
put these together, but it works."

## Genre Distribution (from {profile['total_tracks']} real tracks across {profile['total_nights']} nights)
{genre_text}

## Popularity Mix
{pop_text}

## Transition Flows (what actually follows what — percentages from real data)
{flow_text}

## Signature Juxtapositions (real surprising transitions from the station)
{jux_text}

## How Actual Nights Sound (study the sequencing carefully)
{sample_text}

## Signature Artists (the station's BACKBONE — these recur constantly, they ARE Guerrilla)
{anchor_str}
A real Guerrilla night is built ON these artists — Nick Cave especially is the single
most-played act on the station. A night without its signature spine is not Guerrilla Night,
no matter how good the genre mix looks. But they are a SPINE, not the body: pick a small
handful that fit tonight and build fresh discovery around them.

## Sequencing Rules
1. NEVER play the same genre back-to-back. 95% of real transitions are cross-genre.
2. Each track must emotionally respond to the previous one — by contrast, complement,
   or surprise. After something heavy, go intimate. After something electronic, go acoustic.
   After something famous, go obscure.
3. Vary popularity constantly — mainstream hit → deep cut → known artist → underground.
   Never cluster similar popularity levels.
4. Include 4-6 Signature Artists — and NO MORE THAN 6. This is a hard window in BOTH
   directions: fewer than 4 loses the identity; more than 6 makes every night sound
   identical and starves the freshness. Count them. Pick the 4-6 that best fit tonight's
   flow, spread across the night, then fill everything else with discovery.
5. Romanian/local artists (~{profile['romanian_artist_pct']}%) should appear naturally woven in,
   not clustered. A Romanian track after an international one and before another international one.
6. No two tracks by the same artist.
7. Real tracks only — every entry must be a real, existing song.
8. Beyond the signature spine, at least 50% of the REMAINING artists should be NEW —
   not from this already-played list:
   {known_sample}
   Fresh discovery around a familiar backbone. Keep it new for repeat listeners
   without losing the station's identity.

## Output Format

Return ONLY a JSON array, no other text. Each entry:
```json
{{
  "time": "HH:MM",
  "artist": "Artist Name",
  "title": "Track Title",
  "genre_tags": ["tag1", "tag2"],
  "approx_duration_ms": 240000,
  "reason_for_placement": "why this track HERE, after the previous one"
}}
```

The "reason_for_placement" is critical — it must explain the TRANSITION, not just
describe the track. Example: "After Nick Cave's brooding intensity, this Carla Bruni
whisper feels like exhaling. French chanson after post-punk — the contrast is the point."

Generate the full 6-hour block now."""


# ── Last.fm enrichment of generated tracks ────────────────────────────────────

def _lastfm_get(method: str, **params) -> dict | None:
    import requests
    key = os.environ.get("LASTFM_API_KEY", "")
    if not key:
        return None
    params.update({"method": method, "api_key": key, "format": "json"})
    try:
        r = requests.get(LASTFM_API, params=params, timeout=8)
        r.raise_for_status()
        d = r.json()
        return None if "error" in d else d
    except Exception:
        return None


def enrich_generated(tracks: list[dict]) -> tuple[list[dict], list[str]]:
    """Attach Last.fm listeners/album/duration + artist tags to generated tracks.

    Returns (tracks, unmatched) where unmatched lists tracks Last.fm couldn't
    resolve — a strong signal the model hallucinated a non-existent song.
    """
    import time
    unmatched = []
    artist_cache: dict[str, list[str]] = {}

    for t in tracks:
        artist = t.get("artist", "")
        title = t.get("title", "")
        ak = artist.lower()

        if ak not in artist_cache:
            d = _lastfm_get("artist.getTopTags", artist=artist, autocorrect="1")
            tags = []
            if d:
                tags = [x["name"].lower() for x in d.get("toptags", {}).get("tag", [])[:10]
                        if int(x.get("count", 0)) > 0]
            artist_cache[ak] = tags
            time.sleep(0.2)

        if artist_cache[ak]:
            t["lastfm_genres"] = artist_cache[ak]

        matched = False
        if title:
            d = _lastfm_get("track.getInfo", artist=artist, track=title, autocorrect="1")
            info = {}
            if d and d.get("track"):
                tr = d["track"]
                if tr.get("listeners"):
                    info["lastfm_listeners"] = int(tr["listeners"])
                if tr.get("playcount"):
                    info["lastfm_playcount"] = int(tr["playcount"])
                if tr.get("album"):
                    info["album"] = tr["album"].get("title", "")
                if tr.get("duration") and tr["duration"] != "0":
                    info["duration_ms"] = int(tr["duration"])
                matched = bool(info.get("lastfm_listeners"))
            if info:
                md = t.get("metadata", {})
                md.update(info)
                t["metadata"] = md
            time.sleep(0.2)

        if not matched:
            unmatched.append(f"{artist} — {title}")

    return tracks, unmatched


# ── Anchor-window enforcement (deterministic, post-generation) ─────────────────

def anchor_rank_from_profile(profile: dict, top_n: int = 20) -> dict[str, int]:
    """Signature artists → recurrence count, keyed lowercase. Drives anchor enforcement."""
    return {a.lower(): c for a, c in profile.get("top_recurring_artists", [])[:top_n]}


def trim_anchors(tracks: list[dict], anchor_rank: dict[str, int], hi: int = 6) -> tuple[list[dict], int, int]:
    """Cap signature-artist count at `hi`. The model overshoots the prompt window,
    so enforce it in code: keep the `hi` most-signature anchor tracks (highest
    knowledge-base recurrence), drop the surplus. Returns (tracks, before, after).

    Dropping (not replacing) is deliberate: it's deterministic, preserves the flow
    of every surviving track, and removing over-used known artists *raises* freshness.
    """
    anchor_idxs = [i for i, t in enumerate(tracks) if t.get("artist", "").lower() in anchor_rank]
    before = len(anchor_idxs)
    if before <= hi:
        return tracks, before, before

    # Rank surplus by signature strength (KB recurrence); keep the strongest `hi`.
    ranked = sorted(anchor_idxs, key=lambda i: -anchor_rank.get(tracks[i]["artist"].lower(), 0))
    keep = set(ranked[:hi])
    drop = set(ranked[hi:])
    trimmed = [t for i, t in enumerate(tracks) if i not in drop]
    return trimmed, before, len(keep)


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

    # Parse
    playlist = parse_playlist(raw)
    if not playlist:
        sys.exit(1)

    # Enforce the signature-artist ceiling (the model overshoots the prompt window).
    playlist, before, after = trim_anchors(playlist, anchor_rank_from_profile(profile), hi=6)
    if before != after:
        print(f"  Anchor trim: {before} → {after} signature artists (capped at 6).")

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
