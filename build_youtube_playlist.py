#!/usr/bin/env python3
"""
Resolves YouTube video IDs for a generated playlist and outputs:
  1. A youtube.com/watch_videos temporary playlist URL
  2. An HTML player page with embedded YouTube player

Usage:
    python3 build_youtube_playlist.py data/playlists/playlist_claude_20260528_0752.json
    python3 build_youtube_playlist.py data/playlists/playlist_grok_20260528_0752.json
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROMANIA_TZ = ZoneInfo("Europe/Bucharest")


def search_youtube(artist: str, title: str, timeout: int = 15) -> str | None:
    """Search YouTube for a track and return the video ID."""
    query = f"{artist} {title}"
    try:
        result = subprocess.run(
            ["yt-dlp", f"ytsearch1:{query}", "--get-id", "--no-download"],
            capture_output=True, text=True, timeout=timeout,
        )
        vid = result.stdout.strip()
        return vid if vid and len(vid) < 20 else None
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"      [error: {e.__class__.__name__}]", flush=True)
        return None


def resolve_ids(tracks: list[dict]) -> list[dict]:
    """Resolve YouTube video IDs for all tracks."""
    resolved = []
    total = len(tracks)
    for i, t in enumerate(tracks):
        artist = t["artist"]
        title = t["title"]
        print(f"  [{i+1:>2}/{total}] {artist} — {title}", end="", flush=True)

        vid = search_youtube(artist, title)
        if vid:
            t["youtube_id"] = vid
            print(f"  -> {vid}")
        else:
            print(f"  -> NOT FOUND")

        resolved.append(t)
        time.sleep(0.3)

    found = sum(1 for t in resolved if t.get("youtube_id"))
    print(f"\n  Resolved {found}/{total} tracks.\n")
    return resolved


def build_youtube_url(tracks: list[dict]) -> str:
    """Build a youtube.com/watch_videos temporary playlist URL."""
    ids = [t["youtube_id"] for t in tracks if t.get("youtube_id")]
    return f"https://www.youtube.com/watch_videos?video_ids={','.join(ids)}"


def build_player_html(tracks: list[dict], model: str, score: float | None) -> str:
    """Build an HTML page with an embedded YouTube playlist player."""
    playlist_data = []
    for t in tracks:
        if t.get("youtube_id"):
            playlist_data.append({
                "id": t["youtube_id"],
                "time": t.get("time", ""),
                "artist": t["artist"],
                "title": t["title"],
                "genres": t.get("genre_tags", t.get("genres", []))[:2],
            })

    tracks_json = json.dumps(playlist_data)
    score_display = f"{score}" if score else "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guerrilla Night — Player</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    background: #0a0a0f;
    color: #e0ddd5;
    font-family: 'Space Grotesk', system-ui, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }}

  .header {{
    text-align: center;
    padding: 1.5rem 1rem 1rem;
    background: linear-gradient(135deg, #0a0a0f 0%, #1a1025 50%, #0a0a0f 100%);
    border-bottom: 1px solid rgba(255,255,255,0.06);
  }}

  .header h1 {{
    font-size: 1.6rem;
    font-weight: 700;
    color: #fff;
  }}

  .header h1 span {{
    background: linear-gradient(135deg, #c084fc, #60a5fa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}

  .header .meta {{
    font-size: 0.75rem;
    color: rgba(255,255,255,0.35);
    font-family: 'JetBrains Mono', monospace;
    margin-top: 0.3rem;
  }}

  .main {{
    display: flex;
    flex: 1;
    max-width: 1200px;
    margin: 0 auto;
    width: 100%;
    gap: 0;
  }}

  .player-col {{
    flex: 0 0 640px;
    padding: 1.5rem;
    position: sticky;
    top: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }}

  #player-wrap {{
    background: #000;
    border-radius: 10px;
    overflow: hidden;
    aspect-ratio: 16/9;
    width: 100%;
  }}

  .now-playing {{
    margin-top: 1rem;
    padding: 0.8rem 1rem;
    background: rgba(255,255,255,0.03);
    border-radius: 8px;
  }}

  .now-playing .np-label {{
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: rgba(255,255,255,0.3);
  }}

  .now-playing .np-artist {{
    font-size: 1.1rem;
    font-weight: 700;
    color: #fff;
    margin-top: 0.25rem;
  }}

  .now-playing .np-title {{
    font-size: 0.95rem;
    color: rgba(255,255,255,0.55);
  }}

  .now-playing .np-genres {{
    display: flex;
    gap: 0.3rem;
    margin-top: 0.4rem;
  }}

  .now-playing .np-genres span {{
    font-size: 0.65rem;
    font-family: 'JetBrains Mono', monospace;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    background: rgba(192,132,252,0.1);
    color: rgba(192,132,252,0.7);
  }}

  .tracklist-col {{
    flex: 1;
    overflow-y: auto;
    max-height: 100vh;
    padding: 1rem 1rem 2rem 0;
  }}

  .track {{
    display: grid;
    grid-template-columns: 30px 42px 1fr;
    gap: 0 0.6rem;
    padding: 0.5rem 0.7rem;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.15s;
    align-items: center;
  }}

  .track:hover {{
    background: rgba(255,255,255,0.05);
  }}

  .track.active {{
    background: rgba(192,132,252,0.08);
    border-left: 2px solid #c084fc;
  }}

  .track.missing {{
    opacity: 0.3;
    cursor: default;
  }}

  .t-num {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: rgba(255,255,255,0.2);
    text-align: center;
  }}

  .track.active .t-num {{
    color: #c084fc;
  }}

  .t-time {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: rgba(255,255,255,0.25);
  }}

  .t-info {{
    min-width: 0;
  }}

  .t-artist {{
    font-size: 0.85rem;
    font-weight: 600;
    color: #e0ddd5;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  .t-title {{
    font-size: 0.8rem;
    color: rgba(255,255,255,0.4);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  .controls {{
    display: flex;
    gap: 0.5rem;
    margin-top: 0.8rem;
    justify-content: center;
  }}

  .controls button {{
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    color: rgba(255,255,255,0.6);
    padding: 0.4rem 1rem;
    border-radius: 6px;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.15s;
  }}

  .controls button:hover {{
    background: rgba(255,255,255,0.1);
    color: #fff;
  }}

  @media (max-width: 900px) {{
    .main {{ flex-direction: column; }}
    .player-col {{ flex: none; position: static; height: auto; padding: 1rem; }}
    .tracklist-col {{ max-height: 50vh; padding: 0.5rem 1rem; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>Guerrilla <span>Night</span></h1>
  <div class="meta">{model} &middot; {len(playlist_data)} tracks &middot; score {score_display}</div>
</div>

<div class="main">
  <div class="player-col">
    <div id="player-wrap"><div id="player"></div></div>
    <div class="now-playing" id="now-playing">
      <div class="np-label">Now Playing</div>
      <div class="np-artist" id="np-artist">—</div>
      <div class="np-title" id="np-title">—</div>
      <div class="np-genres" id="np-genres"></div>
    </div>
    <div class="controls">
      <button onclick="prevTrack()">Prev</button>
      <button onclick="togglePlay()" id="btn-play">Pause</button>
      <button onclick="nextTrack()">Next</button>
    </div>
  </div>

  <div class="tracklist-col" id="tracklist"></div>
</div>

<script>
const tracks = {tracks_json};
let currentIdx = 0;
let player;

// Build tracklist
const listEl = document.getElementById('tracklist');
tracks.forEach((t, i) => {{
  const row = document.createElement('div');
  row.className = 'track';
  row.id = 'track-' + i;
  row.onclick = () => playIndex(i);
  row.innerHTML = `
    <div class="t-num">${{String(i+1).padStart(2,'0')}}</div>
    <div class="t-time">${{t.time}}</div>
    <div class="t-info">
      <div class="t-artist">${{t.artist}}</div>
      <div class="t-title">${{t.title}}</div>
    </div>
  `;
  listEl.appendChild(row);
}});

function updateNowPlaying(idx) {{
  const t = tracks[idx];
  document.getElementById('np-artist').textContent = t.artist;
  document.getElementById('np-title').textContent = t.title;
  const gEl = document.getElementById('np-genres');
  gEl.innerHTML = t.genres.map(g => `<span>${{g}}</span>`).join('');

  document.querySelectorAll('.track').forEach(el => el.classList.remove('active'));
  const active = document.getElementById('track-' + idx);
  if (active) {{
    active.classList.add('active');
    active.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
  }}
  currentIdx = idx;
}}

function playIndex(idx) {{
  if (idx < 0 || idx >= tracks.length) return;
  player.loadVideoById(tracks[idx].id);
  updateNowPlaying(idx);
}}

function nextTrack() {{ playIndex(currentIdx + 1); }}
function prevTrack() {{ playIndex(currentIdx - 1); }}

function togglePlay() {{
  const state = player.getPlayerState();
  if (state === 1) {{
    player.pauseVideo();
    document.getElementById('btn-play').textContent = 'Play';
  }} else {{
    player.playVideo();
    document.getElementById('btn-play').textContent = 'Pause';
  }}
}}

// YouTube IFrame API
const tag = document.createElement('script');
tag.src = 'https://www.youtube.com/iframe_api';
document.head.appendChild(tag);

function onYouTubeIframeAPIReady() {{
  player = new YT.Player('player', {{
    width: '100%',
    height: '100%',
    videoId: tracks[0].id,
    playerVars: {{
      autoplay: 1,
      modestbranding: 1,
      rel: 0,
    }},
    events: {{
      onReady: (e) => {{
        updateNowPlaying(0);
        e.target.playVideo();
      }},
      onStateChange: (e) => {{
        if (e.data === 0) nextTrack(); // auto-advance on end
        if (e.data === 1) document.getElementById('btn-play').textContent = 'Pause';
        if (e.data === 2) document.getElementById('btn-play').textContent = 'Play';
      }},
    }},
  }});
}}
</script>
</body>
</html>"""


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 build_youtube_playlist.py <playlist.json>")
        sys.exit(1)

    playlist_path = sys.argv[1]
    with open(playlist_path, encoding="utf-8") as f:
        data = json.load(f)

    tracks = data.get("tracks", data) if isinstance(data, dict) else data
    model = data.get("generator", "unknown")
    model_id = data.get("model_id", "")

    # Try to load score
    score_path = playlist_path.replace(".json", "_score.json")
    score = None
    if os.path.exists(score_path):
        with open(score_path) as f:
            score = json.load(f).get("composite_score")

    print("=" * 55)
    print("  Guerrilla Night — YouTube Playlist Builder")
    print("=" * 55)
    print(f"  Playlist: {playlist_path}")
    print(f"  Model:    {model} ({model_id})")
    print(f"  Tracks:   {len(tracks)}")
    print(f"  Score:    {score}")
    print()

    # Resolve YouTube IDs
    print("  Searching YouTube for each track...\n")
    tracks = resolve_ids(tracks)

    # Save enriched playlist
    data["tracks"] = tracks
    with open(playlist_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Build YouTube URL
    yt_url = build_youtube_url(tracks)
    found = sum(1 for t in tracks if t.get("youtube_id"))

    print(f"  YouTube temp playlist ({found} tracks):")
    print(f"  {yt_url}\n")

    # Build player HTML
    html = build_player_html(tracks, f"{model} ({model_id})", score)
    html_path = playlist_path.replace(".json", "_player.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  Player page: {html_path}")
    print(f"\n  Open the player page to listen, or paste the YouTube URL in your browser.")


if __name__ == "__main__":
    main()
