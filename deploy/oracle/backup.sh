#!/usr/bin/env bash
# Nightly consistent SQLite backup (safe while the core is running in WAL mode).
# Optional off-box copy: install rclone, configure a remote (e.g. Cloudflare R2
# free tier) and set BACKUP_REMOTE=r2:autocrat-backups in the environment/.env.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/autocrat}"
DB="$REPO_DIR/.autocrat/autocrat.db"
OUT_DIR="$HOME/backups/autocrat"
STAMP="$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT_DIR"

if [ ! -f "$DB" ]; then
  echo "$(date -Is) no database at $DB — skipping"
  exit 0
fi

sqlite3 "$DB" ".backup '$OUT_DIR/autocrat-$STAMP.db'"
gzip -f "$OUT_DIR/autocrat-$STAMP.db"
find "$OUT_DIR" -name 'autocrat-*.db.gz' -mtime +14 -delete
echo "$(date -Is) backup ok: autocrat-$STAMP.db.gz"

REMOTE="${BACKUP_REMOTE:-$(grep -E '^BACKUP_REMOTE=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- || true)}"
if [ -n "$REMOTE" ] && command -v rclone >/dev/null 2>&1; then
  rclone copy "$OUT_DIR/autocrat-$STAMP.db.gz" "$REMOTE" && echo "$(date -Is) copied off-box to $REMOTE"
fi
