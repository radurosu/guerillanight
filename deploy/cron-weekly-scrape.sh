#!/bin/bash
# Weekly scrape + enrich for Guerrilla Night.
# Cron: 0 8 * * 1  /var/www/guerillanight/deploy/cron-weekly-scrape.sh
#
# Runs every Monday at 08:00 UTC (11:00 Romania summer / 10:00 winter).
# Scrapes the past week from OnlineRadioBox, enriches via Last.fm,
# and updates the knowledge base that the site uses for generation.

set -euo pipefail

DIR=/var/www/guerillanight
LOG="$DIR/data/scrape.log"
VENV="$DIR/.venv/bin/python3"

# Source API keys
set -a
source "$DIR/.env"
set +a

echo "$(date -u '+%Y-%m-%d %H:%M UTC') — Starting weekly scrape" >> "$LOG"

cd "$DIR"
"$VENV" scrape_weekly.py >> "$LOG" 2>&1

echo "$(date -u '+%Y-%m-%d %H:%M UTC') — Done" >> "$LOG"
echo "" >> "$LOG"
