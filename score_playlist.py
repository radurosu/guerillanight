#!/usr/bin/env python3
"""
Style scorer — measures how close a generated playlist matches
Radio Guerrilla's overnight DNA.

Compares a generated playlist against the knowledge base (or a held-out
test slice) across multiple dimensions:
  - Genre distribution similarity
  - Popularity curve match
  - Artist diversity
  - Freshness (% new artists not in the reference)
  - Romanian artist presence

Usage:
    python3 score_playlist.py data/playlists/playlist_claude_20260522.json
    python3 score_playlist.py data/playlists/playlist_gpt_20260522.json --verbose
"""

import json
import math
import os
import sys
from collections import Counter

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "guerrilla_knowledge.json")

# Which nights to hold out as test data (the scorer compares against these)
# Use the remaining nights as the "training" reference the generator sees
TEST_NIGHTS = 2  # hold out the 2 most recent nights

TARGET_TRACKS = 37  # 6-hour block ≈ 35-40 tracks

# ── Genre normalization ─────────────────────────────────────────────────────
# Maps both Last.fm's broad tags and LLM-specific tags into ~12 families.
# Tags mapping to None are non-genre noise (demographics, decades) and dropped.

GENRE_FAMILIES = {
    # Rock
    "rock": "rock", "alternative rock": "rock", "classic rock": "rock",
    "indie rock": "rock", "post-rock": "rock", "post-punk": "rock",
    "punk": "rock", "garage rock": "rock", "psychedelic rock": "rock",
    "folk rock": "rock", "blues rock": "rock", "hard rock": "rock",
    "grunge": "rock", "britpop": "rock", "new wave": "rock",
    "shoegaze": "rock", "noise rock": "rock", "stoner rock": "rock",
    "math rock": "rock", "punk rock": "rock", "progressive rock": "rock",
    "space rock": "rock",
    # Pop
    "pop": "pop", "indie pop": "pop", "pop rock": "pop",
    "synth-pop": "pop", "synthpop": "pop", "dream pop": "pop",
    "art pop": "pop", "chamber pop": "pop", "baroque pop": "pop",
    "folk pop": "pop", "electropop": "pop", "alternative pop": "pop",
    "darkwave": "pop", "twee pop": "pop",
    # Alternative / indie (umbrella)
    "alternative": "alternative", "indie": "alternative",
    "lo-fi": "alternative", "experimental": "alternative",
    # Electronic
    "electronic": "electronic", "electronica": "electronic",
    "ambient": "electronic", "chillout": "electronic",
    "downtempo": "electronic", "idm": "electronic",
    "techno": "electronic", "house": "electronic",
    "minimal": "electronic", "neoclassical": "electronic",
    "neo-classical": "electronic", "glitch": "electronic",
    "drum and bass": "electronic", "dubstep": "electronic",
    "trance": "electronic", "deep house": "electronic",
    # Trip-hop
    "trip-hop": "trip-hop", "trip hop": "trip-hop",
    # Folk / acoustic / singer-songwriter
    "folk": "folk", "indie folk": "folk", "acoustic": "folk",
    "singer-songwriter": "folk", "country": "folk",
    "americana": "folk", "romanian folk": "folk",
    "world": "folk", "world music": "folk",
    # Soul / R&B / funk
    "soul": "soul", "rnb": "soul", "r&b": "soul",
    "neo-soul": "soul", "motown": "soul", "funk": "soul",
    # Blues
    "blues": "blues", "delta blues": "blues",
    # Jazz
    "jazz": "jazz", "smooth jazz": "jazz", "acid jazz": "jazz",
    "nu jazz": "jazz",
    # Hip-hop
    "hip-hop": "hip-hop", "hip hop": "hip-hop", "rap": "hip-hop",
    "trap": "hip-hop",
    # Romanian
    "romanian": "romanian",
    # Classical
    "classical": "classical", "orchestral": "classical",
    # Noise tags — drop these (non-genre demographic/decade markers)
    "british": None, "american": None, "german": None, "french": None,
    "irish": None, "swedish": None, "canadian": None, "australian": None,
    "italian": None, "icelandic": None, "belgian": None, "scottish": None,
    "norwegian": None, "japanese": None,
    "female vocalists": None, "male vocalists": None,
    "80s": None, "70s": None, "90s": None, "60s": None, "00s": None,
    "10s": None, "2000s": None, "2010s": None,
    "seen live": None, "favorites": None, "favourite": None,
    "under 2000 listeners": None, "spotify": None,
}


