# Guerrilla Night

AI-curated overnight radio, inspired by [Radio Guerrilla](https://www.guerrilla.ro/)'s legendary 20:00–02:00 music block.

Radio Guerrilla's overnight programming is a DJ-less, eclectic mix — alternative rock into trip-hop, Radiohead followed by Leonard Cohen, Massive Attack next to a Romanian indie band. No talk, no ads, just a six-hour nocturnal journey. This project learns that style and generates new playlists that *feel* like Guerrilla Night, while keeping the music fresh.

## How It Works

```
scrape  →  enrich  →  generate  →  score  →  play
```

1. **Scrape** (`scrape_weekly.py`) — Pulls the past week of overnight playlists from OnlineRadioBox. Designed for cron — idempotent, deduplicates, resumes where it left off.

2. **Enrich** — Tags every track with genres, popularity, and metadata via the Last.fm API. Batched with rate-limiting to stay friendly.

3. **Generate** (`generate_playlist.py`) — Computes a style profile from the knowledge base (genre distribution, popularity curve, artist rotation, Romanian artist ratio) and prompts an LLM to curate a fresh 6-hour playlist. Supports multiple models:
   - Claude (Anthropic)
   - GPT (OpenAI)
   - Gemini (Google)
   - Grok (xAI)

4. **Score** (`score_playlist.py`) — Measures how close a generated playlist matches Guerrilla's DNA across six dimensions: genre similarity, popularity curve, artist diversity, freshness, Romanian presence, and track count. Uses cosine similarity with genre normalization to bridge the gap between Last.fm's broad tags and LLM-specific genre labels.

5. **Play** (`build_youtube_playlist.py`) — Resolves YouTube video IDs for every track and generates an HTML player page with embedded YouTube playback, auto-advance, and a visual tracklist.

## Quick Start

```bash
# Clone and set up
git clone https://github.com/radurosu/guerillanight.git
cd guerillanight
python3 -m venv .venv && source .venv/bin/activate
pip install requests beautifulsoup4

# Set up API keys
cp .env.example .env
# Edit .env with your keys

# Scrape + enrich the knowledge base
python3 scrape_weekly.py

# Generate a playlist
python3 generate_playlist.py --model claude

# Score it
python3 score_playlist.py data/playlists/playlist_claude_*.json

# Build a YouTube player
python3 build_youtube_playlist.py data/playlists/playlist_claude_*.json
```

## API Keys

| Key | Required For | Get It |
|-----|-------------|--------|
| `LASTFM_API_KEY` | Genre enrichment | [last.fm/api](https://www.last.fm/api/account/create) |
| `ANTHROPIC_API_KEY` | Claude generation | [console.anthropic.com](https://console.anthropic.com/) |
| `OPENAI_API_KEY` | GPT generation | [platform.openai.com](https://platform.openai.com/) |
| `GEMINI_API_KEY` | Gemini generation | [aistudio.google.com](https://aistudio.google.com/) |
| `XAI_API_KEY` | Grok generation | [console.x.ai](https://console.x.ai/) |

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

A score above 80 means the playlist *is* Guerrilla Night. Above 65 is clearly inspired. The baseline (real Guerrilla test nights scored against training nights) sits around 63.

## Project Structure

```
├── scrape_weekly.py            # Weekly cron scraper
├── generate_playlist.py        # Multi-model playlist generator
├── score_playlist.py           # Style scorer
├── build_youtube_playlist.py   # YouTube player builder
├── data/
│   ├── guerrilla_knowledge.json    # Cumulative knowledge base
│   └── playlists/                  # Generated playlists + scores + players
└── .env.example                    # API key template
```

## What's Next

- **guerillanight.eloquentix.com** — One-click web player: visit, press play, get a curated 6-hour overnight set
- Spotify playlist generation
- Automated weekly generation via cron
- Multi-night memory (don't repeat last week's playlist)

---

Built with Claude, Grok, and a love for late-night radio.
