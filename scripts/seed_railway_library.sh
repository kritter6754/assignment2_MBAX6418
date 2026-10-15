#!/usr/bin/env bash
# ONE-TIME setup for a new Railway project: upload your local library (data/) to the volume.
#
# The live library is managed on the website. This script refuses to run when the volume
# already has a library, because uploading would replace every change made on the site.
# Pass --force only to deliberately restore from a local copy (back up first with
# scripts/backup_railway_library.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
VOLUME=$(railway volume list --json | python3 -c "import json,sys; v=json.load(sys.stdin).get('volumes', []); print(v[0]['name'] if v else '')")
[ -n "$VOLUME" ] || { echo "No Railway volume found. Create one: railway volume add --mount-path /data" >&2; exit 1; }

if railway volume files --volume "$VOLUME" list /library 2>/dev/null | grep -q "library.json"; then
  if [ "${1:-}" != "--force" ]; then
    echo "Railway already has a library. Manage it on the website (Library tab)." >&2
    echo "Refusing to overwrite it. Use --force only to deliberately replace it." >&2
    exit 1
  fi
  echo "WARNING: replacing the Railway library with your local copy (--force)."
fi
uv run python scripts/build_deploy_bundle.py
railway volume files --volume "$VOLUME" upload deploy/build/library / --overwrite
railway restart --yes > /dev/null 2>&1 || railway restart > /dev/null
echo "Library uploaded; the app restarts in about a minute."
