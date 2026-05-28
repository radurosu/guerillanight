# Guerrilla Night

AI-curated overnight radio, inspired by [Radio Guerrilla](https://www.guerrilla.ro/)'s legendary 20:00–02:00 music block.

**Live at [guerillanight.eloquentix.com](https://guerillanight.eloquentix.com)**

Radio Guerrilla's overnight programming is a DJ-less, eclectic mix — alternative rock into trip-hop, Radiohead followed by Leonard Cohen, Massive Attack next to a Romanian indie band. No talk, no ads, just a six-hour nocturnal journey. This project learns that style and generates new playlists that *feel* like Guerrilla Night, while keeping the music fresh.

## How It Works

```
scrape  →  enrich  →  generate  →  score  →  play
```

1. **Scrape** (`scrape_weekly.py`) — Pulls the past week of overnight playlists from OnlineRadioBox. Runs weekly via cron on the server. Idempotent, deduplicates, resumes where it left off.

2. **Enrich** — Tags every track with genres, popularity, and metadata via the Last.fm API. Batched with rate-limiting to stay friendly.

3. **Generate** (`generate_playlist.py`) — Computes a style profile from the knowledge base (genre distribution, popularity curve, artist rotation, Romanian artist ratio) and prompts an LLM to curate a fresh 6-hour playlist. Supports multiple models:
   - Claude (Anthropic)
   - Grok (xAI)
   - GPT (OpenAI)
   - Gemini (Google)

4. **Score** (`score_playlist.py`) — Measures how close a generated playlist matches Guerrilla's DNA across six dimensions: genre similarity, popularity curve, artist diversity, freshness, Romanian presence, and track count. Uses cosine similarity with genre normalization to bridge the gap between Last.fm's broad tags and LLM-specific genre labels.

5. **Play** — The web app at guerillanight.eloquentix.com auto-loads the latest playlist with embedded YouTube playback. Tracks are resolved via direct YouTube web search (no API key needed). During generation, tracks stream in via SSE and playback starts after the first 3 are found.

## Web App

The site (`server.py`) is a FastAPI backend serving a single-page app with:

- Auto-loading the most recent playlist on visit
- On-demand generation with real-time SSE progress streaming
- YouTube iFrame player with auto-advance
- Previous playlists browser

Deployed on a DigitalOcean droplet with nginx + certbot SSL + systemd.

## Quick Start

```bash
git clone https://github.com/radurosu/guerillanight.git
cd guerillanight
python3 -m venv .venv && source .venv/bin/activate
pip install requests beautifulsoup4 fastapi uvicorn

# Set up API keys
cp .env.example .env
# Edit .env with your keys

# Scrape + enrich the knowledge base
python3 scrape_weekly.py

# Generate a playlist
python3 generate_playlist.py --model claude

# Score it
python3 score_playlist.py data/playlists/playlist_claude_*.json

# Run the web app locally
python3 server.py
```

## API Keys

| Key | Required For | Get It |
|-----|-------------|--------|
| `LASTFM_API_KEY` | Genre enrichment | [last.fm/api](https://www.last.fm/api/account/create) |
| `ANTHROPIC_API_KEY` | Claude generation | [console.anthropic.com](https://console.anthropic.com/) |
| `XAI_API_KEY` | Grok generation | [console.x.ai](https://console.x.ai/) |
| `OPENAI_API_KEY` | GPT generation | [platform.openai.com](https://platform.openai.com/) |
| `GEMINI_API_KEY` | Gemini generation | [aistudio.google.com](https://aistudio.google.com/) |

Only `LASTFM_API_KEY` + one model key are needed to run the full pipeline.

## Scoring

The scorer compares generated playlists against real Guerrilla data across:

| Dimension | Weight | What It Measures |
|-----------|--------|-----------------|
| Genre match | 35% | Cosine similarity of normalized genre distributions |
| Freshness | 25% | % of new artists not in the knowledge base (~60% ideal) |
| Artist diversity | 15% | No repeated artists within a playlist |
| Romanian presence | 15% | Local artist ratio matches reference (~9%) |
| Track count | 10% | Close to 37 tracks for a 6-hour block |

A score above 80 means the playlist *is* Guerrilla Night. Above 65 is clearly inspired.

## Project Structure

```
├── server.py                   # FastAPI web app (frontend + API)
├── scrape_weekly.py            # Weekly cron scraper
├── generate_playlist.py        # Multi-model playlist generator
├── score_playlist.py           # Style scorer
├── build_youtube_playlist.py   # Standalone YouTube player builder (CLI)
├── deploy/
│   ├── nginx-guerillanight.conf    # nginx reverse proxy + SSL
│   ├── guerillanight.service       # systemd service
│   └── cron-weekly-scrape.sh       # Weekly data update cron
├── deploy.sh                   # One-command deploy to production
├── data/
│   ├── guerrilla_knowledge.json    # Cumulative knowledge base (~1300 tracks)
│   └── playlists/                  # Generated playlists
└── .env.example                    # API key template
```

---

Built with a love for late-night radio.
