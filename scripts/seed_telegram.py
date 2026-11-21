#!/usr/bin/env python3
"""Add the Telegram notification to Uptime Kuma, on the same bot and chat Hermes Agent uses. Run on the PC.

    python scripts/seed_telegram.py

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the git-ignored .env (never printed). Steps on the VPS:
  1. online backup of kuma.db to /opt/uptime-kuma/backups/kuma-pre-telegram-<stamp>.db (mode 600)
  2. stop ONLY the uptime-kuma container (about a minute), insert one notification 'Telegram (Hermes chat)'
     (active, default) and link it to every monitor, start the container again
The bot is only used through Kuma's sendMessage. This script never reads the bot's update queue and sets
no webhook, because Hermes polls the same bot.

Safe to run again: each run makes a new backup; an existing notification is kept and only new monitors get linked.
Undo: stop uptime-kuma, copy the backup over kuma.db, start it.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from kuma_notify import build_config  # noqa: E402
from vps import Vps, load_env  # noqa: E402

REMOTE = "/opt/uptime-kuma"
DB = f"{REMOTE}/data/kuma.db"
NAME = "Telegram (Hermes chat)"


def main() -> int:
    env = load_env(ROOT / ".env")
    config = build_config(NAME, env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""))
    v = Vps.from_env(env)

    if v.run("docker inspect -f '{{.State.Running}}' uptime-kuma").strip() != "true":
        raise SystemExit("uptime-kuma is not running")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{REMOTE}/backups/kuma-pre-telegram-{stamp}.db"
    v.run(f"sudo bash -c \"umask 077; sqlite3 {DB} \\\".backup '{backup}'\\\"; chmod 600 '{backup}'\"")
    if v.run(f"sudo sqlite3 -readonly '{backup}' 'PRAGMA integrity_check;'") != "ok":
        raise SystemExit("backup failed its integrity check; nothing was changed")
    v.run(f"sudo rm -f '{backup}-wal' '{backup}-shm'")
    print(f"backup: {backup}")

    work = f"/tmp/kuma-notify-{stamp}"
    v.run(f"mkdir -m 700 {work}")
    try:
        with tempfile.TemporaryDirectory() as d:
            cfg_file = Path(d) / "notification.json"
            cfg_file.write_text(json.dumps(config), encoding="utf-8", newline="\n")
            v.put(str(cfg_file), f"{work}/notification.json")
        v.put(str(ROOT / "scripts" / "kuma_notify.py"), f"{work}/kuma_notify.py")
        v.run(f"test -s {work}/notification.json && test -s {work}/kuma_notify.py")  # scp's exit code is not trusted

        owner_before = v.run(f"sudo stat -c '%u:%g' {DB}")
        v.run("docker stop uptime-kuma", timeout=120)
        try:
            raw = v.run(f"sudo python3 {work}/kuma_notify.py {DB} {work}/notification.json")
            result = json.loads(raw.splitlines()[-1])
            v.run(f"sudo chown {owner_before} {DB}; for f in {DB}-wal {DB}-shm; do [ -e $f ] && sudo chown {owner_before} $f; done; true")
        finally:
            v.run("docker start uptime-kuma", timeout=120)
    finally:
        v.run(f"rm -rf {work}")
    print("notification created:", result["created"], "| new links:", result["linked"], "| monitors:", result["monitors"])

    for _ in range(30):
        if v.run("docker inspect -f '{{.State.Health.Status}}' uptime-kuma", check=False) == "healthy":
            break
        time.sleep(5)
    else:
        raise SystemExit("uptime-kuma did not become healthy")
    print("done: Kuma is healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
