#!/usr/bin/env bash
# Update the always-on Railway app's settings and course library.
#
# Code deploys automatically: Railway builds from GitHub on every push to main.
# This script handles what is NOT in GitHub:
#   1. settings from your local .env  -> Railway variables (values go through stdin,
#      never the command line or logs)
#   2. the prepared library (slide images, text, indexes) -> the Railway volume at /data
#   3. a restart so the app loads the new library
#
#   railway login                 # once, in a browser
#   railway link                  # once per computer: pick the course-assistant project/service
#   scripts/deploy_railway.sh     # after adding course files locally, or changing .env
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python scripts/build_deploy_bundle.py

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

VOLUME=$(railway volume list --json | python3 -c "import json,sys; v=json.load(sys.stdin).get('volumes', []); print(v[0]['name'] if v else '')")
if [ -z "$VOLUME" ]; then
  echo "No Railway volume found. Create one first: railway volume add --mount-path /data" >&2
  exit 1
fi
echo "Uploading library to volume $VOLUME ..."
# Uploading the folder to "/" replaces /data/library on the volume.
railway volume files --volume "$VOLUME" upload deploy/build/library / --overwrite

# Restart (no rebuild) so the running app reloads the library from the volume.
railway restart --yes > /dev/null 2>&1 || railway restart > /dev/null
echo "Done. The app restarts with the new library in about a minute."
