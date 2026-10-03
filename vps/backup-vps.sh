#!/usr/bin/env bash
# Runs ON the cloudscale VPS (b2b-tools-01). Backs up the Statement Converter's
# learned mapping data (data/vendor_maps + data/security_maps — small JSON files).
# Tarball, verified, keep-14 local retention, then off-site to Backblaze B2
# (rclone crypt remote 'b2crypt-tools' (B2B Backblaze account, bucket b2b-tools-backup, encrypted with the desk key since 03.10.2026); self-skips until configured — see
# project-manager/vps/setup-b2-remote.sh, one key serves all tools).
# Scheduled by the ubuntu-user crontab (03:30).
set -euo pipefail

DIR=/opt/tools/banana-import
DEST_DIR="$DIR/backups"
RETAIN=14
LOG="$DEST_DIR/backup.log"

mkdir -p "$DEST_DIR"
[ -d "$DIR/data" ] || { echo "$(date -Is) ERROR data dir not found: $DIR/data" | tee -a "$LOG" >&2; exit 1; }

DEST="$DEST_DIR/banana-import-backup-$(date +%F).tar.gz"
tar -czf "$DEST" -C "$DIR" data
tar -tzf "$DEST" >/dev/null || { rm -f "$DEST"; echo "$(date -Is) ERROR tar verify failed" | tee -a "$LOG" >&2; exit 1; }

# Retention: keep newest $RETAIN
ls -1t "$DEST_DIR"/banana-import-backup-*.tar.gz 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f
echo "$(date -Is) ok backup=$(basename "$DEST")" | tee -a "$LOG"

# Off-site to Backblaze B2 (self-skips until the b2crypt-tools remote is configured)
B2_PATH="b2crypt-tools:banana-import-vps"
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q '^b2crypt-tools:'; then
  if rclone copy --include "banana-import-backup-*.tar.gz" "$DEST_DIR" "$B2_PATH/" 2>>"$DEST_DIR/rclone.log"; then
    echo "$(date -Is) ok offsite -> $B2_PATH" | tee -a "$LOG"
    rclone delete --min-age 30d "$B2_PATH/" 2>>"$DEST_DIR/rclone.log" || true
  else
    echo "$(date -Is) WARN offsite push FAILED (see rclone.log)" | tee -a "$LOG"
  fi
else
  echo "$(date -Is) offsite=skipped (b2crypt-tools remote not configured)" | tee -a "$LOG"
fi
