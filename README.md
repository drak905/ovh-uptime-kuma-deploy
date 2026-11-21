# Uptime Kuma v2 on a VPS, reachable only over Tailscale

Uptime Kuma runs in Docker next to a Tailscale sidecar container. The sidecar joins your tailnet and serves Kuma at `https://uptime-kuma.<tailnet>.ts.net` with a real certificate. Nothing is published on the host, so there is no public port.

## Layout

| Path | What it is |
| --- | --- |
| `docker/compose.yaml` | Two services: `ts-uptime-kuma` (Tailscale) and `uptime-kuma` (shares its network) |
| `docker/ts-config/ts-serve.json` | Tailscale Serve: HTTPS 443 to `http://127.0.0.1:3001`. Funnel is off |
| `scripts/deploy.py` | Deploy from the PC: `audit`, `stage`, `sidecar`, `up`, `drop-key`, `status` |
| `scripts/backup_kuma.sh` | SQLite backup, run daily 03:15 by `/etc/cron.d/uptime-kuma-backup` |
| `scripts/monitors.py`, `seed_monitors.py`, `kuma_apply.py` | Monitor plan, PC-side seeding, VPS-side insert |
| `scripts/kuma_push.sh`, `uptime-kuma-push.cron` | Host checks that feed Kuma push monitors, every minute |
| `tests/` | Six pytest tiers (below) |

Versions are pinned: `louislam/uptime-kuma:2.5.12-slim` and `tailscale/tailscale:v1.104.7`.

## Setup

1. Copy `.env.example` to `.env` and fill it in. `TS_AUTHKEY` is needed only for the first login.
2. In the Tailscale admin console, turn on MagicDNS and HTTPS Certificates.
3. `python -m venv .venv`, then `.venv\Scripts\pip install --no-cache-dir -r requirements.txt`.
4. Deploy, in order: `python scripts/deploy.py audit`, `stage`, `sidecar`, `up`, `drop-key`.
5. Open the URL, create the admin account, turn on 2FA (Settings, Security). Do this at once: until then any device on the tailnet can claim the admin account.

## Tests

`python -m pytest -p no:cacheprovider tests`

| Tier | Needs | Checks |
| --- | --- | --- |
| 1 | PC only (VPS for the leak scan) | Compose and serve config, least privilege, no ports, no docker socket, LF endings, gitleaks, no real IP or tailnet name in tracked files |
| 2 | VPS | Containers healthy, node name, TLS page answers, nothing on host port 3001, other apps and host serve config unchanged |
| 3 | PC on the tailnet, no exit node | HTTPS with a valid certificate, `wss` first frame, public IP port 3001 closed. Skips otherwise |
| 4 | VPS | Restart, force-recreate, database integrity, auth key removed |
| 5 | VPS | Backup script and archive, cron entry, outbound and tailnet DNS probes from inside Kuma |
| 6 | VPS (PC offline parts run alone) | Monitor plan, seed script on a stand-in database, backup, rows in `kuma.db`, status page `vps`, push cron, every monitor UP on the status page JSON |

Run tier 3 with the PC exit node off. With an exit node on, a probe to the VPS public IP comes back in through `tailscale0` and proves nothing.

## Monitors and status page

After the owner creates the admin account, run `python scripts/seed_monitors.py` from the PC. It needs `VPS_TS_IP`, `VPS_TS_FQDN`, `KUMA_FQDN` and `KUMA_TS_IP` in `.env`. Kuma's API needs the owner's password and 2FA code, so the script edits `kuma.db` directly. It never touches the `user` table.

1. Backs up `kuma.db` to `/opt/uptime-kuma/backups/kuma-pre-monitors-<stamp>.db` (mode 600).
2. Stops only `uptime-kuma` (about a minute), runs `scripts/kuma_apply.py` on the VPS, starts it again. Insert only, matched by name: a monitor the owner made is kept.
3. Writes `/etc/uptime-kuma-push.env` (mode 600, push tokens), installs `scripts/kuma_push.sh` and `/etc/cron.d/uptime-kuma-push`.

The plan is in `scripts/monitors.py`: groups VPS, UniFi and Apps (12 monitors, 60 s interval, 3 retries) and the public status page `/status/vps`. Push monitors are fed by the host cron once a minute, and only when their check passes. No notifications are set up. Test a push script without sending: `sudo KUMA_PUSH_DRYRUN=1 /opt/uptime-kuma/scripts/kuma_push.sh`.

Undo: `docker stop uptime-kuma`, copy the backup over `/opt/uptime-kuma/data/kuma.db` (delete `kuma.db-wal` and `kuma.db-shm`), `docker start uptime-kuma`, then `sudo rm /etc/cron.d/uptime-kuma-push /etc/uptime-kuma-push.env`.

## Telegram alerts

One notification, `Telegram (Hermes chat)`, sends every monitor's DOWN and UP to the owner's private chat through the
same bot Hermes Agent uses. It is the default, so new monitors get it too.

1. Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` (copy them from Hermes' `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS`). Never commit them.
2. `python scripts/seed_telegram.py`: backs up `kuma.db` to `/opt/uptime-kuma/backups/kuma-pre-telegram-<stamp>.db`, stops only `uptime-kuma` for about a minute, inserts the notification and links it to every monitor.
3. Tests: `pytest tests/test_07_telegram.py`.

Hermes polls this bot, so nothing here may call `getUpdates`, set a webhook or log the bot out. Kuma only calls `sendMessage`.

## Operating notes

- After changing the sidecar, recreate both services: `docker compose -p uptime-kuma --env-file /opt/uptime-kuma/.env -f /opt/uptime-kuma/docker/compose.yaml up -d --force-recreate`. Recreating only the sidecar leaves Kuma on a dead network.
- Node keys expire (180 days for an auth-key node). Disable key expiry for `uptime-kuma` in the admin console, or use a tagged key. Otherwise monitoring goes dark.
- Kuma on the same VPS cannot report that the VPS is down. Add an outside check if that matters.
- Backups stay on the same disk. Copy `/opt/uptime-kuma/backups` off the VPS for disaster recovery.
- The Docker socket is not mounted. Do not add it: it gives control of every container on the host.
- Kuma shares the sidecar network, so it cannot reach host loopback services. Monitor them by the host tailnet name.
- Never run `tailscale serve reset` on the host. The host serve config is shared with other apps.

## Fallback (only if the sidecar auth key is refused)

Save `tailscale serve status --json` first. Then on the host: `sudo tailscale serve --bg --https=8443 http://127.0.0.1:3001`, and publish Kuma as `127.0.0.1:3001:3001` only. The URL becomes the host name on port 8443. Check afterwards that the 443 and 3389 mappings are unchanged.
