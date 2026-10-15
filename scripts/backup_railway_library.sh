#!/usr/bin/env bash
# Download a copy of the course library from Railway (the only place it is edited).
#
#   scripts/backup_railway_library.sh      # -> backups/railway-library-YYYY-MM-DD-HHMM/
#
# backups/ is git-ignored: it contains the instructor's slide images.
set -euo pipefail
cd "$(dirname "$0")/.."
VOLUME=$(railway volume list --json | python3 -c "import json,sys; v=json.load(sys.stdin).get('volumes', []); print(v[0]['name'] if v else '')")
[ -n "$VOLUME" ] || { echo "No Railway volume found." >&2; exit 1; }
DEST="backups/railway-library-$(date +%Y-%m-%d-%H%M)"
mkdir -p backups
railway volume files --volume "$VOLUME" download /library "$DEST"
echo "Saved to $DEST"
