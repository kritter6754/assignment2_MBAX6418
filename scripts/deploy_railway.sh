#!/usr/bin/env bash
# Sync settings from your local .env to the Railway app.
#
# Code deploys automatically: Railway builds from GitHub on every push to main.
# The course library is managed ONLY on the website (Library tab, with the library
# password); this script never touches it. See scripts/backup_railway_library.sh to keep a
# copy, and scripts/seed_railway_library.sh to start a new Railway project from scratch.
#
#   railway login                 # once, in a browser
#   railway link                  # once per computer: pick the course-assistant project/service
#   scripts/deploy_railway.sh     # after changing .env
set -euo pipefail
cd "$(dirname "$0")/.."

KEYS="CLASS_API_KEY LLM_BASE_URL LLM_MODEL TEXT_EMBED_BASE_URL TEXT_EMBED_MODEL
TEXT_EMBED_QUERY_PREFIX TEXT_EMBED_PASSAGE_PREFIX VISUAL_EMBED_BASE_URL VISUAL_EMBED_MODEL
RERANK_BASE_URL RERANK_MODEL OCR_BASE_URL OCR_MODEL APP_USERNAME APP_PASSWORD"
for k in $KEYS; do
  v=$(grep -E "^$k=" .env | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true)
  if [ -n "$v" ]; then
    printf '%s' "$v" | railway variables --set-from-stdin "$k" --skip-deploys > /dev/null
    echo "set $k"
  fi
done

# Redeploy so the app picks up the new settings (the library on the volume is unchanged).
railway redeploy --yes > /dev/null
echo "Done. The app restarts with the new settings in about a minute."