def normalize_genre(tag: str) -> str | None:
    """Map a raw genre tag to a family name, or None to discard."""
    tag = tag.lower().strip()
    if tag in GENRE_FAMILIES:
        return GENRE_FAMILIES[tag]
    # Fuzzy fallback: check if any family key is a substring
    for key, family in GENRE_FAMILIES.items():
        if key in tag and family is not None:
            return family
    # Unknown tag — keep it as-is (contributes to both sides equally)
    return tag


# ── Vector math ──────────────────────────────────────────────────────────────

def to_distribution(counter: Counter, top_n: int = 30) -> dict[str, float]:
    """Normalize a counter into a percentage distribution."""
    total = sum(counter.values())
    if total == 0:
        return {}
    top_keys = [k for k, _ in counter.most_common(top_n)]
    return {k: counter[k] / total for k in top_keys}


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity between two sparse vectors."""
    all_keys = set(a) | set(b)
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in all_keys)
    mag_a = math.sqrt(sum(v ** 2 for v in a.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def distribution_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """Mean absolute difference between two distributions (lower = closer)."""
    all_keys = set(a) | set(b)
    if not all_keys:
        return 1.0
    return sum(abs(a.get(k, 0) - b.get(k, 0)) for k in all_keys) / len(all_keys)


# ── Feature extraction ──────────────────────────────────────────────────────

def extract_features(tracks: list[dict]) -> dict:
    """Extract style features from a track list."""
    # Genre distribution — normalize to families, drop noise tags
    genre_counts = Counter()
    for t in tracks:
        for g in t.get("genre_tags", t.get("genres", [])):
            family = normalize_genre(g)
            if family is not None:
                genre_counts[family] += 1
    genre_dist = to_distribution(genre_counts)

    # Popularity buckets
    pop_buckets = Counter()
    for t in tracks:
        listeners = t.get("metadata", {}).get("lastfm_listeners", 0)
        if listeners == 0:
            continue
        if listeners < 50_000:
            pop_buckets["underground"] += 1
        elif listeners < 500_000:
            pop_buckets["indie"] += 1
        elif listeners < 2_000_000:
            pop_buckets["known"] += 1
        else:
            pop_buckets["mainstream"] += 1
    pop_dist = to_distribution(pop_buckets, top_n=4)

    # Artist diversity
    unique_artists = len(set(t["artist"].lower() for t in tracks))
    artist_diversity = unique_artists / max(len(tracks), 1)

    # Romanian presence — check raw tags and normalized family
    # Note: "ro" was removed as a marker — it false-matches "rock", "electronic", etc.
    romanian_markers = {"romanian", "romania"}
    romanian = set()
    for t in tracks:
        raw_tags = t.get("genre_tags", t.get("genres", []))
        genres_str = " ".join(raw_tags).lower()
        if any(m in genres_str for m in romanian_markers):
            romanian.add(t["artist"].lower())
        elif any(normalize_genre(g) == "romanian" for g in raw_tags):
            romanian.add(t["artist"].lower())
    romanian_pct = len(romanian) / max(unique_artists, 1)

    return {
        "genre_dist": genre_dist,
        "pop_dist": pop_dist,
        "artist_diversity": artist_diversity,
        "unique_artists": unique_artists,
        "total_tracks": len(tracks),
        "romanian_pct": romanian_pct,
        "top_genres": genre_counts.most_common(10),
    }


# ── Scoring ──────────────────────────────────────────────────────────────────

def score_playlist(generated: list[dict], reference: list[dict], all_known_artists: set) -> dict:
    """Score a generated playlist against the reference. Returns per-dimension scores."""
    gen_f = extract_features(generated)
    ref_f = extract_features(reference)

    # 1. Genre similarity (cosine, 0-1)
    genre_sim = cosine_similarity(gen_f["genre_dist"], ref_f["genre_dist"])

    # 2. Popularity curve match (1 - distance, 0-1)
    # Only score if generated tracks have listener data; otherwise skip
    if gen_f["pop_dist"] and ref_f["pop_dist"]:
        pop_sim = max(0, 1 - distribution_distance(gen_f["pop_dist"], ref_f["pop_dist"]) * 3)
        pop_available = True
    else:
        pop_sim = 0.0
        pop_available = False

    # 3. Artist diversity
    # For a single-night playlist, ~1.0 diversity is ideal (no same artist twice).
    # Reference spans multiple nights so its aggregate diversity is artificially low.
    # Score based on how close to 1.0 the generated diversity is.
    diversity_score = gen_f["artist_diversity"]  # 1.0 = perfect, lower = repeats

    # 4. Freshness — what % of generated artists are NOT in the known set
    gen_artists = set(t["artist"].lower() for t in generated)
    new_artists = gen_artists - all_known_artists
    freshness = len(new_artists) / max(len(gen_artists), 1)
    # We want ~60% fresh, penalize if too low or too high
    freshness_score = max(0.0, 1.0 - abs(0.6 - freshness) * 2)

    # 5. Romanian presence (closeness to reference %)
    ro_diff = abs(gen_f["romanian_pct"] - ref_f["romanian_pct"])
    romanian_score = max(0, 1.0 - ro_diff * 5)

    # 6. Track count (target ~37 for a 6-hour block)
    count_diff = abs(gen_f["total_tracks"] - TARGET_TRACKS) / TARGET_TRACKS
    count_score = max(0, 1.0 - count_diff)

    # Weighted composite — redistribute popularity weight if no data
    if pop_available:
        weights = {
            "genre_match": (genre_sim, 0.30),
            "popularity_curve": (pop_sim, 0.15),
            "artist_diversity": (diversity_score, 0.15),
            "freshness": (freshness_score, 0.20),
            "romanian_presence": (romanian_score, 0.10),
            "track_count": (count_score, 0.10),
        }
    else:
        # No popularity data — redistribute 15% to genre (→35%) and freshness (→25%)
        weights = {
            "genre_match": (genre_sim, 0.35),
            "popularity_curve": (pop_sim, 0.00),
            "artist_diversity": (diversity_score, 0.15),
            "freshness": (freshness_score, 0.25),
            "romanian_presence": (romanian_score, 0.15),
            "track_count": (count_score, 0.10),
        }

    composite = sum(score * weight for score, weight in weights.values())

    return {
        "composite": round(composite * 100, 1),
        "dimensions": {
            name: {
                "score": round(score * 100, 1),
                "weight": f"{int(weight * 100)}%",
            }
            for name, (score, weight) in weights.items()
        },
        "generated_features": {
            "total_tracks": gen_f["total_tracks"],
            "unique_artists": gen_f["unique_artists"],
            "artist_diversity": round(gen_f["artist_diversity"], 3),
            "romanian_pct": round(gen_f["romanian_pct"] * 100, 1),
            "freshness_pct": round(freshness * 100, 1),
            "top_genres": [(g, c) for g, c in gen_f["top_genres"]],
        },
        "reference_features": {
            "total_tracks": ref_f["total_tracks"],
            "unique_artists": ref_f["unique_artists"],
            "artist_diversity": round(ref_f["artist_diversity"], 3),
            "romanian_pct": round(ref_f["romanian_pct"] * 100, 1),
            "top_genres": [(g, c) for g, c in ref_f["top_genres"]],
        },
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    verbose = "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if not args:
        print("Usage: python3 score_playlist.py <playlist.json> [--verbose]")
        sys.exit(1)

    playlist_path = args[0]

    # Load generated playlist
    with open(playlist_path, encoding="utf-8") as f:
        gen_data = json.load(f)
    generated = gen_data.get("tracks", gen_data) if isinstance(gen_data, dict) else gen_data

    # Load knowledge base
    with open(DB_PATH, encoding="utf-8") as f:
        db = json.load(f)
    all_tracks = db["tracks"]

    # Split into reference and test
    dates = sorted(set(t["date"] for t in all_tracks))
    test_dates = set(dates[-TEST_NIGHTS:])
    ref_dates = set(dates[:-TEST_NIGHTS])

    reference = [t for t in all_tracks if t["date"] in ref_dates]
    test_set = [t for t in all_tracks if t["date"] in test_dates]
    all_known_artists = set(t["artist"].lower() for t in all_tracks)

    print("=" * 55)
    print("  Guerrilla Night — Style Scorer")
    print("=" * 55)
    print(f"  Playlist:  {playlist_path}")
    print(f"  Generator: {gen_data.get('generator', '?')} ({gen_data.get('model_id', '?')})")
    print(f"  Reference: {len(reference)} tracks ({len(ref_dates)} nights)")
    print(f"  Test set:  {len(test_set)} tracks ({len(test_dates)} nights: {', '.join(sorted(test_dates))})")
    print()

    # Score against reference (style match)
    ref_scores = score_playlist(generated, reference, all_known_artists)

    # Score test set against reference (baseline — how consistent is Guerrilla itself?)
    baseline = score_playlist(test_set, reference, all_known_artists)

    print(f"  {'DIMENSION':<22} {'GENERATED':>10} {'BASELINE':>10} {'WEIGHT':>8}")
    print(f"  {'─' * 52}")
    for dim in ref_scores["dimensions"]:
        gen_s = ref_scores["dimensions"][dim]["score"]
        base_s = baseline["dimensions"][dim]["score"]
        weight = ref_scores["dimensions"][dim]["weight"]
        delta = gen_s - base_s
        arrow = "▲" if delta > 0 else "▼" if delta < 0 else "="
        print(f"  {dim:<22} {gen_s:>9.1f} {base_s:>9.1f}  {arrow}  {weight:>5}")

    print(f"  {'─' * 52}")
    print(f"  {'COMPOSITE':<22} {ref_scores['composite']:>9.1f} {baseline['composite']:>9.1f}")
    print()

    # Interpretation
    score = ref_scores["composite"]
    if score >= 80:
        verdict = "Excellent — this playlist IS Guerrilla Night."
    elif score >= 65:
        verdict = "Good — clearly Guerrilla-inspired, minor drift."
    elif score >= 50:
        verdict = "Decent — captures some DNA but diverges in places."
    else:
        verdict = "Weak — doesn't feel like Guerrilla Night."

    print(f"  Verdict: {verdict}")
    print(f"  Score: {score}/100 (baseline: {baseline['composite']}/100)")

    if verbose:
        print(f"\n  Generated playlist features:")
        gf = ref_scores["generated_features"]
        print(f"    Tracks: {gf['total_tracks']}, Unique artists: {gf['unique_artists']}")
        print(f"    Diversity: {gf['artist_diversity']}, Fresh: {gf['freshness_pct']}%")
        print(f"    Romanian: {gf['romanian_pct']}%")
        print(f"    Top genres: {', '.join(f'{g}({c})' for g, c in gf['top_genres'][:8])}")

        print(f"\n  Reference features:")
        rf = ref_scores["reference_features"]
        print(f"    Tracks: {rf['total_tracks']}, Unique artists: {rf['unique_artists']}")
        print(f"    Diversity: {rf['artist_diversity']}")
        print(f"    Romanian: {rf['romanian_pct']}%")
        print(f"    Top genres: {', '.join(f'{g}({c})' for g, c in rf['top_genres'][:8])}")

    # Save score report
    report_path = playlist_path.replace(".json", "_score.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "playlist": playlist_path,
            "composite_score": ref_scores["composite"],
            "baseline_score": baseline["composite"],
            "dimensions": ref_scores["dimensions"],
            "generated_features": ref_scores["generated_features"],
            "reference_features": ref_scores["reference_features"],
        }, f, indent=2)
    print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    main()
