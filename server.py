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
import json
import re
import os
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
