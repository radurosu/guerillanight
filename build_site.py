#!/usr/bin/env python3
"""
Builds the guerillanight.eloquentix.com static site from generated playlists.
Outputs to ./site/ ready for rsync to the server.

Usage:
    python3 build_site.py
    python3 build_site.py --ga G-XXXXXXXXXX   # with Google Analytics
"""

import json
import os
import re
import shutil
import sys
from glob import glob
from pathlib import Path

SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "playlists")

# Parse --ga flag
GA_ID = ""
for i, arg in enumerate(sys.argv[1:], 1):
    if arg == "--ga" and i < len(sys.argv) - 1:
        GA_ID = sys.argv[i + 1]
    elif arg.startswith("--ga="):
        GA_ID = arg.split("=", 1)[1]


def tracking_snippet() -> str:
    """Google Analytics 4 + custom event tracking."""
    ga_block = ""
    if GA_ID:
        ga_block = f"""
  <!-- Google Analytics 4 -->
  <script async src="https://www.googletagmanager.com/gtag/js?id={GA_ID}"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{ dataLayer.push(arguments); }}
    gtag('js', new Date());
    gtag('config', '{GA_ID}');
  </script>"""

    return f"""{ga_block}
  <!-- Guerrilla Night Analytics -->
  <script>
    window.gnTrack = function(event, data) {{
      // GA4 custom events
      if (typeof gtag === 'function') {{
        gtag('event', event, data || {{}});
      }}
      // Console log for debugging
      console.log('[GN]', event, data || '');
    }};
  </script>"""


def inject_tracking(html: str) -> str:
    """Inject tracking snippet into HTML head."""
    snippet = tracking_snippet()
    # Insert before </head>
    return html.replace("</head>", f"{snippet}\n</head>")


def inject_play_tracking(html: str) -> str:
    """Add play/track-change event tracking to player pages."""
    tracking_js = """
    // Track playlist events
    (function() {
      var tracked = {};
      var origUpdateNP = window.updateNowPlaying;
      if (typeof origUpdateNP === 'function') {
        window.updateNowPlaying = function(idx) {
          origUpdateNP(idx);
          var t = tracks[idx];
          if (t && !tracked[idx]) {
            tracked[idx] = true;
            gnTrack('track_play', {
              track_name: t.artist + ' - ' + t.title,
              track_index: idx,
              track_time: t.time
            });
          }
        };
      }
      // Track page load
      gnTrack('playlist_view', { track_count: typeof tracks !== 'undefined' ? tracks.length : 0 });
    })();
"""
    return html.replace("</script>\n</body>", f"{tracking_js}</script>\n</body>")


def find_latest_playlists() -> list[dict]:
    """Find generated playlists with player HTML files."""
    players = sorted(glob(os.path.join(DATA_DIR, "*_player.html")))
    playlists = []
    for player_path in players:
        json_path = player_path.replace("_player.html", ".json")
        score_path = player_path.replace("_player.html", "_score.json")
        if not os.path.exists(json_path):
            continue

        with open(json_path) as f:
            data = json.load(f)

        score = None
        if os.path.exists(score_path):
            with open(score_path) as f:
                score = json.load(f).get("composite_score")

        playlists.append({
            "player_path": player_path,
            "json_path": json_path,
            "generator": data.get("generator", "unknown"),
            "model_id": data.get("model_id", ""),
            "track_count": len(data.get("tracks", [])),
            "generated_at": data.get("generated_at", ""),
            "score": score,
            "filename": os.path.basename(player_path),
        })

    return playlists


def build_index(playlists: list[dict]) -> str:
    """Build the landing page."""
    # Pick the best-scoring playlist as featured
    scored = [p for p in playlists if p["score"]]
    featured = max(scored, key=lambda p: p["score"]) if scored else playlists[-1] if playlists else None

    playlist_cards = ""
    for p in sorted(playlists, key=lambda x: x.get("score", 0) or 0, reverse=True):
        score_badge = f'<span class="card-score">{p["score"]}</span>' if p["score"] else ""
        active = " featured" if p == featured else ""
        playlist_cards += f"""
      <a href="{p['filename']}" class="card{active}">
        {score_badge}
        <div class="card-model">{p['generator']}</div>
        <div class="card-info">{p['track_count']} tracks &middot; {p['model_id']}</div>
      </a>"""

    featured_link = featured["filename"] if featured else "#"
    featured_score = featured["score"] if featured else "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guerrilla Night — AI-Curated Overnight Radio</title>
