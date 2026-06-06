#!/bin/bash
# Chotot Marketplace Dashboard — daily update
set -e
DIR="$HOME/dashboard"
cd "$DIR"
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Starting marketplace update ===" 
git pull --quiet 2>/dev/null || true
python3 scripts/update_marketplace.py
git add index.html dist/index.html src/index.html
if git diff --staged --quiet; then
  echo "No changes"
else
  git commit -m "data: auto-update $(date '+%Y-%m-%d')"
  git push
  echo "✅ Pushed to GitHub"
fi
echo "Done"
