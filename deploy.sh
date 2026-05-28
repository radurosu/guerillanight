#!/bin/bash
# Deploy Guerrilla Night to production.
# Static site — just rsync HTML/CSS/JS to nginx root.
#
# One-time server setup:
#   1. sudo mkdir -p /var/www/guerillanight
#   2. Copy deploy/nginx-guerillanight.conf → /etc/nginx/sites-available/guerillanight
#   3. sudo ln -s /etc/nginx/sites-available/guerillanight /etc/nginx/sites-enabled/
#   4. sudo nginx -t && sudo systemctl reload nginx
#   5. sudo certbot --nginx -d guerillanight.eloquentix.com

set -e

SERVER="root@104.236.69.158"
REMOTE_PATH="/var/www/guerillanight/"

echo "Building site..."
python3 build_site.py

echo "Deploying to ${SERVER}:${REMOTE_PATH} ..."
rsync -avz --delete \
  --exclude '.git' \
  --exclude '.DS_Store' \
  ./site/ "${SERVER}:${REMOTE_PATH}"

echo "Done. https://guerillanight.eloquentix.com"
