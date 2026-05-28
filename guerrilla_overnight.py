#!/usr/bin/env python3
"""
Scrapes Radio Guerrilla's overnight playlists (20:00–06:00) from OnlineRadioBox
for the past week. Outputs a JSON file ready for AI style analysis.

Genre enrichment via Last.fm is a separate pass — run with --enrich once you
have an API key set in LASTFM_API_KEY env var.

Usage:
    python3 guerrilla_overnight.py             # scrape only
    python3 guerrilla_overnight.py --enrich    # enrich existing tracks with genres
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing deps. Run: pip3 install requests beautifulsoup4")
    sys.exit(1)

ROMANIA_TZ = ZoneInfo("Europe/Bucharest")
BLOCK_START = 20
BLOCK_END = 6
OUTPUT_JSON = os.path.expanduser("~/guerrilla_overnight.json")
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ro,en;q=0.9",
}


# ── Scraping ─────────────────────────────────────────────────────────────────

def fetch_day_playlist(offset: int) -> tuple[str, list[dict]]:
    """Fetch all tracks for a given day offset. Returns (date_str, tracks)."""
    url = f"https://onlineradiobox.com/ro/guerrilla/playlist/{offset}" if offset else \
          "https://onlineradiobox.com/ro/guerrilla/playlist/"

    print(f"    {url}")
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    date_str = "unknown"
    for el in soup.find_all("span"):
        m = re.search(r"(\d{2})\.(\d{2})", el.get_text(strip=True))
        if m:
            day, month = m.groups()
            date_str = f"{datetime.now(ROMANIA_TZ).year}-{month}-{day}"
            break

    tracks = []
    table = soup.find("table")
    if not table:
        return date_str, tracks

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        time_text = cells[0].get_text(strip=True)
        track_text = cells[1].get_text(strip=True)
        if not re.match(r"^\d{2}:\d{2}$", time_text):
            continue
        if not track_text or track_text == "Radio Guerrilla":
            continue

        if " - " in track_text:
            artist, title = track_text.split(" - ", 1)
        else:
            artist, title = track_text, ""

        tracks.append({
            "date": date_str,
            "time": time_text,
            "artist": artist.strip(),
            "title": title.strip(),
        })

    return date_str, tracks


def scrape_week() -> list[dict]:
    """
    Scrape the full week of overnight blocks (20:00–06:00).
    OnlineRadioBox keeps 7 days: offset 0=today .. 6=six days ago.
    Each overnight block spans two calendar days, so we fetch all 7 days
    once and then assemble the blocks.
    """
    now_ro = datetime.now(ROMANIA_TZ)

    # Fetch all available days (0..6)
    day_cache = {}
    print("  Fetching all available days...")
    for offset in range(7):
        date_str, tracks = fetch_day_playlist(offset)
        day_cache[offset] = (date_str, tracks)
        time.sleep(0.5)

    # Build overnight blocks: evening of day N (20:00-23:59) + morning of day N-1 (00:00-05:59)
    # offset 6 = oldest evening, offset 5 = its next morning
    all_tracks = []
    seen = set()

    for evening_offset in range(6, -1, -1):
        morning_offset = evening_offset - 1
        eve_date, eve_tracks = day_cache[evening_offset]

        evening_block = [t for t in eve_tracks if int(t["time"].split(":")[0]) >= BLOCK_START]

        morning_block = []
        if morning_offset >= 0:
            _, morn_tracks = day_cache[morning_offset]
            morning_block = [t for t in morn_tracks if int(t["time"].split(":")[0]) < BLOCK_END]

        for t in evening_block + morning_block:
            key = (t["date"], t["time"], t["artist"])
            if key not in seen:
                seen.add(key)
                all_tracks.append(t)

    all_tracks.sort(key=lambda t: (t["date"], t["time"]))
    return all_tracks


# ── Last.fm enrichment ───────────────────────────────────────────────────────

def lastfm_get(method: str, **params) -> dict | None:
    """Make a Last.fm API call."""
    params.update({"method": method, "api_key": LASTFM_API_KEY, "format": "json"})
    try:
        resp = requests.get(LASTFM_API, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        return data
    except requests.exceptions.Timeout:
        print("    [timeout]", file=sys.stderr, flush=True)
        return None
    except Exception as e:
        print(f"    [error: {e.__class__.__name__}]", file=sys.stderr, flush=True)
        return None


def lastfm_artist_tags(artist: str) -> list[str]:
    """Get top tags for an artist from Last.fm."""
    data = lastfm_get("artist.getTopTags", artist=artist, autocorrect="1")
    if not data:
        return []
    tags = data.get("toptags", {}).get("tag", [])
    return [t["name"].lower() for t in tags[:10] if int(t.get("count", 0)) > 0]


def lastfm_track_info(artist: str, title: str) -> dict:
    """Get track info from Last.fm — tags, album, duration, playcount."""
    data = lastfm_get("track.getInfo", artist=artist, track=title, autocorrect="1")
    if not data:
        return {}
    track = data.get("track", {})
    info = {}
    if track.get("album"):
        info["album"] = track["album"].get("title", "")
    if track.get("duration") and track["duration"] != "0":
        info["duration_ms"] = int(track["duration"])
    if track.get("playcount"):
        info["lastfm_playcount"] = int(track["playcount"])
    if track.get("listeners"):
        info["lastfm_listeners"] = int(track["listeners"])

    track_tags = track.get("toptags", {}).get("tag", [])
    if track_tags:
        info["track_tags"] = [t["name"].lower() for t in track_tags[:5]]

    return info


BATCH_SIZE = 30
BATCH_PAUSE = 10  # seconds between batches


def enrich_with_lastfm(tracks: list[dict]) -> list[dict]:
    """Enrich tracks in small batches with pauses to avoid rate limits."""
    total = len(tracks)
    artist_cache = {}

    pending = [(i, t) for i, t in enumerate(tracks) if not (t.get("genres") and t.get("metadata"))]
    already = total - len(pending)

    if already:
        print(f"  {already} tracks already enriched, {len(pending)} remaining.", flush=True)
    if not pending:
        print("  Nothing to do.", flush=True)
        return tracks

    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    print(f"  Processing {len(pending)} tracks in {len(batches)} batches of {BATCH_SIZE}...\n", flush=True)

    for b_num, batch in enumerate(batches):
        print(f"  ── Batch {b_num + 1}/{len(batches)} ──", flush=True)

        for idx, track in batch:
            print(f"    [{idx+1}/{total}] {track['artist']} — {track['title']}", flush=True)

            artist_key = track["artist"].lower()
            if artist_key not in artist_cache:
                artist_cache[artist_key] = lastfm_artist_tags(track["artist"])
                time.sleep(0.3)

            track["genres"] = artist_cache[artist_key]

            if track["title"]:
                info = lastfm_track_info(track["artist"], track["title"])
                if info:
                    track["metadata"] = info
                time.sleep(0.3)

        _save_progress(tracks)
        done = min((b_num + 1) * BATCH_SIZE, len(pending))
        genres_so_far = sum(1 for t in tracks if t.get("genres"))
        print(f"  ── Saved. {done}/{len(pending)} done, {genres_so_far} genres found.", flush=True)

        if b_num < len(batches) - 1:
            print(f"  ── Pausing {BATCH_PAUSE}s before next batch...\n", flush=True)
            time.sleep(BATCH_PAUSE)

    return tracks


def _save_progress(tracks: list[dict]):
    """Quick-save current state to disk."""
    all_tracks = sorted(tracks, key=lambda t: (t["date"], t["time"]))
    output = {
        "station": "Radio Guerrilla",
        "block": "overnight (20:00–06:00)",
        "description": "Playlist data for AI style learning — genres, artist metadata, "
                       "and track sequencing from Radio Guerrilla's overnight music blocks.",
        "last_updated": datetime.now(ROMANIA_TZ).isoformat(),
        "track_count": len(all_tracks),
        "tracks": all_tracks,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


# ── Persistence ──────────────────────────────────────────────────────────────

def load_existing(path: str) -> dict:
    """Load existing JSON data, keyed by (date, time, artist) for dedup."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {
        (e["date"], e["time"], e["artist"]): e
        for e in data.get("tracks", [])
    }


