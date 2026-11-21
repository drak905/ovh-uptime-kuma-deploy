# Uptime Kuma v2 on a VPS, reachable only over Tailscale

Uptime Kuma runs in Docker next to a Tailscale sidecar container. The sidecar joins your tailnet and serves Kuma at `https://uptime-kuma.<tailnet>.ts.net` with a real certificate. Nothing is published on the host, so there is no public port.

## Layout

| Path | What it is |
| --- | --- |
| `docker/compose.yaml` | Two services: `ts-uptime-kuma` (Tailscale) and `uptime-kuma` (shares its network) |
| `docker/ts-config/ts-serve.json` | Tailscale Serve: HTTPS 443 to `http://127.0.0.1:3001`. Funnel is off |
| `scripts/deploy.py` | Deploy from the PC: `audit`, `stage`, `sidecar`, `up`, `drop-key`, `status` |
| `scripts/backup_kuma.sh` | SQLite backup, run daily 03:15 by `/etc/cron.d/uptime-kuma-backup` |
| `tests/` | Five pytest tiers (below) |

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

Run tier 3 with the PC exit node off. With an exit node on, a probe to the VPS public IP comes back in through `tailscale0` and proves nothing.

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
