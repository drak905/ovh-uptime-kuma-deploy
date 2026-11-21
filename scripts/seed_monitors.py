#!/usr/bin/env python3
"""Seed Uptime Kuma with the essential monitors, groups and the status page 'vps'. Run on the PC.

    python scripts/seed_monitors.py

Needs the Kuma admin account to exist (the owner creates it in the browser). No password or
API login is used: it edits kuma.db directly, because Kuma's API needs the owner's password
and 2FA code. Steps on the VPS:
  1. online backup of kuma.db to /opt/uptime-kuma/backups/kuma-pre-monitors-<stamp>.db (mode 600)
  2. stop ONLY the uptime-kuma container (about a minute), insert rows, start it again
  3. write /etc/uptime-kuma-push.env (mode 600) and install the push script and cron file

Safe to run again: each run makes a new backup, and existing monitors are kept by name.
Undo: stop uptime-kuma, copy the backup over kuma.db, start it, delete /etc/cron.d/uptime-kuma-push.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from monitors import build_spec  # noqa: E402
from vps import Vps, load_env  # noqa: E402

REMOTE = "/opt/uptime-kuma"
DB = f"{REMOTE}/data/kuma.db"


def put(v: Vps, local: Path, remote: str) -> None:
    v.put(str(local), remote)
    v.run(f"test -s {remote}")  # scp's exit code is not trusted (OpenSSH crash on Windows)


def main() -> int:
    env = load_env(ROOT / ".env")
    spec = build_spec(env)
    v = Vps.from_env(env)

    if v.run("docker inspect -f '{{.State.Running}}' uptime-kuma").strip() != "true":
        raise SystemExit("uptime-kuma is not running")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{REMOTE}/backups/kuma-pre-monitors-{stamp}.db"
    v.run(f"sudo bash -c \"umask 077; sqlite3 {DB} \\\".backup '{backup}'\\\"; chmod 600 '{backup}'\"")
    if v.run(f"sudo sqlite3 -readonly '{backup}' 'PRAGMA integrity_check;'") != "ok":
        raise SystemExit("backup failed its integrity check; nothing was changed")
    v.run(f"sudo rm -f '{backup}-wal' '{backup}-shm'")  # the check leaves empty WAL side files
    print(f"backup: {backup}")

    work = f"/tmp/kuma-seed-{stamp}"
    v.run(f"mkdir -m 700 {work}")
    with tempfile.TemporaryDirectory() as d:
        spec_file = Path(d) / "spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8", newline="\n")
        put(v, spec_file, f"{work}/kuma-spec.json")
    put(v, ROOT / "scripts" / "kuma_apply.py", f"{work}/kuma_apply.py")
    put(v, ROOT / "scripts" / "kuma_push.sh", f"{work}/kuma_push.sh")
    put(v, ROOT / "scripts" / "uptime-kuma-push.cron", f"{work}/uptime-kuma-push.cron")

    owner_before = v.run(f"sudo stat -c '%u:%g' {DB}")
    v.run("docker stop uptime-kuma", timeout=120)
    try:
        raw = v.run(f"sudo python3 {work}/kuma_apply.py {DB} {work}/kuma-spec.json")
        result = json.loads(raw.splitlines()[-1])
        # sqlite may have created root-owned files; give them back to the original owner
        v.run(f"sudo chown {owner_before} {DB}; for f in {DB}-wal {DB}-shm; do [ -e $f ] && sudo chown {owner_before} $f; done; true")
    finally:
        v.run("docker start uptime-kuma", timeout=120)
    print("created:", len(result["created"]), "kept:", len(result["kept"]))
    for n in result["kept"]:
        print("  kept as is:", n)

    conf = "".join(f"{k}={result['push'][k]}\n" for k in sorted(result["push"]))
    conf = f"KUMA_FQDN={env['KUMA_FQDN']}\nKUMA_TS_IP={env['KUMA_TS_IP']}\n" + conf
    # Written with LF on purpose: ssh stdin text mode would turn \n into \r\n on Windows.
    with tempfile.TemporaryDirectory() as d:
        conf_file = Path(d) / "push.env"
        conf_file.write_text(conf, encoding="utf-8", newline="\n")
        put(v, conf_file, f"{work}/uptime-kuma-push.env")
    v.run(f"sudo install -m 600 -o root -g root {work}/uptime-kuma-push.env /etc/uptime-kuma-push.env")
    v.run(f"sudo install -d -m 755 {REMOTE}/scripts && "
          f"sudo install -m 755 -o root -g root {work}/kuma_push.sh {REMOTE}/scripts/kuma_push.sh && "
          f"sudo install -m 644 -o root -g root {work}/uptime-kuma-push.cron /etc/cron.d/uptime-kuma-push")
    v.run(f"rm -rf {work}")

    for _ in range(30):
        if v.run("docker inspect -f '{{.State.Health.Status}}' uptime-kuma", check=False) == "healthy":
            break
        time.sleep(5)
    else:
        raise SystemExit("uptime-kuma did not become healthy")
    print("done: Kuma is healthy; push cron installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