def save_json(path: str, tracks: list[dict], existing: dict) -> int:
    """Merge new tracks with existing and save. Returns count of new tracks."""
    new_count = 0
    for t in tracks:
        key = (t["date"], t["time"], t["artist"])
        if key not in existing:
            existing[key] = t
            new_count += 1
        else:
            existing[key].update({k: v for k, v in t.items() if v})

    all_tracks = sorted(existing.values(), key=lambda t: (t["date"], t["time"]))

    output = {
        "station": "Radio Guerrilla",
        "block": "overnight (20:00–06:00)",
        "description": "Playlist data for AI style learning — genres, artist metadata, "
                       "and track sequencing from Radio Guerrilla's overnight music blocks.",
        "last_updated": datetime.now(ROMANIA_TZ).isoformat(),
        "track_count": len(all_tracks),
        "tracks": all_tracks,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return new_count


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    enrich_mode = "--enrich" in sys.argv

    print("=" * 55)
    print("  Radio Guerrilla — Overnight Scraper")
    print("=" * 55)
    print(f"  Block: {BLOCK_START}:00 – {BLOCK_END:02d}:00 next day")
    print(f"  Output: {OUTPUT_JSON}")
    print()

    existing = load_existing(OUTPUT_JSON)

    if enrich_mode:
        if not LASTFM_API_KEY:
            print("Set LASTFM_API_KEY env var first.")
            print("  export LASTFM_API_KEY=your_key_here")
            sys.exit(1)
        print(f"Enriching {len(existing)} existing tracks with Last.fm data...")
        tracks = list(existing.values())
        tracks = enrich_with_lastfm(tracks)
        existing = {(t["date"], t["time"], t["artist"]): t for t in tracks}
        save_json(OUTPUT_JSON, [], existing)
        genres_found = sum(1 for t in tracks if t.get("genres"))
        print(f"\n  Genres resolved: {genres_found}/{len(tracks)}")
        print(f"  Saved to: {OUTPUT_JSON}")
        return

    print("Scraping the full week...")
    tracks = scrape_week()

    if not tracks:
        print("No tracks found. The page structure may have changed.")
        return

    added = save_json(OUTPUT_JSON, tracks, existing)
    total = len(existing) + added

    print(f"\nResults:")
    print(f"  Tracks this week   : {len(tracks)}")
    print(f"  New tracks added   : {added}")
    print(f"  Total in database  : {total}")

    dates = sorted(set(t["date"] for t in tracks))
    print(f"  Days covered       : {len(dates)}")
    for d in dates:
        day_count = sum(1 for t in tracks if t["date"] == d)
        print(f"    {d}: {day_count} tracks")

    print(f"\nSaved to: {OUTPUT_JSON}")
    if not LASTFM_API_KEY:
        print("\nTo add genres, set LASTFM_API_KEY and run with --enrich")


if __name__ == "__main__":
    main()
