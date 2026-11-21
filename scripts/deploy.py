"""Deploy Uptime Kuma (Tailscale sidecar) to the VPS from the PC. Run from the repo root.

Steps (run in this order):
  python scripts/deploy.py audit      read-only checks, saves host baseline once
  python scripts/deploy.py stage      copy files, write VPS .env (mode 600)
  python scripts/deploy.py sidecar    start ONLY the sidecar, wait for login, show log problems
  python scripts/deploy.py up         start the whole stack
  python scripts/deploy.py drop-key   remove TS_AUTHKEY from the VPS .env
  python scripts/deploy.py status

It never touches apt, Docker engine settings, ufw, or the host's tailscale serve config.
Secret values are never printed.
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from vps import Vps, load_env  # noqa: E402

R = "/opt/uptime-kuma"
COMPOSE = f"docker compose -p uptime-kuma --env-file {R}/.env -f {R}/docker/compose.yaml"
STAGE = "/tmp/uk-stage"
FILES = {
    "docker/compose.yaml": "docker/compose.yaml",
    "docker/ts-config/ts-serve.json": "docker/ts-config/ts-serve.json",
    "scripts/backup_kuma.sh": "scripts/backup_kuma.sh",
    "scripts/uptime-kuma-backup.cron": "scripts/uptime-kuma-backup.cron",
}


def conn():
    env = load_env(ROOT / ".env")
    return Vps.from_env(env), env


def audit(v):
    print(v.run("docker ps --format '{{.Names}} {{.Status}}'; systemctl is-active hermes-agent uosserver tailscaled; "
                "free -m | sed -n 2p; df -h / | tail -1; sudo ss -tlnH | grep -c ':3001 '; true"))
    # Baseline of the host serve config, saved once. Never overwritten, never reset.
    v.run(f"sudo install -d -m 700 -o root -g root {R} {R}/baseline")
    v.run(f"test -f {R}/baseline/host-serve.json || sudo tailscale serve status --json | sudo tee {R}/baseline/host-serve.json >/dev/null; "
          f"test -f {R}/baseline/host-name.txt || tailscale status --json | jq -r .Self.HostName | sudo tee {R}/baseline/host-name.txt >/dev/null")
    print("baseline saved:", v.run(f"sudo ls {R}/baseline"))


def stage(v, env):
    v.run(f"rm -rf {STAGE}; mkdir -p {STAGE}/docker/ts-config {STAGE}/scripts")
    for src, dst in FILES.items():
        v.put(str(ROOT / src), f"{STAGE}/{dst}")
    v.run(f"ls {STAGE}/docker/compose.yaml {STAGE}/docker/ts-config/ts-serve.json {STAGE}/scripts/backup_kuma.sh {STAGE}/scripts/uptime-kuma-backup.cron >/dev/null")
    # Layout and owners. data/ is root-owned (Kuma runs as root), ts-state/ and backups/ are private.
    v.run(f"sudo install -d -m 755 -o root -g root {R} {R}/docker {R}/docker/ts-config {R}/scripts && "
          f"sudo install -d -m 750 -o root -g root {R}/data && "
          f"sudo install -d -m 700 -o root -g root {R}/ts-state {R}/backups && "
          f"sudo install -m 644 -o root -g root {STAGE}/docker/compose.yaml {R}/docker/compose.yaml && "
          f"sudo install -m 644 -o root -g root {STAGE}/docker/ts-config/ts-serve.json {R}/docker/ts-config/ts-serve.json && "
          f"sudo install -m 755 -o root -g root {STAGE}/scripts/backup_kuma.sh {R}/scripts/backup_kuma.sh && "
          f"sudo install -m 644 -o root -g root {STAGE}/scripts/uptime-kuma-backup.cron /etc/cron.d/uptime-kuma-backup && "
          f"bash -n {R}/scripts/backup_kuma.sh && rm -rf {STAGE}")
    # VPS .env: only these keys. Created with umask 077 so it is never readable by others.
    if v.run(f"test -f {R}/.env && echo yes || echo no") == "no":
        key = env.get("TS_AUTHKEY", "")
        body = f"TS_HOSTNAME={env.get('TS_HOSTNAME', 'uptime-kuma')}\nTS_AUTHKEY={key}\n"
        v.run(f"umask 077; cat > /tmp/uk.env && sudo install -m 600 -o ubuntu -g ubuntu /tmp/uk.env {R}/.env; rm -f /tmp/uk.env",
              input=body)
    print("stage ok; .env mode", v.run(f"stat -c %a {R}/.env"), "; keys:", v.run(f"cut -d= -f1 {R}/.env | tr '\\n' ' '"))
    v.run(f"{COMPOSE} config -q")
    print("compose config ok")


def sidecar_log(v):
    return v.run("docker logs ts-uptime-kuma --tail 60 2>&1 | sed -E 's/tskey-[A-Za-z0-9-]+/tskey-REDACTED/g'", check=False)


def sidecar(v):
    v.run(f"{COMPOSE} pull ts-uptime-kuma uptime-kuma", timeout=600)
    v.run(f"{COMPOSE} up -d --no-deps ts-uptime-kuma", timeout=120)
    for _ in range(30):
        out = v.run("docker exec ts-uptime-kuma tailscale status --json", check=False)
        try:
            st = json.loads(out)
        except ValueError:
            st = {}
        if st.get("BackendState") == "Running":
            print("sidecar logged in; host name:", st["Self"]["HostName"])
            return True
        log = sidecar_log(v)
        if re.search(r"invalid key|key.*(expired|revoked|invalid)|unauthorized|requires.*(auth|login)", log, re.I) and st.get("BackendState") in {"NeedsLogin", None, ""}:
            if _ > 6:
                print("AUTH KEY PROBLEM. Log tail:\n" + log[-1500:])
                return False
        time.sleep(4)
    print("sidecar not Running after 2 min. Log tail:\n" + sidecar_log(v)[-1500:])
    return False


def up(v):
    v.run(f"{COMPOSE} up -d", timeout=300)
    print(v.run("docker ps --filter name=uptime-kuma --format '{{.Names}} {{.Status}}'"))


def drop_key(v):
    v.run(f"umask 077; grep -v '^TS_AUTHKEY=' {R}/.env > /tmp/uk.env; sudo install -m 600 -o ubuntu -g ubuntu /tmp/uk.env {R}/.env; rm -f /tmp/uk.env")
    print("keys now:", v.run(f"cut -d= -f1 {R}/.env | tr '\\n' ' '"))


def status(v):
    print(v.run(f"{COMPOSE} ps; docker exec ts-uptime-kuma tailscale serve status 2>&1 | head -5; true"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    v, env = conn()
    if cmd == "audit":
        audit(v)
    elif cmd == "stage":
        stage(v, env)
    elif cmd == "sidecar":
        sys.exit(0 if sidecar(v) else 2)
    elif cmd == "up":
        up(v)
    elif cmd == "drop-key":
        drop_key(v)
    elif cmd == "status":
        status(v)
    else:
        sys.exit("unknown command")
