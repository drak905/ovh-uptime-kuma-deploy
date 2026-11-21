#!/usr/bin/env python3
"""Seed Uptime Kuma with the essential monitors, groups and the status page 'vps'. Run on the PC.

    python scripts/seed_monitors.py [backup-label]

Needs the Kuma admin account to exist (the owner creates it in the browser). No password or
API login is used: it edits kuma.db directly, because Kuma's API needs the owner's password
and 2FA code. Steps on the VPS:
  1. online backup of kuma.db to /opt/uptime-kuma/backups/kuma-pre-<label>-<stamp>.db (mode 600),
     label "monitors" unless given
  2. preflight, with Kuma still running: every push check passes in a dry run of the new script, and every
     HTTP monitor answers an accepted code from inside the Kuma container. Any miss stops the run before
     anything is changed, so a new monitor never starts DOWN
  3. stop ONLY the uptime-kuma container (about a minute), insert rows, write /etc/uptime-kuma-push.env
     (mode 600), install the push script and cron file, then start Kuma again

Safe to run again: each run makes a new backup, and existing monitors are kept by name. New monitors get the
default notification (see kuma_apply.py) and join the status page.
Undo: stop uptime-kuma, copy the backup over kuma.db, start it, delete /etc/cron.d/uptime-kuma-push.
"""
import json
import re
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


def accepts(code: int, accepted: list) -> bool:
    """Does an HTTP status match Kuma's accepted_statuscodes list (items like "200" or "200-299")?"""
    for item in accepted:
        m = re.fullmatch(r"(\d{3})(?:-(\d{3}))?", str(item))
        if m and int(m.group(1)) <= code <= int(m.group(2) or m.group(1)):
            return True
    return False


def put(v: Vps, local: Path, remote: str) -> None:
    v.put(str(local), remote)
    v.run(f"test -s {remote}")  # scp's exit code is not trusted (OpenSSH crash on Windows)


def preflight(v: Vps, spec: dict, work: str) -> None:
    """Runs with Kuma still up. Exits before any change if a new monitor would start DOWN."""
    monitors = [m for g in spec["groups"] for m in g["monitors"]]
    keys = " ".join(m["push_key"] for m in monitors if m["type"] == "push")
    pre = f"{work}/pre.env"
    # the live conf (if any) plus a dummy value for each push key it lacks: tokens do not matter in a dry run
    v.run(f"sudo bash -c 'umask 077; touch {pre}; cat /etc/uptime-kuma-push.env >> {pre} 2>/dev/null; "
          f"for k in {keys}; do grep -q \"^$k=\" {pre} || echo $k=dryrun >> {pre}; done'")
    out = v.run(f"sudo KUMA_PUSH_CONF={pre} KUMA_PUSH_DRYRUN=1 bash {work}/kuma_push.sh")
    status = dict(l.rsplit(" ", 1) for l in out.splitlines() if " " in l)
    bad = sorted(k for k, s in status.items() if s != "pass")
    if bad or len(status) != len(keys.split()):
        raise SystemExit(f"preflight: push checks not passing ({', '.join(bad) or 'count mismatch'}); nothing was changed")
    for m in (m for m in monitors if m["type"] == "http"):
        code = v.run(f"docker exec uptime-kuma curl -s -o /dev/null -w '%{{http_code}}' --max-time 15 '{m['url']}'", check=False)
        if not code.isdigit() or not accepts(int(code), json.loads(m.get("accepted_statuscodes_json", '["200-299"]'))):
            raise SystemExit(f"preflight: '{m['name']}' answers {code or 'nothing'}; nothing was changed")
    print("preflight ok: push checks pass in a dry run, HTTP monitors answer from inside Kuma")


def main() -> int:
    env = load_env(ROOT / ".env")
    spec = build_spec(env)
    v = Vps.from_env(env)

    if v.run("docker inspect -f '{{.State.Running}}' uptime-kuma").strip() != "true":
        raise SystemExit("uptime-kuma is not running")

    label = sys.argv[1] if len(sys.argv) > 1 else "monitors"
    if not re.fullmatch(r"[a-z0-9-]+", label):
        raise SystemExit("backup label: lower-case letters, digits and - only")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{REMOTE}/backups/kuma-pre-{label}-{stamp}.db"
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

    preflight(v, spec, work)

    owner_before = v.run(f"sudo stat -c '%u:%g' {DB}")
    v.run("docker stop uptime-kuma", timeout=120)
    try:
        raw = v.run(f"sudo python3 {work}/kuma_apply.py {DB} {work}/kuma-spec.json")
        result = json.loads(raw.splitlines()[-1])
        # sqlite may have created root-owned files; give them back to the original owner
        v.run(f"sudo chown {owner_before} {DB}; for f in {DB}-wal {DB}-shm; do [ -e $f ] && sudo chown {owner_before} $f; done; true")
        # token file, script and cron go in before Kuma starts, so the first push lands in the first cycle
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
    finally:
        v.run("docker start uptime-kuma", timeout=120)
    v.run(f"rm -rf {work}")
    print("created:", len(result["created"]), "kept:", len(result["kept"]))
    for n in result["kept"]:
        print("  kept as is:", n)

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
