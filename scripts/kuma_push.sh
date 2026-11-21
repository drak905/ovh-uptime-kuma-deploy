#!/usr/bin/env bash
# Runs as root from /etc/cron.d every minute. For each check that passes it calls the
# matching Uptime Kuma push URL. A failing check sends nothing, so Kuma turns DOWN.
# Real values (tailnet name and IP, push tokens) live in /etc/uptime-kuma-push.env (mode 600),
# written by scripts/seed_monitors.py. Nothing real is stored in the repo.
# Dry run: KUMA_PUSH_DRYRUN=1 prints "<name> pass|fail" and sends nothing.
# Every check runs under `timeout $CHECK_TIMEOUT` (default 10 s), so a hung docker or systemctl
# counts as a fail and cannot block the next check. The cron line also runs under `flock -n`,
# so a slow run is never joined by a second copy.
# shortcut: Kuma is reached with --resolve because the host does not use MagicDNS; if the
# Kuma node's tailnet IP changes, re-run seed_monitors.py to rewrite the conf file.
set -u
umask 077

CONF="${KUMA_PUSH_CONF:-/etc/uptime-kuma-push.env}"
# shellcheck disable=SC1090
. "$CONF" || { logger -t kuma-push "cannot read $CONF"; exit 1; }

DISK_MAX="${DISK_MAX:-90}"
MEM_MAX="${MEM_MAX:-90}"
DRY="${KUMA_PUSH_DRYRUN:-0}"
CHECK_TIMEOUT="${CHECK_TIMEOUT:-10}"
export DISK_MAX MEM_MAX

check_disk() { [ "$(df --output=pcent / | tail -n 1 | tr -dc '0-9')" -lt "$DISK_MAX" ]; }
check_mem() { [ "$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{printf "%d", (t-a)*100/t}' /proc/meminfo)" -lt "$MEM_MAX" ]; }
check_unit() { systemctl is-active --quiet "$1"; }
check_container() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
# every named container must be running and report health "healthy"
check_healthy() {
  local c
  for c in "$@"; do
    [ "$(docker inspect -f '{{.State.Running}} {{.State.Health.Status}}' "$c" 2>/dev/null)" = "true healthy" ] || return 1
  done
}
# timeout runs a program, not a function, so each check runs in a child bash that inherits the exported functions
export -f check_disk check_mem check_unit check_container check_healthy

# push <label> <token> <check function> [args...]
push() {
  local label="$1" token="$2"; shift 2
  if ! timeout "$CHECK_TIMEOUT" bash -c '"$@"' _ "$@"; then
    [ "$DRY" = "1" ] && echo "$label fail"
    return 0
  fi
  if [ "$DRY" = "1" ]; then
    echo "$label pass"
    return 0
  fi
  curl -fsS --max-time 10 --resolve "$KUMA_FQDN:443:$KUMA_TS_IP" -o /dev/null \
    "https://$KUMA_FQDN/api/push/$token?status=up&msg=OK" 2>/dev/null \
    || logger -t kuma-push "push failed: $label"
}

push disk "${PUSH_DISK}" check_disk
push memory "${PUSH_MEM}" check_mem
push uosserver "${PUSH_UOSSERVER}" check_unit uosserver
push hermes-agent "${PUSH_HERMES}" check_unit hermes-agent
push vn-stock-scanner "${PUSH_SCANNER}" check_container vn-stock-scanner
push arcane "${PUSH_ARCANE}" check_healthy arcane ts-arcane