<meta name="description" content="AI-curated 6-hour playlists inspired by Radio Guerrilla's legendary overnight music block. Alternative, electronic, trip-hop, folk — the soundtrack to your night.">
<meta property="og:title" content="Guerrilla Night">
<meta property="og:description" content="AI-curated overnight radio inspired by Radio Guerrilla. Press play, lose 6 hours.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://guerillanight.eloquentix.com">
{tracking_snippet()}
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
    align-items: center;
  }}

  .hero {{
    text-align: center;
    padding: 6rem 2rem 3rem;
    max-width: 700px;
    position: relative;
  }}

  .hero::before {{
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: radial-gradient(ellipse at 40% 30%, rgba(180,120,255,0.06) 0%, transparent 50%),
                radial-gradient(ellipse at 60% 70%, rgba(100,180,255,0.04) 0%, transparent 50%);
    pointer-events: none;
    z-index: 0;
  }}

  .hero > * {{ position: relative; z-index: 1; }}

  h1 {{
    font-size: 3.5rem;
    font-weight: 700;
    letter-spacing: -0.03em;
    line-height: 1.1;
    color: #fff;
  }}

  h1 span {{
    background: linear-gradient(135deg, #c084fc, #60a5fa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}

  .tagline {{
    margin-top: 1rem;
    font-size: 1.1rem;
    color: rgba(255,255,255,0.45);
    line-height: 1.6;
  }}

  .play-btn {{
    display: inline-flex;
    align-items: center;
    gap: 0.6rem;
    margin-top: 2.5rem;
    padding: 0.9rem 2.2rem;
    background: linear-gradient(135deg, #c084fc, #7c3aed);
    color: #fff;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 1.1rem;
    font-weight: 600;
    border: none;
    border-radius: 50px;
    cursor: pointer;
    text-decoration: none;
    transition: transform 0.15s, box-shadow 0.15s;
    box-shadow: 0 4px 24px rgba(124,58,237,0.3);
  }}

  .play-btn:hover {{
    transform: translateY(-2px);
    box-shadow: 0 6px 32px rgba(124,58,237,0.4);
  }}

  .play-btn svg {{
    width: 20px;
    height: 20px;
    fill: currentColor;
  }}

  .score-badge {{
    display: inline-block;
    margin-top: 1rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: rgba(255,255,255,0.3);
  }}

  .score-badge strong {{
    color: #c084fc;
    font-size: 0.85rem;
  }}

  .playlists {{
    margin-top: 3rem;
    padding: 0 2rem 4rem;
    max-width: 600px;
    width: 100%;
    position: relative;
    z-index: 1;
  }}

  .playlists h2 {{
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: rgba(255,255,255,0.2);
    margin-bottom: 1rem;
  }}

  .cards {{
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }}

  .card {{
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.8rem 1rem;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 8px;
    text-decoration: none;
    color: inherit;
    transition: background 0.15s, border-color 0.15s;
  }}

  .card:hover {{
    background: rgba(255,255,255,0.06);
    border-color: rgba(255,255,255,0.1);
  }}

  .card.featured {{
    border-color: rgba(192,132,252,0.2);
    background: rgba(192,132,252,0.04);
  }}

  .card-score {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.1rem;
    font-weight: 700;
    color: #c084fc;
    min-width: 40px;
  }}

  .card-model {{
    font-weight: 600;
    font-size: 0.95rem;
    color: #f0ece4;
    text-transform: capitalize;
  }}

  .card-info {{
    font-size: 0.75rem;
    color: rgba(255,255,255,0.3);
    font-family: 'JetBrains Mono', monospace;
    margin-left: auto;
  }}

  .footer {{
    padding: 2rem;
    text-align: center;
    font-size: 0.7rem;
    color: rgba(255,255,255,0.12);
    font-family: 'JetBrains Mono', monospace;
    position: relative;
    z-index: 1;
  }}

  .footer a {{ color: rgba(255,255,255,0.2); }}
</style>
</head>
<body>

<div class="hero">
  <h1>Guerrilla <span>Night</span></h1>
  <p class="tagline">
    AI-curated overnight radio inspired by Radio Guerrilla.<br>
    6 hours of alternative, electronic, trip-hop, folk.<br>
    Press play, lose the night.
  </p>
  <a href="{featured_link}" class="play-btn" onclick="gnTrack('play_click', {{source: 'hero'}})">
    <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
    Play Tonight's Set
  </a>
  <div class="score-badge">style match <strong>{featured_score}/100</strong></div>
</div>

<div class="playlists">
  <h2>Available Playlists</h2>
  <div class="cards">
    {playlist_cards}
  </div>
</div>

<div class="footer">
  Built with AI and a love for late-night radio.<br>
  Inspired by <a href="https://www.guerrilla.ro/" target="_blank" rel="noopener">Radio Guerrilla</a>.
</div>

<script>gnTrack('page_view', {{ page: 'index' }});</script>
</body>
</html>"""


def main():
    print("=" * 55)
    print("  Guerrilla Night — Site Builder")
    print("=" * 55)

    # Clean and recreate site dir
    if os.path.exists(SITE_DIR):
        shutil.rmtree(SITE_DIR)
    os.makedirs(SITE_DIR)

    # Find playlists
    playlists = find_latest_playlists()
    print(f"  Found {len(playlists)} playlists with players")

    if not playlists:
        print("  No player HTML files found. Run build_youtube_playlist.py first.")
        sys.exit(1)

    # Copy player HTML files with tracking injected
    for p in playlists:
        with open(p["player_path"]) as f:
            html = f.read()
        html = inject_tracking(html)
        html = inject_play_tracking(html)
        dest = os.path.join(SITE_DIR, p["filename"])
        with open(dest, "w") as f:
            f.write(html)
        print(f"  + {p['filename']} ({p['generator']}, score {p['score']})")

    # Also copy the static list HTML files
    for html_file in glob(os.path.join(DATA_DIR, "*.html")):
        basename = os.path.basename(html_file)
        if "_player" in basename:
            continue  # already copied with tracking
        with open(html_file) as f:
            html = f.read()
        html = inject_tracking(html)
        dest = os.path.join(SITE_DIR, basename)
        with open(dest, "w") as f:
            f.write(html)
        print(f"  + {basename} (static list)")

    # Build index
    index_html = build_index(playlists)
    with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
        f.write(index_html)
    print(f"  + index.html")

    ga_status = f"GA4: {GA_ID}" if GA_ID else "GA4: not configured (use --ga G-XXXXXXXXXX)"
    print(f"\n  {ga_status}")
    print(f"  Site built: {SITE_DIR}/")
    print(f"  Files: {len(os.listdir(SITE_DIR))}")


if __name__ == "__main__":
    main()
