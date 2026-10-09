#!/usr/bin/env bash
# Deploy (or redeploy) the always-on app to Railway.
#
#   railway login                 # once, in a browser
#   railway link                  # once per computer: pick the course-assistant project/service
#   scripts/deploy_railway.sh
#
# Copies settings from your local .env into Railway variables (values go through stdin,
# never the command line or logs), builds deploy/build/, and uploads it.
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

railway up deploy/build --path-as-root --detach -m "deploy $(git rev-parse --short HEAD)"
echo "Deploy started. Watch it with: railway logs"
