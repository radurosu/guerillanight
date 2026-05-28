#!/usr/bin/env python3
"""
Weekly scraper for Radio Guerrilla's overnight block (20:00–06:00).
Fetches the past week from OnlineRadioBox, enriches with Last.fm,
and appends to the cumulative knowledge base.

Designed for cron: idempotent, deduplicates, resumes enrichment.

Usage:
    python3 scrape_weekly.py                  # scrape + enrich
    python3 scrape_weekly.py --scrape-only    # skip enrichment

Env:
    LASTFM_API_KEY   — required for genre enrichment
    GUERRILLA_DATA   — override data dir (default: ./data)
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("pip3 install requests beautifulsoup4")
    sys.exit(1)

ROMANIA_TZ = ZoneInfo("Europe/Bucharest")
BLOCK_START = 20
BLOCK_END = 6
DATA_DIR = os.environ.get("GUERRILLA_DATA", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "guerrilla_knowledge.json")
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
    url = f"https://onlineradiobox.com/ro/guerrilla/playlist/{offset}" if offset else \
          "https://onlineradiobox.com/ro/guerrilla/playlist/"

    print(f"    {url}", flush=True)
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
    print("  Fetching 7 days from OnlineRadioBox...", flush=True)
    day_cache = {}
    for offset in range(7):
        date_str, tracks = fetch_day_playlist(offset)
        day_cache[offset] = (date_str, tracks)
        time.sleep(0.5)

    all_tracks = []
    seen = set()

    for evening_offset in range(6, -1, -1):
        morning_offset = evening_offset - 1
        _, eve_tracks = day_cache[evening_offset]
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
    params.update({"method": method, "api_key": LASTFM_API_KEY, "format": "json"})
    try:
        resp = requests.get(LASTFM_API, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return None if "error" in data else data
    except requests.exceptions.Timeout:
        print("      [timeout]", flush=True)
        return None
    except Exception as e:
        print(f"      [{e.__class__.__name__}]", flush=True)
        return None


def lastfm_artist_tags(artist: str) -> list[str]:
    data = lastfm_get("artist.getTopTags", artist=artist, autocorrect="1")
    if not data:
        return []
    tags = data.get("toptags", {}).get("tag", [])
    return [t["name"].lower() for t in tags[:10] if int(t.get("count", 0)) > 0]


def lastfm_track_info(artist: str, title: str) -> dict:
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
BATCH_PAUSE = 10


def enrich_tracks(tracks: list[dict]) -> list[dict]:
    pending = [(i, t) for i, t in enumerate(tracks) if not (t.get("genres") and t.get("metadata"))]
    already = len(tracks) - len(pending)

    if already:
        print(f"  {already} already enriched, {len(pending)} remaining.", flush=True)
    if not pending:
        print("  All tracks enriched.", flush=True)
        return tracks

    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    print(f"  {len(pending)} tracks in {len(batches)} batches...\n", flush=True)

    artist_cache = {}
    for b_num, batch in enumerate(batches):
        print(f"  ── Batch {b_num + 1}/{len(batches)} ──", flush=True)

        for idx, track in batch:
            print(f"    [{idx+1}/{len(tracks)}] {track['artist']} — {track['title']}", flush=True)

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

        save_db(tracks)
        done = min((b_num + 1) * BATCH_SIZE, len(pending))
        genres_found = sum(1 for t in tracks if t.get("genres"))
        print(f"  ── Saved. {done}/{len(pending)} done, {genres_found} genres total.\n", flush=True)

        if b_num < len(batches) - 1:
            print(f"  ── Pausing {BATCH_PAUSE}s...", flush=True)
            time.sleep(BATCH_PAUSE)

    return tracks


# ── Persistence ──────────────────────────────────────────────────────────────

def load_db() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    with open(DB_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {(e["date"], e["time"], e["artist"]): e for e in data.get("tracks", [])}


def save_db(tracks_or_dict):
    if isinstance(tracks_or_dict, dict):
        all_tracks = sorted(tracks_or_dict.values(), key=lambda t: (t["date"], t["time"]))
    else:
        all_tracks = sorted(tracks_or_dict, key=lambda t: (t["date"], t["time"]))

    dates = sorted(set(t["date"] for t in all_tracks))
    genres_found = sum(1 for t in all_tracks if t.get("genres"))

    output = {
        "station": "Radio Guerrilla",
        "block": "overnight (20:00–06:00)",
        "description": "Cumulative playlist knowledge base for AI style learning.",
        "last_updated": datetime.now(ROMANIA_TZ).isoformat(),
        "date_range": {"first": dates[0], "last": dates[-1]} if dates else {},
        "track_count": len(all_tracks),
        "enriched_count": genres_found,
        "tracks": all_tracks,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    scrape_only = "--scrape-only" in sys.argv

    print("=" * 55)
    print("  Radio Guerrilla — Weekly Scraper")
    print("=" * 55)
    now_ro = datetime.now(ROMANIA_TZ)
    print(f"  Run at: {now_ro.strftime('%Y-%m-%d %H:%M')} Romania time")
    print(f"  Data:   {DB_PATH}")
    print()

    # Load existing
    existing = load_db()
    print(f"  Existing tracks in DB: {len(existing)}")

    # Scrape
    print("\n[1/2] Scraping...")
    new_tracks = scrape_week()
    new_count = 0
    for t in new_tracks:
        key = (t["date"], t["time"], t["artist"])
        if key not in existing:
            existing[key] = t
            new_count += 1

    print(f"\n  Found {len(new_tracks)} this week, {new_count} new.")
    save_db(existing)

    # Enrich
    if scrape_only:
        print("\n[2/2] Enrichment skipped (--scrape-only).")
    elif not LASTFM_API_KEY:
        print("\n[2/2] Enrichment skipped (no LASTFM_API_KEY).")
    else:
        print("\n[2/2] Enriching with Last.fm...")
        all_tracks = list(existing.values())
        enrich_tracks(all_tracks)
        existing = {(t["date"], t["time"], t["artist"]): t for t in all_tracks}
        save_db(existing)

    # Summary
    all_tracks = list(existing.values())
    genres_found = sum(1 for t in all_tracks if t.get("genres"))
    dates = sorted(set(t["date"] for t in all_tracks))
    print(f"\n{'='*55}")
    print(f"  Total tracks  : {len(all_tracks)}")
    print(f"  Date range    : {dates[0]} → {dates[-1]}")
    print(f"  Genres tagged : {genres_found}/{len(all_tracks)}")
    print(f"  Saved to      : {DB_PATH}")


if __name__ == "__main__":
    main()
