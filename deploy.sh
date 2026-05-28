#!/bin/bash
# Deploy Guerrilla Night to production.
#
# One-time server setup:
#   1. Run this script — it handles first-time setup automatically

set -e

SERVER="root@104.236.69.158"
REMOTE_PATH="/var/www/guerillanight/"

echo "Deploying to ${SERVER}:${REMOTE_PATH} ..."

rsync -avz \
  --exclude '__pycache__' \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.env' \
  --exclude '.DS_Store' \
  --exclude '.claude' \
  --exclude 'site' \
  ./ "${SERVER}:${REMOTE_PATH}"

echo "Setting up server..."
ssh "${SERVER}" bash -s <<'REMOTE'
  cd /var/www/guerillanight

  # Python venv
  if [ ! -d .venv ]; then
    echo "Creating venv..."
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q requests beautifulsoup4 fastapi uvicorn 2>/dev/null

  # Nginx config
  cp deploy/nginx-guerillanight.conf /etc/nginx/sites-available/guerillanight
  ln -sf /etc/nginx/sites-available/guerillanight /etc/nginx/sites-enabled/guerillanight
  nginx -t && systemctl reload nginx

  # Systemd service
  cp deploy/guerillanight.service /etc/systemd/system/guerillanight.service
  systemctl daemon-reload
  systemctl enable guerillanight
  systemctl restart guerillanight

  echo "Service status:"
  systemctl status guerillanight --no-pager -l | head -10
REMOTE

echo ""
echo "Done. https://guerillanight.eloquentix.com"
