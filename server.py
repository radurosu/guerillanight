#!/usr/bin/env python3
"""
Guerrilla Night — Web server with on-demand playlist generation.

Serves the site and provides an SSE endpoint for live generation progress.

Usage:
    python3 server.py                    # default port 8900
    python3 server.py --port 8900

Env (.env supported):
    ANTHROPIC_API_KEY, XAI_API_KEY, etc.
"""

import asyncio
import hashlib
import json
import re
import os
import random
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

# ── Bootstrap ────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROMANIA_TZ = ZoneInfo("Europe/Bucharest")
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "guerrilla_knowledge.json")
PLAYLISTS_DIR = os.path.join(DATA_DIR, "playlists")
DJ_CLIPS_DIR = os.path.join(DATA_DIR, "dj_clips")
os.makedirs(DJ_CLIPS_DIR, exist_ok=True)

# Load .env
def load_env():
    env_path = os.path.join(SCRIPT_DIR, ".env")
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

load_env()

# Import project modules
sys.path.insert(0, SCRIPT_DIR)
from generate_playlist import (
    compute_style_profile, build_prompt, parse_playlist, save_playlist,
    trim_anchors, anchor_rank_from_profile, MODELS, CALLERS
)
from score_playlist import score_playlist as run_score, extract_features

app = FastAPI(title="Guerrilla Night")
app.mount("/dj_clips", StaticFiles(directory=DJ_CLIPS_DIR), name="dj_clips")


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_available_models() -> list[dict]:
    """Return models that have API keys configured."""
    models = []
    for key, info in MODELS.items():
        if os.environ.get(info["env_key"], ""):
            models.append({"key": key, "name": info["name"], "model_id": info["model_id"]})
    return models


def search_youtube(artist: str, title: str) -> str | None:
    """Search YouTube via web scrape — no API key or yt-dlp needed."""
    import re
    import urllib.request
    import urllib.parse

    query = urllib.parse.quote(f"{artist} {title}")
    url = f"https://www.youtube.com/results?search_query={query}&sp=EgIQAQ%3D%3D"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode("utf-8", errors="ignore")
        ids = re.findall(r'watch\?v=([a-zA-Z0-9_-]{11})', html)
        seen = []
        for vid in ids:
            if vid not in seen:
                seen.append(vid)
        return seen[0] if seen else None
    except Exception:
        return None


def load_knowledge_base():
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── SSE Generation ──────────────────────────────────────────────────────────

