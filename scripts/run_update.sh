#!/bin/bash
# Chotot Marketplace Dashboard — weekly data update
# Run manually: bash ~/dashboard/scripts/run_update.sh
# Auto crontab: 30 10 * * 1

set -e
DIR="$HOME/dashboard"
GCLOUD="$HOME/google-cloud-sdk/bin/gcloud"
LOG="$DIR/scripts/update.log"

echo "========================================" | tee -a "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') Starting update..." | tee -a "$LOG"

cd "$DIR"

# Ensure gcloud auth is active
$GCLOUD auth list --filter=status:ACTIVE 2>/dev/null | grep chile@chotot.vn > /dev/null || {
  echo "⚠️  gcloud not authenticated. Run: ~/google-cloud-sdk/bin/gcloud auth login" | tee -a "$LOG"
  exit 1
}

# Pull latest
git pull --quiet

# Run Python update (patches index.html source)
python3 scripts/update_dashboard.py 2>&1 | tee -a "$LOG"

# Pre-compile JSX → plain JS (permanent fix — no browser Babel needed)
node build.js 2>&1 | tee -a "$LOG"

# Copy compiled output to root for GitHub Pages
cp dist/index.html index.html

# Commit & push if changed
git add index.html dist/index.html
if git diff --staged --quiet; then
  echo "$(date '+%H:%M:%S') No new data — already up to date." | tee -a "$LOG"
else
  git commit -m "data: auto-update dashboard $(date '+%Y-%m')"
  git push
  echo "$(date '+%H:%M:%S') ✅ Pushed — dashboard updated." | tee -a "$LOG"
fi

echo "$(date '+%H:%M:%S') Done." | tee -a "$LOG"
