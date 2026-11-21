#!/usr/bin/env bash
# Online SQLite backup of Uptime Kuma. Runs as root (cron.d) because data/ is root-owned.
# Backups stay on the same disk. Copy them off the VPS if you need disaster recovery.
set -euo pipefail
umask 077

DATA_DIR="${KUMA_DATA_DIR:-/opt/uptime-kuma/data}"
OUT_DIR="${KUMA_BACKUP_DIR:-/opt/uptime-kuma/backups}"
KEEP_DAYS="${KUMA_KEEP_DAYS:-14}"

mkdir -p "$OUT_DIR"
exec 9>"$OUT_DIR/.backup.lock"
flock -n 9 || { echo "backup already running" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

[ -f "$DATA_DIR/kuma.db" ] || { echo "no kuma.db in $DATA_DIR" >&2; exit 2; }
sqlite3 "$DATA_DIR/kuma.db" ".backup '$tmp/kuma.db'"
[ "$(sqlite3 "$tmp/kuma.db" 'PRAGMA integrity_check;')" = "ok" ] || { echo "integrity_check failed" >&2; exit 3; }

stamp="$(date +%Y%m%d-%H%M%S)"
final="$OUT_DIR/uptime-kuma-$stamp.tar.gz"
tar -czf "$final.partial" -C "$tmp" kuma.db
tar -tzf "$final.partial" > /dev/null
mv "$final.partial" "$final"
chmod 600 "$final"

find "$OUT_DIR" -maxdepth 1 -type f -name 'uptime-kuma-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "$final"