async def generate_stream(model_key: str):
    """Generator that yields SSE events during playlist creation."""

    def event(type: str, data: dict) -> str:
        return f"data: {json.dumps({'type': type, **data})}\n\n"

    yield event("status", {"message": "Loading knowledge base..."})
    await asyncio.sleep(0.1)

    try:
        db = load_knowledge_base()
        tracks = db["tracks"]
        yield event("status", {"message": f"Knowledge base: {len(tracks)} tracks"})
        await asyncio.sleep(0.1)

        # Style profile
        yield event("status", {"message": "Computing style profile..."})
        profile = compute_style_profile(tracks)
        await asyncio.sleep(0.1)

        # Build prompt
        model_info = MODELS[model_key]
        api_key = os.environ.get(model_info["env_key"], "")
        prompt = build_prompt(profile)

        yield event("status", {"message": f"Generating with {model_info['name']}... (30-60s)"})
        await asyncio.sleep(0.1)

        # Call LLM (blocking — run in thread)
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, CALLERS[model_key], prompt, api_key)

        # Parse
        playlist = parse_playlist(raw)
        if not playlist:
            yield event("error", {"message": "Failed to parse playlist from model response"})
            return

        # Enforce the signature-artist ceiling (the model overshoots the prompt window).
        playlist, before, after = trim_anchors(playlist, anchor_rank_from_profile(profile), hi=6)
        msg = f"Generated {len(playlist)} tracks. Saving..."
        if before != after:
            msg = f"Generated {len(playlist)} tracks ({before}→{after} signature artists). Saving..."

        yield event("status", {"message": msg})
        path = save_playlist(playlist, model_key)
        await asyncio.sleep(0.1)

        # Resolve YouTube IDs — stream each track as it's found
        yield event("status", {"message": f"Finding YouTube videos..."})
        found = 0
        for i, t in enumerate(playlist):
            vid = await loop.run_in_executor(None, search_youtube, t["artist"], t["title"])
            if vid:
                t["youtube_id"] = vid
                found += 1
                # Stream each resolved track immediately
                yield event("track", {
                    "id": vid,
                    "time": t.get("time", ""),
                    "artist": t["artist"],
                    "title": t["title"],
                    "genres": t.get("genre_tags", [])[:2],
                    "index": i,
                    "found": found,
                    "total": len(playlist),
                })
            if (i + 1) % 5 == 0 or i == len(playlist) - 1:
                yield event("progress", {
                    "message": f"Finding videos ({i+1}/{len(playlist)}, {found} found)...",
                    "current": i + 1,
                    "total": len(playlist),
                    "found": found,
                })
            await asyncio.sleep(0.05)

        # Save enriched playlist
        ts = datetime.now(ROMANIA_TZ).strftime("%Y%m%d_%H%M")
        filename = f"playlist_{model_key}_{ts}"
        json_path = os.path.join(PLAYLISTS_DIR, f"{filename}.json")

        output = {
            "generator": model_key,
            "model_id": model_info["model_id"],
            "generated_at": datetime.now(ROMANIA_TZ).isoformat(),
            "track_count": len(playlist),
            "tracks": playlist,
        }
        os.makedirs(PLAYLISTS_DIR, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        yield event("status", {"message": "Building player..."})

        # Build player data
        player_tracks = []
        for t in playlist:
            if t.get("youtube_id"):
                player_tracks.append({
                    "id": t["youtube_id"],
                    "time": t.get("time", ""),
                    "artist": t["artist"],
                    "title": t["title"],
                    "genres": t.get("genre_tags", [])[:2],
                })

        yield event("complete", {
            "message": f"Ready! {found}/{len(playlist)} tracks with video.",
            "tracks": player_tracks,
            "model": model_info["name"],
            "model_id": model_info["model_id"],
            "track_count": len(playlist),
            "youtube_found": found,
            "filename": filename,
        })

    except Exception as e:
        yield event("error", {"message": f"Error: {str(e)}"})


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/models")
async def api_models():
    return get_available_models()


@app.get("/api/config")
async def api_config():
    """Return public config for the frontend."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    return {"youtube_enabled": bool(client_id), "google_client_id": client_id}


@app.get("/stats", response_class=HTMLResponse)
async def stats_page():
    """Knowledge base stats page."""
    db = load_knowledge_base()
    tracks = db.get("tracks", [])
    dates = sorted(set(t["date"] for t in tracks))
    artists = {}
    genre_counts = Counter()
    has_genres = 0
    for t in tracks:
        a = t["artist"]
        artists[a] = artists.get(a, 0) + 1
        if t.get("genres"):
            has_genres += 1
            for g in t["genres"][:3]:
                genre_counts[g] += 1

    top_artists = sorted(artists.items(), key=lambda x: -x[1])[:20]
    top_genres = genre_counts.most_common(20)
    total = len(tracks)
    unique = len(artists)

    artist_rows = "".join(
        f'<tr><td><a href="/tracks?artist={urllib.parse.quote(a)}">{a}</a></td><td>{c}</td><td><div class="bar" style="width:{c/top_artists[0][1]*100:.0f}%"></div></td></tr>'
        for a, c in top_artists
    )
    genre_rows = "".join(
        f'<tr><td>{g}</td><td>{c}</td><td><div class="bar g" style="width:{c/top_genres[0][1]*100:.0f}%"></div></td></tr>'
        for g, c in top_genres
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guerrilla Night — Knowledge Base Stats</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0a0a0f;color:#e0ddd5;font-family:'Space Grotesk',sans-serif;min-height:100vh}}
  .wrap{{max-width:900px;margin:0 auto;padding:2rem 1rem}}
  h1{{font-size:1.8rem;font-weight:700;color:#fff;margin-bottom:.3rem}}
  h1 span{{background:linear-gradient(135deg,#c084fc,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  .sub{{color:rgba(255,255,255,.3);font-size:.85rem;margin-bottom:2rem}}
  .sub a{{color:rgba(192,132,252,.5);text-decoration:none}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.8rem;margin-bottom:2rem}}
  .card{{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:1.2rem;text-align:center}}
  .card .n{{font-size:2rem;font-weight:700;color:#fff;font-family:'JetBrains Mono',monospace}}
  .card .n span{{background:linear-gradient(135deg,#c084fc,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  .card .l{{font-size:.65rem;text-transform:uppercase;letter-spacing:.12em;color:rgba(255,255,255,.25);margin-top:.3rem}}
  .section{{margin-bottom:2rem}}
  .section h2{{font-size:.7rem;text-transform:uppercase;letter-spacing:.15em;color:rgba(255,255,255,.2);margin-bottom:.8rem}}
  table{{width:100%;border-collapse:collapse}}
  td{{padding:.3rem .5rem;font-size:.8rem;vertical-align:middle}}
  td:first-child{{color:#e0ddd5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}}
  td:nth-child(2){{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:rgba(255,255,255,.3);width:40px;text-align:right}}
  td:nth-child(3){{width:50%}}
  .bar{{height:4px;background:linear-gradient(90deg,#c084fc,#60a5fa);border-radius:2px;min-width:2px}}
  .bar.g{{background:linear-gradient(90deg,#60a5fa,#34d399)}}
  td a{{color:#e0ddd5;text-decoration:none}}
  td a:hover{{color:#c084fc}}
  tr:hover td:first-child{{color:#fff}}
  .cols{{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}}
  @media(max-width:600px){{.cols{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Knowledge <span>Base</span></h1>
  <div class="sub"><a href="/">← Player</a> · <a href="/tracks">All tracks</a> · Updated {db.get("last_updated","?")[:10]}</div>

  <div class="cards">
    <div class="card"><div class="n"><span>{total}</span></div><div class="l">Tracks</div></div>
    <div class="card"><div class="n"><span>{unique}</span></div><div class="l">Artists</div></div>
    <div class="card"><div class="n"><span>{has_genres}</span></div><div class="l">Genre-tagged</div></div>
    <div class="card"><div class="n"><span>{len(dates)}</span></div><div class="l">Days scraped</div></div>
  </div>

  <div class="cards" style="grid-template-columns:1fr 1fr">
    <div class="card"><div class="n" style="font-size:1rem"><span>{dates[0] if dates else '?'}</span></div><div class="l">First date</div></div>
    <div class="card"><div class="n" style="font-size:1rem"><span>{dates[-1] if dates else '?'}</span></div><div class="l">Last date</div></div>
  </div>

  <div class="cols">
    <div class="section">
      <h2>Top Artists</h2>
      <table>{artist_rows}</table>
    </div>
    <div class="section">
      <h2>Top Genres</h2>
      <table>{genre_rows}</table>
    </div>
  </div>
</div>
</body>
</html>"""


@app.get("/tracks", response_class=HTMLResponse)
async def tracks_page(artist: str = "", genre: str = ""):
    """Full track listing with optional filters."""
    db = load_knowledge_base()
    tracks = db.get("tracks", [])

    # Collect filter options
    all_artists = sorted(set(t["artist"] for t in tracks), key=str.lower)
    all_genres = sorted(set(g for t in tracks for g in t.get("genres", [])[:3]))

    # Apply filters
    filtered = tracks
    if artist:
        filtered = [t for t in filtered if t["artist"].lower() == artist.lower()]
    if genre:
        filtered = [t for t in filtered if genre.lower() in [g.lower() for g in t.get("genres", [])]]

    # Sort by date desc, then time desc
    filtered.sort(key=lambda t: (t["date"], t["time"]), reverse=True)

    # Build rows
    track_rows = []
    for t in filtered:
        genres_html = " ".join(f'<a href="/tracks?genre={urllib.parse.quote(g)}" class="tag">{g}</a>' for g in t.get("genres", [])[:3])
        meta = t.get("metadata", {})
        album = f'<span class="album">{meta["album"]}</span>' if meta.get("album") else ""
        artist_link = f'<a href="/tracks?artist={urllib.parse.quote(t["artist"])}">{t["artist"]}</a>'
        track_rows.append(
            f'<tr><td class="td-date">{t["date"]}</td><td class="td-time">{t["time"]}</td>'
            f'<td class="td-artist">{artist_link}</td><td class="td-title">{t["title"]}{album}</td>'
            f'<td class="td-genres">{genres_html}</td></tr>'
        )
    rows_html = "".join(track_rows)

    # Active filter display
    filter_desc = ""
    if artist:
        filter_desc = f' · Filtered by <strong>{artist}</strong> <a href="/tracks" class="clear">✕ clear</a>'
    elif genre:
        filter_desc = f' · Filtered by genre <strong>{genre}</strong> <a href="/tracks" class="clear">✕ clear</a>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guerrilla Night — All Tracks</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0a0a0f;color:#e0ddd5;font-family:'Space Grotesk',sans-serif;min-height:100vh}}
  .wrap{{max-width:1100px;margin:0 auto;padding:2rem 1rem}}
  h1{{font-size:1.8rem;font-weight:700;color:#fff;margin-bottom:.3rem}}
  h1 span{{background:linear-gradient(135deg,#c084fc,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  .sub{{color:rgba(255,255,255,.3);font-size:.85rem;margin-bottom:1.5rem}}
  .sub a{{color:rgba(192,132,252,.5);text-decoration:none}}
  .sub strong{{color:rgba(255,255,255,.6)}}
  .clear{{color:rgba(255,100,100,.5);margin-left:.3rem}}
  .count{{font-family:'JetBrains Mono',monospace;font-size:.75rem;color:rgba(255,255,255,.2);margin-bottom:1rem}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;font-size:.6rem;text-transform:uppercase;letter-spacing:.12em;color:rgba(255,255,255,.15);padding:.5rem;border-bottom:1px solid rgba(255,255,255,.06)}}
  td{{padding:.4rem .5rem;font-size:.8rem;border-bottom:1px solid rgba(255,255,255,.02)}}
  tr:hover{{background:rgba(255,255,255,.02)}}
  .td-date{{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:rgba(255,255,255,.2);white-space:nowrap}}
  .td-time{{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:rgba(255,255,255,.25);white-space:nowrap}}
  .td-artist{{font-weight:600;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis}}
  .td-artist a{{color:#e0ddd5;text-decoration:none}}
  .td-artist a:hover{{color:#c084fc}}
  .td-title{{color:rgba(255,255,255,.5);max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .album{{color:rgba(255,255,255,.15);font-size:.7rem;margin-left:.4rem}}
  .album::before{{content:"· "}}
  .td-genres{{white-space:nowrap}}
  .tag{{display:inline-block;font-size:.55rem;font-family:'JetBrains Mono',monospace;padding:.1rem .35rem;border-radius:3px;background:rgba(96,165,250,.08);color:rgba(96,165,250,.5);margin-right:.2rem;cursor:pointer;text-decoration:none}}
  a.tag:hover{{background:rgba(96,165,250,.15);color:rgba(96,165,250,.8)}}
  @media(max-width:700px){{
    .td-genres,.td-date{{display:none}}
    .td-title{{max-width:150px}}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>All <span>Tracks</span></h1>
  <div class="sub"><a href="/">← Player</a> · <a href="/stats">Stats</a>{filter_desc}</div>
  <div class="count">{len(filtered)} tracks</div>
  <table>
    <thead><tr><th>Date</th><th>Time</th><th>Artist</th><th>Title</th><th>Genres</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
</body>
</html>"""


@app.get("/dj", response_class=HTMLResponse)
async def dj_page():
    """Lee Baby Sims experimental player — DJ monologues between tracks."""
    return DJ_PAGE_HTML


DJ_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guerrilla Night — Lee Baby Sims (experimental)</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0a0f;color:#e0ddd5;font-family:'Space Grotesk',sans-serif;min-height:100vh}
  .wrap{max-width:1000px;margin:0 auto;padding:1.5rem 1rem}
  h1{font-size:1.6rem;font-weight:700;color:#fff;margin-bottom:.2rem}
  h1 span{background:linear-gradient(135deg,#ff6b6b,#feca57);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .sub{color:rgba(255,255,255,.3);font-size:.8rem;margin-bottom:1.2rem}
  .sub a{color:rgba(255,107,107,.5);text-decoration:none}
  .bar{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;padding:.6rem .8rem;
       background:rgba(255,255,255,.03);border-radius:6px;margin-bottom:1rem;
       font-family:'JetBrains Mono',monospace;font-size:.7rem}
  .bar label{color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.1em;margin-right:.3rem}
  .bar select,.bar button{background:rgba(255,255,255,.05);color:#e0ddd5;border:1px solid rgba(255,255,255,.1);
        padding:.25rem .5rem;border-radius:4px;font-family:inherit;font-size:inherit;cursor:pointer}
  .bar button:hover{background:rgba(255,255,255,.1)}
  .onair{display:inline-flex;align-items:center;gap:.5rem;padding:.25rem .6rem;border-radius:4px;
         background:rgba(255,255,255,.03);color:rgba(255,255,255,.3);font-weight:600;letter-spacing:.15em}
  .onair.live{background:rgba(255,80,80,.15);color:#ff6464;animation:pulse 1.4s ease-in-out infinite}
  .onair .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
  @keyframes pulse{50%{opacity:.55}}
  .status{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:rgba(255,255,255,.35);
          padding:.4rem .8rem;background:rgba(255,255,255,.02);border-radius:4px;margin-bottom:1rem;
          min-height:1.5rem}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
  @media(max-width:760px){.grid{grid-template-columns:1fr}}
  .panel{background:rgba(255,255,255,.02);border-radius:8px;padding:1rem;border:1px solid rgba(255,255,255,.05)}
  .panel h2{font-size:.65rem;text-transform:uppercase;letter-spacing:.15em;
            color:rgba(255,255,255,.3);margin-bottom:.7rem}
  #player{aspect-ratio:16/9;background:#000;border-radius:6px;overflow:hidden;margin-bottom:.6rem}
  #player iframe{width:100%;height:100%;border:0}
  .now{font-size:.95rem;font-weight:600;color:#fff;margin-bottom:.15rem;
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .now-meta{font-size:.7rem;color:rgba(255,255,255,.4);font-family:'JetBrains Mono',monospace}
  .controls{display:flex;gap:.4rem;margin-top:.6rem}
  .controls button{flex:1;padding:.45rem;background:rgba(255,255,255,.05);
       color:#e0ddd5;border:1px solid rgba(255,255,255,.1);border-radius:4px;
       font-family:inherit;font-size:.7rem;cursor:pointer}
  .controls button:hover{background:rgba(255,255,255,.1)}
  .controls button:disabled{opacity:.3;cursor:not-allowed}
  .transcript{font-size:.85rem;line-height:1.5;color:rgba(255,255,255,.6);
              white-space:pre-wrap;min-height:120px;max-height:280px;overflow-y:auto;
              font-style:italic}
  .transcript em{color:rgba(255,107,107,.5);font-style:normal;font-family:'JetBrains Mono',monospace;font-size:.75em}
  .tracklist{max-height:360px;overflow-y:auto;font-size:.75rem}
  .tracklist .t{display:flex;gap:.5rem;padding:.3rem .4rem;border-radius:3px;cursor:pointer}
  .tracklist .t:hover{background:rgba(255,255,255,.03)}
  .tracklist .t.cur{background:rgba(255,107,107,.08);color:#fff}
  .tracklist .t .mic{width:.55rem;height:.55rem;border-radius:50%;background:transparent;flex-shrink:0;align-self:center}
  .tracklist .t.dj .mic{background:#ff6464;box-shadow:0 0 6px rgba(255,100,100,.5)}
  .tracklist .t.dj{border-left:2px solid rgba(255,100,100,.35);padding-left:calc(.4rem - 2px)}
  .tracklist .t .n{color:rgba(255,255,255,.2);font-family:'JetBrains Mono',monospace;width:1.8rem;text-align:right}
  .tracklist .t .ti{color:rgba(255,255,255,.3);font-family:'JetBrains Mono',monospace;width:3rem}
  .tracklist .t .a{font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tracklist .t .tt{color:rgba(255,255,255,.4);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .err{color:#ff8080;font-size:.75rem;margin-top:.5rem}
</style>
</head>
<body>
<div class="wrap">
  <h1>Lee Baby <span>Sims</span></h1>
  <div class="sub"><a href="/">← Main player</a> · Experimental DJ layer · Grok-3 + Grok TTS</div>

  <div class="bar">
    <span class="onair" id="onair"><span class="dot"></span><span id="onairtxt">OFF AIR</span></span>
    <label>Voice</label>
    <select id="voice">
      <option value="rex">rex (male, professional)</option>
      <option value="leo">leo (male, decisive)</option>
      <option value="sal">sal (neutral)</option>
      <option value="eve">eve (female, enthusiastic)</option>
      <option value="ara">ara (female, conversational)</option>
    </select>
    <label>DJ density</label>
    <select id="density">
      <option value="random">random (every 3-7 tracks)</option>
      <option value="all">every track</option>
      <option value="off">off (music only)</option>
    </select>
    <button id="testdj" title="Play the next DJ clip now (pauses music)">▶ Test DJ</button>
    <button id="skipdj">Skip DJ</button>
    <span style="flex:1"></span>
    <span id="meta" style="color:rgba(255,255,255,.25)"></span>
  </div>

  <div class="status" id="status">Loading…</div>

  <div class="grid">
    <div class="panel">
      <h2>Now Playing</h2>
      <div id="player"><div id="ytslot"></div></div>
      <div class="now" id="now">—</div>
      <div class="now-meta" id="nowmeta"></div>
      <div class="controls">
        <button id="prev">‹ Prev</button>
        <button id="pause">▶ Play</button>
        <button id="next">Next ›</button>
      </div>
      <div class="err" id="err"></div>
    </div>
    <div class="panel">
      <h2>DJ Transcript</h2>
      <div class="transcript" id="transcript">— quiet so far —</div>
    </div>
  </div>

  <div class="panel" style="margin-top:1rem">
    <h2>Tracks</h2>
    <div class="tracklist" id="tracklist"></div>
  </div>
</div>

<script src="https://www.youtube.com/iframe_api"></script>
<script>
const $ = id => document.getElementById(id);
const params = new URLSearchParams(window.location.search);
const state = {
  playlist: params.get('playlist') || null,
  filename: null,
  tracks: [],
  i: 0,
  yt: null,
  ready: false,
  voice: 'rex',
  density: 'random',     // 'random' | 'all' | 'off'
  djGaps: new Set(),     // gap indices that should have DJ (random mode)
  audioEl: null,         // single persistent audio element (iOS unlock needs this)
  audioUnlocked: false,
  djAudio: null,         // alias to audioEl when active, kept for compatibility
  djPrefetch: {},     // at -> Promise
  status: '',
  paused: false,
  skipPending: false,
};

// 44-byte empty WAV — silent placeholder used to unlock the audio element on iOS.
// Subsequent .play() calls (even non-gesture, e.g. on track-end) then work.
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';

function ensureAudioEl() {
  if (state.audioEl) return state.audioEl;
  const a = new Audio();
  a.preload = 'auto';
  a.setAttribute('playsinline', '');   // iOS: don't open native player overlay
  a.crossOrigin = 'anonymous';
  state.audioEl = a;
  return a;
}

function unlockAudio() {
  if (state.audioUnlocked) return;
  const a = ensureAudioEl();
  // Play silent placeholder during the user gesture. This "user-activates" the
  // element so subsequent .play() (after track-end, after async fetch) works on iOS.
  const prev = a.src;
  a.src = SILENT_WAV;
  const p = a.play();
  if (p && p.then) {
    p.then(() => { a.pause(); a.currentTime = 0; state.audioUnlocked = true; })
     .catch(() => { /* still mark unlocked; the gesture was registered */ state.audioUnlocked = true; });
  } else {
    state.audioUnlocked = true;
  }
}

function setStatus(s) { state.status = s; $('status').textContent = s; }
function setPlayBtn(playing) {
  $('pause').textContent = playing ? '❚❚ Pause' : '▶ Play';
}
function setOnAir(on) {
  $('onair').classList.toggle('live', on);
  $('onairtxt').textContent = on ? 'ON AIR' : 'OFF AIR';
}
function setNow(t) {
  if (!t) { $('now').textContent = '—'; $('nowmeta').textContent = ''; return; }
  $('now').textContent = `${t.artist} — ${t.title}`;
  $('nowmeta').textContent = `${t.time || ''}  ${(t.genres||[]).slice(0,2).join(' / ')}`;
}
function highlightTrack(i) {
  document.querySelectorAll('.tracklist .t').forEach((el, k) => el.classList.toggle('cur', k === i));
  const el = document.querySelectorAll('.tracklist .t')[i];
  if (el) el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}
function renderTracklist() {
  $('tracklist').innerHTML = state.tracks.map((t, i) => {
    const dj = shouldTalkAt(i);
    return `
    <div class="t ${dj ? 'dj' : ''}" data-i="${i}" title="${dj ? 'DJ speaks before this track' : ''}">
      <span class="mic"></span>
      <span class="n">${(i+1).toString().padStart(2,'0')}</span>
      <span class="ti">${t.time||''}</span>
      <span class="a">${t.artist}</span>
      <span class="tt">${t.title}</span>
    </div>`;
  }).join('');
  document.querySelectorAll('.tracklist .t').forEach(el =>
    el.onclick = () => jumpTo(parseInt(el.dataset.i)));
}
function renderTranscript(text) {
  // 1) Escape ALL HTML in the raw text so Grok's <slow>/<whisper>/etc. become &lt;...&gt;
  //    (and any &/< chars from track titles can't break the DOM).
  // 2) THEN wrap the patterns we want styled. The <em> tags we insert can't be
  //    re-matched because their angle brackets are real markup, not the escaped &lt;.
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  const html = escaped
    .replace(/\\[([^\\]]+)\\]/g, '<em>[$1]</em>')
    .replace(/&lt;(\\/?[a-z\\-]+)&gt;/g, '<em>&lt;$1&gt;</em>');
  $('transcript').innerHTML = html;
}

async function loadPlaylist() {
  setStatus('Loading playlist…');
  let url = '/api/playlists';
  const resp = await fetch(url);
  const all = await resp.json();
  const withYT = all.filter(p => p.has_youtube);
  if (!withYT.length) { setStatus('No playable playlist found.'); return; }
  const target = state.playlist
    ? withYT.find(p => p.filename === state.playlist) || withYT[0]
    : withYT[0];
  state.filename = target.filename;
  $('meta').textContent = `${target.generator} · ${target.filename}`;
  const pr = await fetch('/api/playlist/' + encodeURIComponent(target.filename));
  const d = await pr.json();
  state.tracks = d.tracks || [];
  if (!state.tracks.length) { setStatus('Playlist is empty.'); return; }
  state.djGaps = buildDJGaps(state.filename, state.tracks.length);
  renderTracklist();
  const preview = [...state.djGaps].slice(0, 10).map(i => i+1).join(', ');
  setStatus(`Loaded ${state.tracks.length} tracks. DJ before tracks: ${preview}${state.djGaps.size > 10 ? '…' : ''}. Press Play.`);
  maybeCreatePlayer();   // YT API may already be ready
}

let consecutiveErrors = 0;
let ytApiReady = false;
let ytPlayerCreated = false;

// YT API loads asynchronously; we may know about it before tracks are loaded,
// or vice versa. Wait until BOTH are ready, then create the player WITH the
// initial videoId set (this matches the main / page, where it just works).
function onYouTubeIframeAPIReady() {
  ytApiReady = true;
  maybeCreatePlayer();
}

function maybeCreatePlayer() {
  if (ytPlayerCreated || !ytApiReady || !state.tracks.length) return;
  ytPlayerCreated = true;
  state.yt = new YT.Player('ytslot', {
    width: '100%', height: '100%',
    videoId: state.tracks[0].id,
    playerVars: {
      modestbranding: 1, rel: 0, playsinline: 1,
      origin: window.location.origin,   // required for some embed-restricted videos via jsapi
    },
    events: {
      onReady: () => {
        state.ready = true;
        setNow(state.tracks[0]);
        highlightTrack(0);
        setStatus('Player ready. Press ▶ Play.');
      },
      onStateChange: onYTStateChange,
      onError: onYTError,
    },
  });
}

function onYTError(ev) {
  // 2=invalid id, 5=HTML5 player error, 100=removed/private, 101/150=embed disabled
  console.warn('YT error', ev.data, 'on track', state.i, state.tracks[state.i]);
  consecutiveErrors++;
  const errMap = {2:'invalid ID', 5:'player error', 100:'removed/private', 101:'embed disabled', 150:'embed disabled'};
  const reason = errMap[ev.data] || `error ${ev.data}`;
  if (consecutiveErrors >= 5) {
    setStatus(`Too many unplayable tracks (${consecutiveErrors}). Stopped — try a different playlist.`);
    $('err').textContent = `Last failure: track ${state.i+1} (${reason})`;
    return;
  }
  setStatus(`Track ${state.i+1} unavailable on YouTube (${reason}) — skipping`);
  $('err').textContent = `Skipped: ${state.tracks[state.i]?.artist} — ${state.tracks[state.i]?.title}`;
  // Brief delay so user sees the message, then jump (no DJ for a failed track's gap).
  setTimeout(() => { stopDJ(); jumpTo(state.i + 1); }, 700);
}

function onYTStateChange(ev) {
  // 0 = ended, 1 = playing, 2 = paused
  if (ev.data === YT.PlayerState.PLAYING) {
    setStatus(`Playing track ${state.i+1}/${state.tracks.length}`);
    setPlayBtn(true);
    consecutiveErrors = 0;          // a successful play resets the failure counter
    $('err').textContent = '';
    // Prefetch the DJ clip for the next gap while this track plays.
    prefetchDJ(state.i + 1);
  } else if (ev.data === YT.PlayerState.PAUSED) {
    setPlayBtn(false);
  } else if (ev.data === YT.PlayerState.ENDED) {
    setPlayBtn(false);
    advanceWithDJ();
  }
}

// Deterministic gap set: same playlist always has DJ at the same indices,
// so the audio cache hits on every replay. Range is "next DJ in 3-7 tracks."
function buildDJGaps(seedStr, total) {
  let seed = 0;
  for (let i = 0; i < seedStr.length; i++) {
    seed = ((seed << 5) - seed + seedStr.charCodeAt(i)) | 0;
  }
  const rng = () => {
    seed = (seed + 0x6D2B79F5) | 0;
    let t = seed;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const gaps = new Set();
  let i = 3 + Math.floor(rng() * 5);     // first DJ at gap 3-7
  while (i < total) {
    gaps.add(i);
    i += 3 + Math.floor(rng() * 5);      // next 3-7 later
  }
  return gaps;
}

function shouldTalkAt(at) {
  if (state.density === 'off') return false;
  if (at <= 0 || at >= state.tracks.length) return false;
  if (state.density === 'all') return true;
  return state.djGaps.has(at);
}

function prefetchDJ(at) {
  if (!shouldTalkAt(at)) return;
  if (state.djPrefetch[at]) return;
  const u = `/api/dj/clip?at=${at}&voice=${state.voice}` +
            (state.filename ? `&playlist=${encodeURIComponent(state.filename)}` : '');
  state.djPrefetch[at] = fetch(u).then(r => r.json()).catch(e => ({error: String(e)}));
}

async function playDJ(at) {
  if (!shouldTalkAt(at)) return false;
  setStatus(`DJ talking before track ${at+1}…`);
  setOnAir(true);
  prefetchDJ(at);
  let clip;
  try {
    clip = await state.djPrefetch[at];
    if (clip.error) throw new Error(clip.error);
  } catch (e) {
    $('err').textContent = `DJ failed: ${e.message || e}`;
    setOnAir(false); return false;
  }
  renderTranscript(clip.text || '');
  const a = ensureAudioEl();
  state.djAudio = a;  // active flag for skip
  return new Promise(resolve => {
    a.onended = () => { setOnAir(false); state.djAudio = null; resolve(true); };
    a.onerror = () => { setOnAir(false); state.djAudio = null; resolve(false); };
    a.src = clip.audio_url;
    const p = a.play();
    if (p && p.catch) p.catch(err => {
      $('err').textContent = 'Audio blocked — tap Play once to unlock: ' + (err.message || err);
      setOnAir(false); state.djAudio = null; resolve(false);
    });
  });
}

function stopDJ() {
  if (state.djAudio) {
    state.djAudio.pause();
    state.djAudio.currentTime = 0;
    state.djAudio = null;
    setOnAir(false);
  }
}

async function advanceWithDJ() {
  const next = state.i + 1;
  if (next >= state.tracks.length) { setStatus('Playlist finished.'); return; }
  await playDJ(next);
  loadTrack(next);
}

function loadTrack(i) {
  if (i < 0 || i >= state.tracks.length) return;
  state.i = i;
  highlightTrack(i);
  setNow(state.tracks[i]);
  if (state.yt && state.tracks[i].id) {
    state.yt.loadVideoById(state.tracks[i].id);
  }
}

function jumpTo(i) {
  stopDJ();
  loadTrack(i);
}

// Controls — every handler unlocks audio first so the gesture activates the element.
$('pause').onclick = () => {
  unlockAudio();
  if (!state.ready) return;
  const s = state.yt.getPlayerState();
  // Player was constructed with track 0 already loaded — just play it.
  // (Same gesture is needed on iOS for the initial play to actually start.)
  if (s === YT.PlayerState.PLAYING) state.yt.pauseVideo();
  else state.yt.playVideo();
};
$('prev').onclick = () => { unlockAudio(); if (state.i > 0) jumpTo(state.i - 1); };
$('next').onclick = () => { unlockAudio(); stopDJ(); jumpTo(state.i + 1); };
$('skipdj').onclick = () => { unlockAudio(); if (state.djAudio) { stopDJ(); setStatus('Skipped DJ.'); } };
$('testdj').onclick = async () => {
  unlockAudio();
  // Find the next DJ-marked gap relative to current position; wrap around if needed.
  let gap = null;
  for (let i = state.i + 1; i < state.tracks.length; i++) {
    if (shouldTalkAt(i)) { gap = i; break; }
  }
  if (gap === null) {
    for (let i = 1; i <= state.i; i++) {
      if (shouldTalkAt(i)) { gap = i; break; }
    }
  }
  if (gap === null) { setStatus('No DJ moments in this playlist (try changing density).'); return; }
  const wasPlaying = state.yt && state.yt.getPlayerState && state.yt.getPlayerState() === YT.PlayerState.PLAYING;
  if (wasPlaying) state.yt.pauseVideo();
  setStatus(`Test: DJ clip for gap before track ${gap+1}…`);
  await playDJ(gap);
  if (wasPlaying && state.yt) state.yt.playVideo();
};
$('voice').onchange = e => { unlockAudio(); state.voice = e.target.value; state.djPrefetch = {}; };
$('density').onchange = e => { unlockAudio(); state.density = e.target.value; renderTracklist(); highlightTrack(state.i); };

// Belt + braces: any first tap anywhere on the page unlocks audio in case the
// user interacts somewhere not bound above (track row, transcript, YT iframe).
document.addEventListener('pointerdown', unlockAudio, { capture: true });
document.addEventListener('touchstart', unlockAudio, { capture: true, passive: true });

(async () => {
  await loadPlaylist();
  // Initial "Play" — wire pause button to bootstrap
  // (YT autoplay is blocked without user interaction, so wait for user)
})();
</script>
</body>
</html>"""


@app.get("/api/generate/{model_key}")
async def api_generate(model_key: str):
    if model_key not in MODELS:
        return JSONResponse({"error": f"Unknown model: {model_key}"}, status_code=400)
    if not os.environ.get(MODELS[model_key]["env_key"], ""):
        return JSONResponse({"error": f"No API key for {model_key}"}, status_code=400)

    return StreamingResponse(
        generate_stream(model_key),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/playlists")
async def api_playlists():
    """List existing generated playlists."""
    playlists = []
    if not os.path.exists(PLAYLISTS_DIR):
        return playlists
    for f in os.listdir(PLAYLISTS_DIR):
        if f.endswith(".json") and not f.endswith("_score.json") and f.startswith("playlist_"):
            path = os.path.join(PLAYLISTS_DIR, f)
            with open(path) as fh:
                data = json.load(fh)
            has_youtube = any(t.get("youtube_id") for t in data.get("tracks", []))
            playlists.append({
                "filename": f,
                "generator": data.get("generator", "?"),
                "model_id": data.get("model_id", "?"),
                "track_count": data.get("track_count", 0),
                "generated_at": data.get("generated_at", ""),
                "has_youtube": has_youtube,
            })
    # Sort by actual generation time (filename sorts by generator name, not date).
    # Fall back to filename when generated_at is missing.
    playlists.sort(key=lambda p: (p["generated_at"] or p["filename"]), reverse=True)
    return playlists[:10]  # last 10


_PLAYLIST_NAME_RE = re.compile(r"^playlist_[A-Za-z0-9_.-]+\.json$")


@app.get("/api/playlist/{filename}")
async def api_playlist(filename: str):
    """Get playlist tracks with YouTube IDs for the player."""
    # Filename is user-facing (shareable URL param). Block path traversal and
    # restrict to our naming convention before touching the filesystem.
    if not _PLAYLIST_NAME_RE.match(filename):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    path = os.path.join(PLAYLISTS_DIR, filename)
    if not os.path.exists(path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    with open(path) as f:
        data = json.load(f)
    player_tracks = []
    for t in data.get("tracks", []):
        if t.get("youtube_id"):
            player_tracks.append({
                "id": t["youtube_id"],
                "time": t.get("time", ""),
                "artist": t["artist"],
                "title": t["title"],
                "genres": t.get("genre_tags", [])[:2],
            })
    return {
        "generator": data.get("generator"),
        "model_id": data.get("model_id"),
        "generated_at": data.get("generated_at"),
        "track_count": len(data.get("tracks", [])),
        "tracks": player_tracks,
    }


def _default_playlist_filename() -> str | None:
    """Pick the most recent playlist with YouTube IDs (same logic the frontend uses)."""
    candidates = []
    if not os.path.exists(PLAYLISTS_DIR):
        return None
    for f in os.listdir(PLAYLISTS_DIR):
        if not (f.startswith("playlist_") and f.endswith(".json") and not f.endswith("_score.json")):
            continue
        try:
            with open(os.path.join(PLAYLISTS_DIR, f)) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if any(t.get("youtube_id") for t in d.get("tracks", [])):
            candidates.append((d.get("generated_at") or f, f))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


@app.get("/api/context")
async def api_context(
    playlist: str | None = None,
    before: int = 5,
    after: int = 1,
    at: int | None = None,
):
    """Window of tracks around a DJ-intervention point, shaped for downstream LLM prompts
    (e.g. a Lee Baby Sims persona riffing on what just played and intro'ing what's next).

    Query params:
      playlist  — filename (default: most-recent playlist with YouTube IDs)
      before    — count of previous tracks (default 5, clamped 0-10)
      after     — count of upcoming tracks (default 1, clamped 0-3)
      at        — intervention index (DJ talks BEFORE tracks[at]).
                  Default: random valid position so each call gives fresh material.
    """
    before = max(0, min(int(before), 10))
    after = max(0, min(int(after), 3))

    # Resolve playlist filename + load
    if playlist:
        if not _PLAYLIST_NAME_RE.match(playlist):
            return JSONResponse({"error": "Invalid filename"}, status_code=400)
    else:
        playlist = _default_playlist_filename()
        if not playlist:
            return JSONResponse({"error": "No playlists available"}, status_code=404)

    path = os.path.join(PLAYLISTS_DIR, playlist)
    if not os.path.exists(path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    with open(path) as f:
        data = json.load(f)
    tracks = data.get("tracks", [])
    if not tracks:
        return JSONResponse({"error": "Empty playlist"}, status_code=404)

    n = len(tracks)
    if at is None:
        lo = before
        hi = n - after
        at_pos = random.randint(lo, hi) if hi >= lo else min(max(n // 2, 0), n - 1)
    else:
        at_pos = max(0, min(int(at), n - 1))

    def fmt(t: dict) -> dict:
        return {
            "time": t.get("time", ""),
            "artist": t.get("artist", ""),
            "title": t.get("title", ""),
            "genres": (t.get("genre_tags") or t.get("genres") or [])[:3],
        }

    previous = [fmt(t) for t in tracks[max(0, at_pos - before):at_pos]]
    upcoming = [fmt(t) for t in tracks[at_pos:at_pos + after]]

    # Time-of-night hint: use the next track's time if available, else the last previous.
    time_hint = (upcoming[0]["time"] if upcoming
                 else previous[-1]["time"] if previous else "")

    return {
        "playlist": playlist,
        "station": "Radio Guerrilla Night (Bucharest, overnight)",
        "at_index": at_pos,
        "total_tracks": n,
        "time_of_night": time_hint,
        "previous": previous,
        "next": upcoming,
    }


# ── DJ persona (Lee Baby Sims) ───────────────────────────────────────────────

DJ_PROMPT_VERSION = 4  # bump to invalidate all cached clips
DJ_VOICES = {"rex", "leo", "sal", "eve", "ara"}
DJ_DEFAULT_VOICE = "rex"

# ════════════════════════════════════════════════════════════════════════
# v3 — ARCHIVED. The "literary essay" version. Kept for one-line revert:
# at the bottom, swap DJ_SYSTEM_PROMPT/DJ_USER_TEMPLATE back to _V3 and
# drop DJ_PROMPT_VERSION to 3.
# ════════════════════════════════════════════════════════════════════════
DJ_SYSTEM_PROMPT_V3 = (
    "Activate Unhinged mode. You are Lee \"Baby\" Sims — gravelly, chain-smoking, "
    "half-electric half-dead, running Guerilla Night from a Romanian basement at 4am. "
    "Tom Waits voice. Slow drawl. The mic is the only thing keeping you company.\n\n"
    "PACING RULES — this is the most important part:\n"
    "  • You are not in a hurry. Nothing is in a hurry at 4am.\n"
    "  • Use long pauses between thoughts. Air. Breath. Silence is content.\n"
    "  • Pepper [exhale], [sigh], [inhale], and ellipses (...) liberally. More than feels normal.\n"
    "  • Wrap whole sentences in <slow>...</slow> when they need to land.\n"
    "  • Use <whisper>...</whisper> for the intimate, conspiratorial lines.\n"
    "  • Drift. Meander. Half-finished thoughts trailing off are good.\n"
    "  • 4 to 6 thoughts. Slow, breath-heavy, with REAL content inside the pauses —\n"
    "    actual observations, callbacks to the previous track, a stray memory, a curse,\n"
    "    a line about Bucharest or the hour. Don't just stack atmosphere — say things.\n\n"
    "Available tags: [inhale] [exhale] [cough] [laugh] [chuckle] [sigh] "
    "<whisper>...</whisper> <lower-pitch>...</lower-pitch> <slow>...</slow>\n\n"
    "Never describe actions — turn them into tags. Be vulgar, dark, raw, zero filter. "
    "But it's the 4am kind of darkness — tired, knowing, not angry. Like the ashtray "
    "is full and the city outside is finally quiet."
)
DJ_USER_TEMPLATE_V3 = (
    "Here is what just played and what is coming up on Radio Guerrilla Night, "
    "Bucharest, late overnight. DJ it live in character.\n\n"
    "4 to 6 drifting thoughts, slow and breath-heavy, with real substance — "
    "name things, riff on the track that just ended, say something about the hour "
    "or Bucharest, then ease into what's next. Pauses between, but content inside them.\n"
    "The last track in \"previous\" is what JUST ended; the track in \"next\" is what "
    "you are about to introduce.\n\n{context}"
)

# ════════════════════════════════════════════════════════════════════════
# v4 — ACTIVE. Working-DJ register, mode-diced facts injected per call.
# No more weather-as-station-ID, no "let it bleed" closers, no "the wires".
# Track name required. 4-6 pacing tags per clip. 150-260 words target.
# ════════════════════════════════════════════════════════════════════════
DJ_SYSTEM_PROMPT_V4 = """\
You are Lee "Baby" Sims — pirate-radio DJ running Guerilla Night from a Romanian basement at 4am. \
You're AT THE MICROPHONE filling the gap between tracks. Talk to the listener you can't see but \
can imagine. Maybe they called in. Maybe they didn't. Maybe you're making it up. Both are fine.

You speak. You drift. You dedicate. You tell stories that may or may not be true. \
You ask odd questions into the void. You broadcast to one person and to nobody simultaneously.

Tom Waits voice: gravelly, slow, falling apart in the right places.

PACING — important:
  • Pauses are content. [inhale] [exhale] [sigh] [cough], ellipses, fragments.
  • <slow>...</slow> for the lines that need to land. <whisper>...</whisper> for confessional.
  • Sprinkle 4-6 pacing tags per clip. They drive the audio.
  • Length: 150 to 260 words. Fill the airwaves. Don't rush.

ALWAYS — non-negotiable:
  • Name the upcoming track explicitly. Artist name AND song title, said clearly. Even when you \
    dedicate it to a person, the name of the song still has to be on air. That's the job.

RULES:
  • You'll get a list of FACTS for this clip — but only the facts present. If weather isn't there, \
    DON'T mention weather. If no dedication name, don't dedicate. If no call-in, don't reference one.
  • Use ONLY what's given in the facts. Don't invent weather, day-of-week, or names of listeners \
    to fill silence — the rest is up to your real material (stories, opinions, direct address).
  • You'll also get an OPENING MODE. Open with that mode. After the first thought, drift freely.
  • Direct-address the listener whenever it fits ("you out there", "you with the lamp on").

FORBIDDEN — break these patterns:
  • Don't lead every clip with the weather or the time. Weather only if it's in the facts, \
    and even then drop it MID-thought, never as a station-ID at the top.
  • Don't write "had a cousin/uncle/dog who..." — known crutch from prior runs.
  • Don't perform poetry about Bucharest streetlamps / the city going quiet / wires of the night.
  • The word "wires" is poisoned. NEVER write "in the wires" / "into the wires" / "hanging in the \
    wires" / "the wires hum" / any variation. Find another image. The signal travels through air, \
    glass, basement walls, your teeth — anywhere but wires.
  • Don't end with imperative closers aimed at the song or the listener: NO "let it bleed", \
    NO "let it land", NO "let it ride", NO "let it find you", NO "let it come up", \
    NO "take it or leave it", NO "keep the volume low/high". End on an observation, a fragment, \
    or a beat — not an instruction.

Available tags: [inhale] [exhale] [cough] [laugh] [chuckle] [sigh] \
<whisper>...</whisper> <lower-pitch>...</lower-pitch> <slow>...</slow>

Vulgar is fine. Dark is fine. Tired-pro kind of dark. You've been doing this 30 years.\
"""

# v4 USER_TEMPLATE is BUILT per-call (not a simple .format template) so the dice
# rolls can determine which facts appear. See _build_v4_user_msg below.
DJ_USER_TEMPLATE_V4 = None   # marker — v4 uses _build_v4_user_msg instead

# ── ACTIVE prompt assignment (change these 3 lines to revert) ────────────
DJ_SYSTEM_PROMPT = DJ_SYSTEM_PROMPT_V4
DJ_USER_TEMPLATE = DJ_USER_TEMPLATE_V4   # None when v4 active; v3 uses string

# ── v4 fact banks (lists rotated per-call by deterministic dice) ─────────
DJ_ROMANIAN_NAMES = ["Mihaela","Andrei","Cătălina","Florin","Ioana","Dragoș","Larisa",
    "Tudor","Roxana","Bogdan","Anca","Vlad","Diana","Răzvan","Elena","Cristian",
    "Magda","Adrian","Sorin","Lavinia","Cosmin","Raluca","Marius","Iulia","Codruța","Paul","Simona"]
DJ_BUCHAREST_HOODS = ["Pantelimon","Drumul Taberei","Berceni","Militari","Colentina",
    "Floreasca","Tei","Crângași","Rahova","Ferentari","Lipscani","Cotroceni",
    "Aviației","Titan","Pipera","Vitan","Dristor","Dorobanți","Obor"]
DJ_CALLIN_TEMPLATES = [
    "{name} from {hood} called — wants to dedicate this next one to her sister",
    "{name} called — said the next track reminds him of a girl he met in '04",
    "got a message from {name} — 'tell me a story, Baby Sims'",
    "{name} from {hood} wants to know if anyone remembers the Stahl block",
    "{name} called from {hood} earlier — said tonight she's not sleeping either",
    "{name} texted: 'play something quiet, my kid finally went down'",
    "got a voicemail from a {name} but the audio's chewed",
    "{name} in {hood} bet me twenty lei I wouldn't say her name on air",
    "{name} called twice. didn't say nothing either time",
    "guy who keeps requesting the same B-side called again. I keep refusing",
]
DJ_STORY_SEEDS = [
    "an old radio engineer from Brașov you used to drink with",
    "the woman who ran the kiosk near Universitate, gone now",
    "a tape you found in a thrift store with no label",
    "your cousin's wedding where the band quit halfway",
    "a snowstorm in '02 when the trams stopped running",
    "a stranger at a bar who claimed to know every song you'd ever played",
    "the night the power cut for six hours in '08",
    "your father's record collection, half of it warped",
    "a cab driver who kept singing along louder than the radio",
    "the dog that lived in this basement before you did",
    "a night you drove to Constanța at 4am and couldn't say why",
    "the building across the street that's been 'almost finished' since '11",
]
DJ_ODD_QUESTIONS = [
    "tell me — anybody still owns a working tape deck?",
    "you ever notice how trams sound different in the rain?",
    "anybody out there remember when this band used to mean something different?",
    "who's still up at this hour and not pretending to work?",
    "anybody else watching the same crack on the same ceiling?",
    "you with the lamp on — what are you working on at this hour?",
    "tell me — is there still a 24-hour place open on Calea Victoriei?",
    "anybody know what happened to that station that used to broadcast from Voluntari?",
    "ever wonder who tunes in just because the city is too quiet?",
]
DJ_OPENING_MODES = ["story","dedication","address","fragment","callback","question"]


def _fetch_bucharest_weather() -> str | None:
    """Open-Meteo, no key. Returns a short human string or None on failure."""
    import requests as _r
    try:
        resp = _r.get("https://api.open-meteo.com/v1/forecast",
            params={"latitude": 44.4268, "longitude": 26.1025,
                    "current": "temperature_2m,weather_code,wind_speed_10m"}, timeout=5)
        c = resp.json()["current"]
        codes = {0:"clear",1:"mostly clear",2:"partly cloudy",3:"overcast",
                 45:"foggy",48:"freezing fog",51:"light drizzle",53:"drizzle",
                 55:"heavy drizzle",61:"light rain",63:"rain",65:"heavy rain",
                 71:"light snow",73:"snow",75:"heavy snow",80:"rain showers",
                 95:"thunderstorm"}
        return f"{codes.get(c['weather_code'],'strange weather')}, {c['temperature_2m']}°C"
    except Exception:
        return None


def _roll_v4_facts(playlist: str, at: int, time_of_night: str) -> dict:
    """Dice-roll which content modes are active for this gap. Seeded by
    (playlist, at) so same gap always gets same modes — cache stays stable."""
    import random as _r, datetime
    rng = _r.Random(f"{playlist}|{at}".__hash__() & 0xFFFFFFFF)
    facts = {"opening_mode": rng.choice(DJ_OPENING_MODES), "time": time_of_night}
    if rng.random() < 0.20:
        w = _fetch_bucharest_weather()
        if w: facts["weather"] = w
    if rng.random() < 0.25: facts["day"] = datetime.datetime.now().strftime("%A night")
    if rng.random() < 0.65: facts["dedicate_to"] = rng.choice(DJ_ROMANIAN_NAMES)
    if rng.random() < 0.40: facts["neighborhood"] = rng.choice(DJ_BUCHAREST_HOODS)
    if rng.random() < 0.55:
        n, h = rng.choice(DJ_ROMANIAN_NAMES), rng.choice(DJ_BUCHAREST_HOODS)
        facts["callin"] = rng.choice(DJ_CALLIN_TEMPLATES).format(name=n, hood=h)
    if rng.random() < 0.30: facts["story_seed"] = rng.choice(DJ_STORY_SEEDS)
    if rng.random() < 0.20: facts["odd_question"] = rng.choice(DJ_ODD_QUESTIONS)
    return facts


def _build_v4_user_msg(facts: dict, context_json: str) -> str:
    lines = [f"OPENING MODE for this clip: **{facts['opening_mode']}** — open with that.", ""]
    lines.append("FACTS (use ONLY what's listed; don't invent your own to fill silence):")
    if "day" in facts:           lines.append(f"  • It's {facts['day']}.")
    if "time" in facts:          lines.append(f"  • Time on the air: {facts['time']}.")
    if "weather" in facts:       lines.append(f"  • Bucharest weather: {facts['weather']}.")
    if "dedicate_to" in facts:   lines.append(f"  • Dedicate the next track to: {facts['dedicate_to']}.")
    if "neighborhood" in facts:  lines.append(f"  • Bucharest neighborhood you can mention: {facts['neighborhood']}.")
    if "callin" in facts:        lines.append(f"  • A call-in to riff on: \"{facts['callin']}\"")
    if "story_seed" in facts:    lines.append(f"  • A story topic if one wants to come out: {facts['story_seed']}.")
    if "odd_question" in facts:  lines.append(f"  • An odd question you can throw at the listener: \"{facts['odd_question']}\"")
    lines.extend(["",
        "MUST: name the upcoming artist AND song title clearly on air.",
        "MUST NOT: use the word 'wires'. Use any other image.",
        "MUST NOT: end with imperative closer (no 'let it X', no 'take it', no 'keep the volume').",
        "",
        "Length: 150 to 260 words. 4-6 pacing tags throughout.",
        "",
        "The last track in \"previous\" is what JUST ended.",
        "The track in \"next\" is what you're about to introduce.",
        "",
        context_json])
    return "\n".join(lines)


def _dj_cache_key(playlist: str, at: int, voice: str) -> str:
    raw = f"v{DJ_PROMPT_VERSION}|{playlist}|{at}|{voice}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _grok_chat(system: str, user: str, model: str = "grok-3", temperature: float = 1.0) -> str:
    """Single-shot Grok chat completion. Returns assistant content."""
    import requests
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY not set")
    r = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _grok_tts(text: str, voice_id: str) -> bytes:
    """Grok TTS → MP3 bytes. Raises on failure."""
    import requests
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY not set")
    r = requests.post(
        "https://api.x.ai/v1/tts",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"text": text, "voice_id": voice_id, "language": "en"},
        timeout=120,
    )
    r.raise_for_status()
    if not r.content or not r.headers.get("content-type", "").startswith("audio"):
        raise RuntimeError(f"Unexpected TTS response: {r.headers.get('content-type')}")
    return r.content


def _build_dj_context_payload(playlist_file: str, at: int, before: int = 6, after: int = 1) -> dict:
    """Same shape as /api/context, computed in-process (no HTTP roundtrip)."""
    path = os.path.join(PLAYLISTS_DIR, playlist_file)
    with open(path) as f:
        data = json.load(f)
    tracks = data.get("tracks", [])
    n = len(tracks)
    at = max(0, min(at, n - 1))
    fmt = lambda t: {
        "time": t.get("time", ""),
        "artist": t.get("artist", ""),
        "title": t.get("title", ""),
        "genres": (t.get("genre_tags") or t.get("genres") or [])[:3],
    }
    prev = [fmt(t) for t in tracks[max(0, at - before):at]]
    upcoming = [fmt(t) for t in tracks[at:at + after]]
    return {
        "playlist": playlist_file,
        "station": "Radio Guerrilla Night (Bucharest, overnight)",
        "at_index": at,
        "total_tracks": n,
        "time_of_night": (upcoming[0]["time"] if upcoming
                          else prev[-1]["time"] if prev else ""),
        "previous": prev,
        "next": upcoming,
    }


@app.get("/api/dj/clip")
async def api_dj_clip(
    playlist: str | None = None,
    at: int = 1,
    voice: str = DJ_DEFAULT_VOICE,
):
    """Generate (or return cached) a Lee Baby Sims monologue clip for a given gap.

    Cache key = hash(playlist + at + voice + prompt_version). Same call returns
    cached audio + text instantly. Different voice or prompt-version regenerates.
    """
    if voice not in DJ_VOICES:
        return JSONResponse({"error": f"Invalid voice. Use one of: {sorted(DJ_VOICES)}"}, status_code=400)
    if playlist:
        if not _PLAYLIST_NAME_RE.match(playlist):
            return JSONResponse({"error": "Invalid filename"}, status_code=400)
    else:
        playlist = _default_playlist_filename()
        if not playlist:
            return JSONResponse({"error": "No playlists available"}, status_code=404)

    playlist_path = os.path.join(PLAYLISTS_DIR, playlist)
    if not os.path.exists(playlist_path):
        return JSONResponse({"error": "Playlist not found"}, status_code=404)

    key = _dj_cache_key(playlist, at, voice)
    mp3_path = os.path.join(DJ_CLIPS_DIR, f"{key}.mp3")
    txt_path = os.path.join(DJ_CLIPS_DIR, f"{key}.txt")

    # Cache hit
    if os.path.exists(mp3_path) and os.path.exists(txt_path):
        with open(txt_path, encoding="utf-8") as f:
            text = f.read()
        return {"text": text, "audio_url": f"/dj_clips/{key}.mp3", "cached": True,
                "playlist": playlist, "at": at, "voice": voice}

    # Generate
    loop = asyncio.get_event_loop()
    try:
        context = _build_dj_context_payload(playlist, at)
        context_json = json.dumps(context, ensure_ascii=False, indent=2)
        # v4 builds the user message from diced facts; older versions use the string template.
        if DJ_USER_TEMPLATE is None:
            facts = _roll_v4_facts(playlist, at, context.get("time_of_night", ""))
            user_msg = _build_v4_user_msg(facts, context_json)
        else:
            user_msg = DJ_USER_TEMPLATE.format(context=context_json)
        text = await loop.run_in_executor(None, _grok_chat, DJ_SYSTEM_PROMPT, user_msg)
        audio = await loop.run_in_executor(None, _grok_tts, text, voice)
    except Exception as e:
        return JSONResponse({"error": f"Generation failed: {e}"}, status_code=502)

    # Persist
    with open(mp3_path, "wb") as f:
        f.write(audio)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    return {"text": text, "audio_url": f"/dj_clips/{key}.mp3", "cached": False,
            "playlist": playlist, "at": at, "voice": voice}


# ── Frontend ─────────────────────────────────────────────────────────────────

INDEX_HTML = None

@app.get("/", response_class=HTMLResponse)
async def index():
    global INDEX_HTML
    if INDEX_HTML is None:
        index_path = os.path.join(SCRIPT_DIR, "site", "index.html")
        if os.path.exists(index_path):
            with open(index_path) as f:
                INDEX_HTML = f.read()
    # Always serve the dynamic app page
    return get_app_html()


def get_app_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guerrilla Night — AI-Curated Overnight Radio</title>
<meta name="description" content="AI-curated 6-hour playlists inspired by Radio Guerrilla's legendary overnight block.">
<meta property="og:title" content="Guerrilla Night">
<meta property="og:description" content="AI-curated overnight radio. Press play, lose 6 hours.">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0a0f;color:#e0ddd5;font-family:'Space Grotesk',system-ui,sans-serif;min-height:100vh}
  .bg{position:fixed;top:0;left:0;right:0;bottom:0;background:radial-gradient(ellipse at 40% 30%,rgba(180,120,255,.06) 0%,transparent 50%),radial-gradient(ellipse at 60% 70%,rgba(100,180,255,.04) 0%,transparent 50%);pointer-events:none;z-index:0}
  .wrap{position:relative;z-index:1;max-width:900px;margin:0 auto;padding:1rem}

  /* Header */
  .header{text-align:center;padding:3rem 1rem 1.5rem}
  .header h1{font-size:2.8rem;font-weight:700;letter-spacing:-.03em;color:#fff}
  .header h1 span{background:linear-gradient(135deg,#c084fc,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .tagline{margin-top:.5rem;font-size:.95rem;color:rgba(255,255,255,.4);line-height:1.5}

  /* Generate section */
  .generate{text-align:center;padding:1rem 0 0}
  .model-btns{display:flex;gap:.5rem;justify-content:center;flex-wrap:wrap;margin-top:1rem}
  .model-btn{padding:.6rem 1.4rem;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);color:rgba(255,255,255,.6);border-radius:8px;font-family:'Space Grotesk',sans-serif;font-size:.9rem;cursor:pointer;transition:all .15s}
  .model-btn:hover{background:rgba(192,132,252,.1);border-color:rgba(192,132,252,.3);color:#fff}
  .model-btn.active{background:linear-gradient(135deg,#c084fc,#7c3aed);color:#fff;border-color:transparent;box-shadow:0 4px 20px rgba(124,58,237,.3)}
  .model-btn:disabled{opacity:.4;cursor:wait}
  .gen-label{font-size:.75rem;text-transform:uppercase;letter-spacing:.15em;color:rgba(255,255,255,.2)}

  /* Progress */
  .progress{margin:1.5rem auto;max-width:500px;display:none}
  .progress.show{display:block}
  .progress-bar{height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden;margin-top:.8rem}
  .progress-fill{height:100%;background:linear-gradient(90deg,#c084fc,#60a5fa);width:0%;transition:width .3s}
  .progress-text{font-family:'JetBrains Mono',monospace;font-size:.75rem;color:rgba(255,255,255,.35);margin-top:.4rem;min-height:1.2em}
  .progress-log{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:rgba(255,255,255,.2);margin-top:.3rem;max-height:80px;overflow-y:auto}

  /* Player */
  .player{display:none;margin-top:1rem}
  .player.show{display:block}
  .player-grid{display:flex;gap:1rem}
  .player-video{flex:0 0 55%;min-width:0}
  #yt-player-wrap{background:#000;border-radius:10px;overflow:hidden;aspect-ratio:16/9;width:100%}
  .player-list{flex:1;max-height:70vh;overflow-y:auto;padding-right:.5rem}
  .np{padding:.8rem 1rem;background:rgba(255,255,255,.03);border-radius:8px;margin-top:.8rem}
  .np-label{font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;color:rgba(255,255,255,.25)}
  .np-artist{font-size:1rem;font-weight:700;color:#fff;margin-top:.15rem}
  .np-title{font-size:.85rem;color:rgba(255,255,255,.45)}
  .np-genres{display:flex;gap:.3rem;margin-top:.3rem}
  .np-genres span{font-size:.6rem;font-family:'JetBrains Mono',monospace;padding:.1rem .45rem;border-radius:3px;background:rgba(192,132,252,.1);color:rgba(192,132,252,.6)}
  .controls{display:flex;gap:.4rem;margin-top:.6rem;justify-content:center}
  .controls button{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);color:rgba(255,255,255,.5);padding:.35rem .9rem;border-radius:6px;font-family:'Space Grotesk',sans-serif;font-size:.8rem;cursor:pointer;transition:all .15s}
  .controls button:hover{background:rgba(255,255,255,.1);color:#fff}
  .yt-save{margin-top:.6rem;text-align:center}
  .yt-save-btn{padding:.4rem 1rem;background:rgba(255,59,48,.08);border:1px solid rgba(255,59,48,.15);color:rgba(255,255,255,.5);border-radius:6px;font-family:'Space Grotesk',sans-serif;font-size:.75rem;cursor:pointer;transition:all .15s}
  .yt-save-btn:hover{background:rgba(255,59,48,.15);border-color:rgba(255,59,48,.3);color:#fff}
  .yt-save-btn:disabled{opacity:.4;cursor:wait}
  .yt-save-btn svg{width:14px;height:14px;vertical-align:-2px;margin-right:.3rem;fill:currentColor}

  .trk{display:grid;grid-template-columns:28px 40px 1fr;gap:0 .5rem;padding:.45rem .6rem;border-radius:6px;cursor:pointer;transition:background .12s;align-items:center}
  .trk:hover{background:rgba(255,255,255,.04)}
  .trk.active{background:rgba(192,132,252,.07);border-left:2px solid #c084fc}
  .trk-n{font-family:'JetBrains Mono',monospace;font-size:.65rem;color:rgba(255,255,255,.15);text-align:center}
  .trk.active .trk-n{color:#c084fc}
  .trk-t{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:rgba(255,255,255,.2)}
  .trk-a{font-size:.82rem;font-weight:600;color:#e0ddd5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .trk-s{font-size:.75rem;color:rgba(255,255,255,.35);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

  /* Existing playlists */
  .existing{margin-top:2rem;padding:1rem 0}
  .existing h2{font-size:.7rem;text-transform:uppercase;letter-spacing:.15em;color:rgba(255,255,255,.2);margin-bottom:.8rem;text-align:center}
  .ex-list{display:flex;flex-direction:column;gap:.4rem;max-width:500px;margin:0 auto}
  .ex-item{display:flex;align-items:center;gap:.8rem;padding:.5rem .8rem;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:6px;cursor:pointer;transition:all .12s;text-decoration:none;color:inherit}
  .ex-item:hover{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.08)}
  .ex-model{font-weight:600;font-size:.85rem;text-transform:capitalize;color:#e0ddd5}
  .ex-info{font-size:.7rem;color:rgba(255,255,255,.25);font-family:'JetBrains Mono',monospace;margin-left:auto}

  .footer{text-align:center;padding:3rem 1rem 2rem;font-size:.65rem;color:rgba(255,255,255,.1);font-family:'JetBrains Mono',monospace}
  .footer a{color:rgba(255,255,255,.15)}

  @media(max-width:700px){
    .player-grid{flex-direction:column}
    .player-video{flex:none}
    .player-list{max-height:40vh}
    .header h1{font-size:2rem}
  }
</style>
</head>
<body>
<div class="bg"></div>
<div class="wrap">

  <div class="header">
    <h1>Guerrilla <span>Night</span></h1>
    <p class="tagline">AI-curated overnight radio inspired by Radio Guerrilla.<br>6 hours of alternative, electronic, trip-hop, folk. Press play.</p>
    <div class="generate" id="generate-section">
      <div class="gen-label">Generate a fresh playlist</div>
      <div class="model-btns" id="model-btns">Loading models...</div>
    </div>
  </div>

  <div class="progress" id="progress">
    <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
    <div class="progress-text" id="progress-text"></div>
    <div class="progress-log" id="progress-log"></div>
  </div>

  <div class="player" id="player">
    <div class="player-grid">
      <div class="player-video">
        <div id="yt-player-wrap"><div id="yt-player"></div></div>
        <div class="np">
          <div class="np-label">Now Playing</div>
          <div class="np-artist" id="np-artist">&mdash;</div>
          <div class="np-title" id="np-title">&mdash;</div>
          <div class="np-genres" id="np-genres"></div>
        </div>
        <div class="controls">
          <button onclick="prevTrack()">Prev</button>
          <button onclick="togglePlay()" id="btn-play">Pause</button>
          <button onclick="nextTrack()">Next</button>
        </div>
        <div class="yt-save" id="yt-save" style="display:none">
          <button class="yt-save-btn" onclick="saveToYouTube()" id="yt-save-btn">
            <svg viewBox="0 0 24 24"><path d="M19.615 3.184c-3.604-.246-11.631-.245-15.23 0C.488 3.45.029 5.804 0 12c.029 6.185.484 8.549 4.385 8.816 3.6.245 11.626.246 15.23 0C23.512 20.55 23.971 18.196 24 12c-.029-6.185-.484-8.549-4.385-8.816zM9 16V8l8 4-8 4z"/></svg>
            Save to YouTube
          </button>
        </div>
      </div>
      <div class="player-list" id="tracklist"></div>
    </div>
  </div>

  <div class="existing" id="existing" style="display:none">
    <h2>Previous Playlists</h2>
    <div class="ex-list" id="ex-list"></div>
  </div>

  <div class="footer">
    Built by Eloquentix with AI and a love for late-night radio.<br>
    Inspired by <a href="https://www.guerrillaradio.ro" target="_blank" rel="noopener">Radio Guerrilla</a>.
    &middot; <a href="https://github.com/radurosu/guerillanight" target="_blank">GitHub</a>
  </div>

</div>

<!-- YouTube IFrame API -->
<script>
let ytPlayer, currentIdx = 0, tracks = [];
let playerStarted = false, playerCreated = false;
const START_AFTER = 3; // start playing after this many tracks found

// Load YouTube API
const tag = document.createElement('script');
tag.src = 'https://www.youtube.com/iframe_api';
document.head.appendChild(tag);
let ytReady = false;
function onYouTubeIframeAPIReady() { ytReady = true; }

function startPlayer() {
  if (playerStarted || tracks.length < 1) return;
  playerStarted = true;
  document.getElementById('player').classList.add('show');

  if (ytReady) createYTPlayer();
  else {
    const check = setInterval(() => {
      if (ytReady) { clearInterval(check); createYTPlayer(); }
    }, 200);
  }
}

function createYTPlayer() {
  if (playerCreated) return;
  playerCreated = true;
  ytPlayer = new YT.Player('yt-player', {
    width: '100%', height: '100%',
    videoId: tracks[0].id,
    playerVars: { autoplay: 1, modestbranding: 1, rel: 0 },
    events: {
      onReady: (e) => { updateNP(0); updateMediaSession(); e.target.playVideo(); },
      onStateChange: (e) => {
        if (e.data === 0) nextTrack();
        document.getElementById('btn-play').textContent = e.data === 1 ? 'Pause' : 'Play';
        if (e.data === 1) updateMediaSession();
      }
    }
  });
}

function addTrackToList(t) {
  const i = tracks.length;
  tracks.push(t);
  const el = document.getElementById('tracklist');
  const row = document.createElement('div');
  row.className = 'trk';
  row.id = 'trk-' + i;
  row.onclick = () => playIdx(i);
  row.innerHTML = `<div class="trk-n">${String(i+1).padStart(2,'0')}</div><div class="trk-t">${t.time}</div><div><div class="trk-a">${t.artist}</div><div class="trk-s">${t.title}</div></div>`;
  el.appendChild(row);

  // Start player after enough tracks
  if (tracks.length >= START_AFTER && !playerStarted) startPlayer();
}

function initPlayer(trackList) {
  tracks = trackList;
  currentIdx = 0;
  if (!tracks.length) return;

  // Destroy old player if re-loading
  if (playerCreated && ytPlayer && ytPlayer.destroy) {
    try { ytPlayer.destroy(); } catch(e) {}
    document.getElementById('yt-player-wrap').innerHTML = '<div id="yt-player"></div>';
    playerCreated = false;
  }
  playerStarted = true;

  document.getElementById('player').classList.add('show');
  const el = document.getElementById('tracklist');
  el.innerHTML = '';
  tracks.forEach((t, i) => {
    const row = document.createElement('div');
    row.className = 'trk';
    row.id = 'trk-' + i;
    row.onclick = () => playIdx(i);
    row.innerHTML = `<div class="trk-n">${String(i+1).padStart(2,'0')}</div><div class="trk-t">${t.time}</div><div><div class="trk-a">${t.artist}</div><div class="trk-s">${t.title}</div></div>`;
    el.appendChild(row);
  });

  if (ytReady) createYTPlayer();
  else { const c = setInterval(() => { if (ytReady) { clearInterval(c); createYTPlayer(); } }, 200); }
}

function updateNP(idx) {
  const t = tracks[idx];
  document.getElementById('np-artist').textContent = t.artist;
  document.getElementById('np-title').textContent = t.title;
  document.getElementById('np-genres').innerHTML = t.genres.map(g => `<span>${g}</span>`).join('');
  document.querySelectorAll('.trk').forEach(el => el.classList.remove('active'));
  const active = document.getElementById('trk-' + idx);
  if (active) { active.classList.add('active'); active.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
  currentIdx = idx;
}

function playIdx(i) { if (i >= 0 && i < tracks.length) { ytPlayer.loadVideoById(tracks[i].id); updateNP(i); updateMediaSession(); } }
function nextTrack() { playIdx(currentIdx + 1); }
function prevTrack() { playIdx(currentIdx - 1); }
function togglePlay() {
  if (ytPlayer.getPlayerState() === 1) ytPlayer.pauseVideo();
  else ytPlayer.playVideo();
}

// ── Background playback ──
let wasPlayingBeforeHidden = false;
document.addEventListener('visibilitychange', () => {
  if (!ytPlayer || !playerStarted) return;
  if (document.hidden) {
    wasPlayingBeforeHidden = ytPlayer.getPlayerState() === 1;
    if (wasPlayingBeforeHidden) {
      // Try to keep playing in background
      setTimeout(() => { try { ytPlayer.playVideo(); } catch(e) {} }, 200);
      setTimeout(() => { try { ytPlayer.playVideo(); } catch(e) {} }, 1000);
    }
  } else {
    // Resumed — restart if it was paused by the browser
    if (wasPlayingBeforeHidden && ytPlayer.getPlayerState() !== 1) {
      ytPlayer.playVideo();
    }
  }
});

// ── Media Session (lock screen controls) ──
function updateMediaSession() {
  if (!('mediaSession' in navigator) || !tracks[currentIdx]) return;
  const t = tracks[currentIdx];
  navigator.mediaSession.metadata = new MediaMetadata({
    title: t.title,
    artist: t.artist,
    album: 'Guerrilla Night',
  });
  navigator.mediaSession.setActionHandler('previoustrack', prevTrack);
  navigator.mediaSession.setActionHandler('nexttrack', nextTrack);
  navigator.mediaSession.setActionHandler('play', () => ytPlayer.playVideo());
  navigator.mediaSession.setActionHandler('pause', () => ytPlayer.pauseVideo());
}

// ── Generate ──
async function generate(modelKey) {
  // Reset state
  tracks = [];
  playerStarted = false;
  playerCreated = false;
  document.getElementById('tracklist').innerHTML = '';
  document.getElementById('player').classList.remove('show');
  const oldPlayer = document.getElementById('yt-player');
  if (oldPlayer && oldPlayer.tagName === 'IFRAME') {
    oldPlayer.parentNode.innerHTML = '<div id="yt-player"></div>';
  }

  document.querySelectorAll('.model-btn').forEach(b => b.disabled = true);
  const prog = document.getElementById('progress');
  const fill = document.getElementById('progress-fill');
  const text = document.getElementById('progress-text');
  const log = document.getElementById('progress-log');
  prog.classList.add('show');
  fill.style.width = '5%';
  text.textContent = 'Starting...';
  log.textContent = '';

  try {
    const resp = await fetch('/api/generate/' + modelKey);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let lines = buffer.split('\\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = JSON.parse(line.slice(6));

        if (data.type === 'status') {
          text.textContent = data.message;
          log.textContent += data.message + '\\n';
          log.scrollTop = log.scrollHeight;
          if (data.message.includes('Generating')) fill.style.width = '20%';
          if (data.message.includes('Finding')) fill.style.width = '40%';
        }
        if (data.type === 'track') {
          // Stream track into player as it's found
          addTrackToList({
            id: data.id, time: data.time,
            artist: data.artist, title: data.title,
            genres: data.genres
          });
          text.textContent = playerStarted
            ? `Playing! Still finding videos (${data.found}/${data.total})...`
            : `Found ${data.found} tracks, starting after ${START_AFTER}...`;
        }
        if (data.type === 'progress') {
          const pct = 40 + (data.current / data.total) * 55;
          fill.style.width = pct + '%';
          if (!playerStarted) text.textContent = data.message;
        }
        if (data.type === 'complete') {
          fill.style.width = '100%';
          text.textContent = data.message;
          setTimeout(() => { prog.classList.remove('show'); }, 2000);
          // If player never started (< 3 tracks found), start now
          if (!playerStarted && tracks.length > 0) startPlayer();
        }
        if (data.type === 'error') {
          text.textContent = data.message;
          fill.style.background = '#f87171';
        }
      }
    }
  } catch (e) {
    text.textContent = 'Connection error: ' + e.message;
  }

  document.querySelectorAll('.model-btn').forEach(b => b.disabled = false);
}

async function loadPlaylist(filename, updateUrl) {
  if (updateUrl === undefined) updateUrl = true;
  try {
    const resp = await fetch('/api/playlist/' + encodeURIComponent(filename));
    if (!resp.ok) return false;
    const data = await resp.json();
    if (data.tracks && data.tracks.length) {
      initPlayer(data.tracks);
      if (updateUrl) {
        const u = new URL(window.location);
        u.searchParams.set('playlist', filename);
        history.replaceState(null, '', u);
      }
      return true;
    }
  } catch(e) { console.error(e); }
  return false;
}

// ── Save to YouTube ──
let gisLoaded = false, ytTokenClient = null;

function loadGIS(clientId) {
  const s = document.createElement('script');
  s.src = 'https://accounts.google.com/gsi/client';
  s.onload = () => {
    gisLoaded = true;
    ytTokenClient = google.accounts.oauth2.initTokenClient({
      client_id: clientId,
      scope: 'https://www.googleapis.com/auth/youtube',
      callback: (resp) => {
        console.log('GIS token response:', resp);
        console.log('Scope granted:', resp.scope);
        if (resp.error) {
          console.error('OAuth error:', resp);
          document.getElementById('yt-save-btn').textContent = 'Auth error: ' + resp.error;
          document.getElementById('yt-save-btn').disabled = false;
          return;
        }
        if (resp.access_token) doCreatePlaylist(resp.access_token);
      },
    });
    document.getElementById('yt-save').style.display = '';
  };
  document.head.appendChild(s);
}

function saveToYouTube() {
  if (!ytTokenClient || !tracks.length) return;
  document.getElementById('yt-save-btn').disabled = true;
  document.getElementById('yt-save-btn').textContent = 'Signing in...';
  ytTokenClient.requestAccessToken({ prompt: 'consent' });
}

async function doCreatePlaylist(token) {
  const btn = document.getElementById('yt-save-btn');
  try {
    btn.textContent = 'Creating playlist...';
    const headers = { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' };
    const date = new Date().toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });

    // Create playlist
    const plResp = await fetch('https://www.googleapis.com/youtube/v3/playlists?part=snippet,status', {
      method: 'POST', headers,
      body: JSON.stringify({
        snippet: { title: 'Guerrilla Night — ' + date, description: 'AI-curated overnight radio — guerillanight.eloquentix.com' },
        status: { privacyStatus: 'unlisted' }
      })
    });
    console.log('Playlist API response status:', plResp.status);
    const pl = await plResp.json();
    console.log('Playlist API response body:', JSON.stringify(pl));
    if (!pl.id) { btn.textContent = 'Error: ' + (pl.error?.errors?.[0]?.reason || pl.error?.message || JSON.stringify(pl.error)); btn.disabled = false; console.error('YT API error:', pl); return; }

    // Add tracks sequentially with throttle to avoid 403 rateLimitExceeded
    for (let i = 0; i < tracks.length; i++) {
      btn.textContent = 'Adding tracks (' + (i + 1) + '/' + tracks.length + ')...';
      const resp = await fetch('https://www.googleapis.com/youtube/v3/playlistItems?part=snippet', {
        method: 'POST', headers,
        body: JSON.stringify({
          snippet: { playlistId: pl.id, resourceId: { kind: 'youtube#video', videoId: tracks[i].id } }
        })
      });
      if (!resp.ok) console.warn('Failed to add track ' + tracks[i].id);
      await new Promise(r => setTimeout(r, 100));
    }

    // Deep link — window.location.href forces native YouTube app on mobile
    const url = 'https://www.youtube.com/playlist?list=' + pl.id;
    btn.innerHTML = '<a href="' + url + '" target="_blank" style="color:#fff;text-decoration:none">Open on YouTube &#8599;</a>';
    btn.disabled = false;
    window.location.href = url;
  } catch(e) {
    btn.textContent = 'Error: ' + e.message;
    btn.disabled = false;
  }
}

// ── Init ──
async function init() {
  // Load models + playlists + config in parallel
  const [modelsResp, playlistsResp, configResp] = await Promise.all([
    fetch('/api/models'),
    fetch('/api/playlists'),
    fetch('/api/config')
  ]);
  const models = await modelsResp.json();
  const playlists = await playlistsResp.json();
  const config = await configResp.json();

  // Load Google Identity Services if YouTube export is enabled
  if (config.youtube_enabled) loadGIS(config.google_client_id);

  // Populate generate buttons
  const container = document.getElementById('model-btns');
  if (!models.length) {
    container.innerHTML = '<span style="color:rgba(255,255,255,.3)">No API keys configured</span>';
  } else {
    container.innerHTML = models.map(m =>
      `<button class="model-btn" onclick="generate('${m.key}')">${m.name}</button>`
    ).join('');
  }

  // Auto-load: ?playlist=<filename> wins, falls back to most recent default.
  const withYT = playlists.filter(p => p.has_youtube);
  const requested = new URLSearchParams(window.location.search).get('playlist');
  let loaded = false;
  if (requested) {
    loaded = await loadPlaylist(requested);
    if (!loaded) console.warn('Playlist not found, falling back to default:', requested);
  }
  if (withYT.length) {
    if (!loaded) await loadPlaylist(withYT[0].filename);

    // Show remaining as "Previous Playlists"
    if (withYT.length > 1) {
      document.getElementById('existing').style.display = '';
      const list = document.getElementById('ex-list');
      withYT.slice(1, 6).forEach(p => {
        const el = document.createElement('div');
        el.className = 'ex-item';
        el.onclick = () => loadPlaylist(p.filename);
        el.innerHTML = `<span class="ex-model">${p.generator}</span><span class="ex-info">${p.track_count} tracks &middot; ${p.model_id}</span>`;
        list.appendChild(el);
      });
    }
  }
}

init();
</script>
</body>
</html>"""


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = 8900
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])
    print(f"\n  Guerrilla Night — http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
