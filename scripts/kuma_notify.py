#!/usr/bin/env python3
"""Runs ON the VPS (python3 standard library only). Adds one Telegram notification to kuma.db.

usage: kuma_notify.py <kuma.db> <config.json>      (the Kuma container must be stopped)

The config file holds the notification as Kuma stores it (see build_config). Rules: insert only,
matched by name. If a notification with that name exists it is kept as it is, and only the
monitors that miss a link get one. The `user` table is never touched. One transaction.
Prints one JSON line: created, linked (new links), monitors (total).
"""
import json
import re
import sqlite3
import sys

TOKEN_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{35}$")


def build_config(name: str, token: str, chat_id: str) -> dict:
    """The JSON Kuma 2.x keeps in notification.config for its Telegram provider
    (field names from server/notification-providers/telegram.js)."""
    if not TOKEN_RE.match(token or ""):
        raise ValueError("bot token is missing or has the wrong shape")
    if not re.match(r"^-?\d+$", chat_id or ""):
        raise ValueError("chat id is missing or not a number")
    return {
        "name": name,
        "type": "telegram",
        "isDefault": True,
        "active": True,
        "applyExisting": False,
        "telegramBotToken": token,
        "telegramChatID": chat_id,
        "telegramSendSilently": False,
        "telegramProtectContent": False,
        "telegramUseTemplate": False,
    }


def apply(db_path: str, config: dict) -> dict:
    con = sqlite3.connect(db_path, isolation_level=None)
    cur = con.cursor()
    owner = cur.execute("SELECT id FROM user ORDER BY id LIMIT 1").fetchone()
    if owner is None:
        raise SystemExit("no Kuma user yet: create the admin account first")
    cur.execute("BEGIN IMMEDIATE")
    try:
        row = cur.execute("SELECT id FROM notification WHERE name = ?", (config["name"],)).fetchone()
        created = row is None
        if created:
            cur.execute("INSERT INTO notification (name, active, user_id, is_default, config) VALUES (?,?,?,?,?)",
                        (config["name"], 1, owner[0], 1, json.dumps(config)))
            nid = cur.lastrowid
        else:
            nid = row[0]
        cur.execute("INSERT INTO monitor_notification (monitor_id, notification_id) "
                    "SELECT id, ? FROM monitor WHERE id NOT IN "
                    "(SELECT monitor_id FROM monitor_notification WHERE notification_id = ?)", (nid, nid))
        linked = cur.rowcount
        total = cur.execute("SELECT COUNT(*) FROM monitor").fetchone()[0]
        cur.execute("COMMIT")
    except BaseException:
        cur.execute("ROLLBACK")
        raise
    ok = cur.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        raise SystemExit("integrity_check failed after insert")
    return {"created": created, "linked": linked, "monitors": total}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    with open(sys.argv[2], encoding="utf-8") as f:
        print(json.dumps(apply(sys.argv[1], json.load(f))))
