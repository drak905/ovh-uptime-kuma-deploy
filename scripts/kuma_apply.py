#!/usr/bin/env python3
"""Runs ON the VPS (python3 standard library only). Inserts the monitor plan into kuma.db.

usage: kuma_apply.py <kuma.db> <spec.json>      (the Kuma container must be stopped)

Rules: insert only, matched by name. A monitor, group or status page that already exists is
kept as it is. The `user` table is never touched. Every monitor created by this run gets a link
to each active default notification (what Kuma's own "default enabled" does); existing monitors
get none. On a status page that already exists, monitors created by this run are added to the
page's group of the same name (the group is made if it is missing). Prints one JSON line:
created, kept and the push tokens by push_key. All inserts run in one transaction.
"""
import json
import secrets
import sqlite3
import string
import sys

ALNUM = string.ascii_letters + string.digits


def token() -> str:
    return "".join(secrets.choice(ALNUM) for _ in range(32))


def insert(cur, table, row):
    cols = [c for c in row]
    cur.execute(f'INSERT INTO "{table}" ({",".join(chr(34) + c + chr(34) for c in cols)}) '
                f'VALUES ({",".join("?" * len(cols))})', [row[c] for c in cols])
    return cur.lastrowid


def apply(db_path: str, spec: dict) -> dict:
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    monitor_cols = {r["name"] for r in cur.execute("PRAGMA table_info(monitor)")}
    owner = cur.execute("SELECT id FROM user ORDER BY id LIMIT 1").fetchone()
    if owner is None:
        raise SystemExit("no Kuma user yet: create the admin account first")
    owner = owner["id"]
    out = {"created": [], "kept": [], "push": {}}
    tables = {r["name"] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    notify = []
    if {"notification", "monitor_notification"} <= tables:
        notify = [r["id"] for r in cur.execute("SELECT id FROM notification WHERE is_default = 1 AND active = 1")]
    new_ids = set()

    cur.execute("BEGIN IMMEDIATE")
    try:
        def ensure(name, fields):
            row = cur.execute("SELECT id, push_token FROM monitor WHERE name = ?", (name,)).fetchone()
            if row:
                out["kept"].append(name)
                return row["id"], row["push_token"]
            unknown = set(fields) - monitor_cols
            if unknown:
                raise SystemExit(f"monitor column(s) missing in this Kuma version: {sorted(unknown)}")
            fields = {"name": name, "user_id": owner, "active": 1, **fields}
            monitor_id = insert(cur, "monitor", fields)
            for nid in notify:
                insert(cur, "monitor_notification", {"monitor_id": monitor_id, "notification_id": nid})
            new_ids.add(monitor_id)
            out["created"].append(name)
            return monitor_id, fields.get("push_token")

        ids = {}
        for g in spec["groups"]:
            gid, _ = ensure(g["name"], {"type": "group", "interval": 60, "retry_interval": 60, "maxretries": 0})
            for m in g["monitors"]:
                fields = {k: v for k, v in m.items() if k not in ("name", "push_key")}
                fields["parent"] = gid
                if m["type"] == "push":
                    fields["push_token"] = token()
                mid, tok = ensure(m["name"], fields)
                ids[m["name"]] = mid
                if m["type"] == "push":
                    out["push"][m["push_key"]] = tok

        sp = spec["status_page"]
        page = cur.execute("SELECT id FROM status_page WHERE slug = ?", (sp["slug"],)).fetchone()
        if page:
            out["kept"].append("status page " + sp["slug"])
            for gi, g in enumerate(spec["groups"], 1):
                fresh = [m for m in g["monitors"] if ids[m["name"]] in new_ids]
                if not fresh:
                    continue
                grp = cur.execute('SELECT id FROM "group" WHERE status_page_id = ? AND name = ?',
                                  (page["id"], g["name"])).fetchone()
                gid = grp["id"] if grp else insert(cur, "group", {
                    "name": g["name"], "public": 1, "active": 1, "weight": gi, "status_page_id": page["id"]})
                weight = cur.execute("SELECT COALESCE(MAX(weight), 0) FROM monitor_group WHERE group_id = ?",
                                     (gid,)).fetchone()[0]
                for m in fresh:
                    weight += 1
                    insert(cur, "monitor_group", {"monitor_id": ids[m["name"]], "group_id": gid, "weight": weight})
        else:
            page_id = insert(cur, "status_page", {
                "slug": sp["slug"], "title": sp["title"], "icon": "/icon.svg", "theme": "auto",
                "published": 1, "search_engine_index": 0, "show_tags": 0, "show_powered_by": 1,
                "show_certificate_expiry": 1,
            })
            for gi, g in enumerate(spec["groups"], 1):
                gid = insert(cur, "group", {"name": g["name"], "public": 1, "active": 1,
                                            "weight": gi, "status_page_id": page_id})
                for mi, m in enumerate(g["monitors"], 1):
                    insert(cur, "monitor_group", {"monitor_id": ids[m["name"]], "group_id": gid, "weight": mi})
            out["created"].append("status page " + sp["slug"])
        cur.execute("COMMIT")
    except BaseException:
        cur.execute("ROLLBACK")
        raise
    ok = cur.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        raise SystemExit("integrity_check failed after insert")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    with open(sys.argv[2], encoding="utf-8") as f:
        print(json.dumps(apply(sys.argv[1], json.load(f))))
