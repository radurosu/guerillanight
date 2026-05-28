#!/usr/bin/env python3
"""
Scrapes Radio Guerrilla's playlist from OnlineRadioBox for the most recent
Tuesday (Nic Cocîrlea's Nomansland show, 21:00–23:00 Romania time) and
appends new tracks to ~/nomansland_playlist.csv.

Requirements: pip3 install requests beautifulsoup4
"""

import csv
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip3 install requests beautifulsoup4")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

ROMANIA_TZ    = ZoneInfo("Europe/Bucharest")
SHOW_START    = 21   # 21:00 Romania time
SHOW_END      = 24
OUTPUT_CSV    = os.path.expanduser("~/nomansland_playlist.csv")
CSV_HEADERS   = ["date", "time", "artist", "title", "source_url"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def find_tuesday_offset() -> int:
    """
    OnlineRadioBox uses offsets: 0 = today, 1 = yesterday, etc.
    Find the offset for the most recent Tuesday (Romania time).
    """
    now_ro = datetime.now(ROMANIA_TZ)
    days_since_tuesday = (now_ro.weekday() - 1) % 7  # Monday=0, Tuesday=1
    if days_since_tuesday == 0:
        # It's Tuesday — but only use today if the show has already started
        if now_ro.hour < SHOW_START:
            days_since_tuesday = 7  # use last Tuesday instead
    return days_since_tuesday


def fetch_playlist(offset: int) -> tuple[str, list[dict]]:
    """Fetch the playlist page and return (date_str, list of track dicts)."""
    url = f"https://onlineradiobox.com/ro/guerrilla/playlist/{offset}" if offset else \
          "https://onlineradiobox.com/ro/guerrilla/playlist/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ro,en;q=0.9",
    }

    print(f"  Fetching: {url}")
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Active day is a <span> like "Mar19.05" (day abbrev + DD.MM)
    date_str = "unknown"
    for el in soup.find_all("span"):
        m = re.search(r"(\d{2})\.(\d{2})", el.get_text(strip=True))
        if m:
            day, month = m.groups()
            date_str = f"{datetime.now(ROMANIA_TZ).year}-{month}-{day}"
            break

    # Parse the playlist table
    tracks = []
    table = soup.find("table")
    if not table:
        return date_str, tracks

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        time_text = cells[0].get_text(strip=True)   # e.g. "21:34"
        track_text = cells[1].get_text(strip=True)  # e.g. "ARTIST - Title"

        if not re.match(r"^\d{2}:\d{2}$", time_text):
            continue
        if not track_text or track_text == "Radio Guerrilla":
            continue

        hour = int(time_text.split(":")[0])
        if not (SHOW_START <= hour < SHOW_END):
            continue

        # Split "ARTIST - Title" — split on first " - "
        if " - " in track_text:
            artist, title = track_text.split(" - ", 1)
        else:
            artist = track_text
            title = ""

        tracks.append({
            "date":       date_str,
            "time":       time_text,
            "artist":     artist.strip(),
            "title":      title.strip(),
            "source_url": url,
        })

    return date_str, tracks


def load_existing(csv_path: str) -> set[tuple]:
    """Return a set of (date, time, artist, title) already in the CSV."""
    seen = set()
    if not os.path.exists(csv_path):
        return seen
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            seen.add((row["date"], row["time"], row["artist"], row["title"]))
    return seen


def append_tracks(csv_path: str, tracks: list[dict], seen: set[tuple]) -> int:
    """Append new tracks to the CSV. Returns count of newly added tracks."""
    file_exists = os.path.exists(csv_path)
    new_count = 0

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()

        for track in tracks:
            key = (track["date"], track["time"], track["artist"], track["title"])
            if key not in seen:
                writer.writerow(track)
                seen.add(key)
                new_count += 1

    return new_count


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  Nomansland Weekly Playlist Scraper")
    print("=" * 55)

    offset = find_tuesday_offset()
    now_ro = datetime.now(ROMANIA_TZ)
    target = now_ro - timedelta(days=offset)
    print(f"  Target Tuesday: {target.strftime('%A %d %B %Y')} (offset={offset})")
    print(f"  Show window: {SHOW_START}:00 – {SHOW_END-1}:59 Romania time")
    print(f"  Output CSV: {OUTPUT_CSV}")
    print()

    print("Fetching playlist...")
    date_str, tracks = fetch_playlist(offset)

    if not tracks:
        print("No tracks found in the show window. The page structure may have changed,")
        print("or the show hasn't aired yet. Try running again after Tuesday 23:00 Romania time.")
        return

    print(f"Found {len(tracks)} tracks for {date_str} in the Nomansland window.")
    print()

    seen = load_existing(OUTPUT_CSV)
    added = append_tracks(OUTPUT_CSV, tracks, seen)

    print(f"Results:")
    print(f"  Tracks found this week : {len(tracks)}")
    print(f"  New tracks added to CSV: {added}")
    print(f"  Duplicates skipped     : {len(tracks) - added}")
    print()

    # Pretty-print what was found this week
    print(f"  Nomansland — {date_str}")
    print("  " + "-" * 45)
    for t in tracks:
        print(f"  {t['time']}  {t['artist']} — {t['title']}")

    print()
    print(f"CSV saved to: {OUTPUT_CSV}")
    print("Run this script again next Wednesday to keep building the list.")


if __name__ == "__main__":
    main()

