"""Tier 8: Kuma watches Arcane. An HTTPS monitor and a push monitor in the 'Apps' group, on Telegram.

Part A is offline (spec, apply script on a stand-in database, push script and cron text).
Part B reads the VPS and Kuma. Assertion messages never echo IPs, tailnet names, tokens or keys.
No test sends an alert.
"""
import json
import re
import sqlite3

import pytest

from conftest import ROOT, REMOTE

import monitors  # scripts/monitors.py (conftest puts scripts/ on sys.path)
import kuma_apply

DB = f"{REMOTE}/data/kuma.db"
FAKE_ENV = {
    "VPS_TS_IP": "10.0.0.1", "VPS_TS_FQDN": "host.example.ts.net",
    "KUMA_FQDN": "kuma.example.ts.net", "KUMA_TS_IP": "10.0.0.2",
    "ARCANE_FQDN": "arcane.example.ts.net",
}
HTTP_NAME = "Arcane (HTTPS /api/health)"
PUSH_NAME = "arcane containers running"


def _apps(spec):
    return next(g for g in spec["groups"] if g["name"] == "Apps")["monitors"]


# ---------------------------------------------------------------- part A: offline

@pytest.fixture(scope="module")
def spec():
    return monitors.build_spec(FAKE_ENV)


def test_spec_needs_arcane_name():
    env = {k: v for k, v in FAKE_ENV.items() if k != "ARCANE_FQDN"}
    with pytest.raises(KeyError):
        monitors.build_spec(env)


def test_spec_arcane_http_monitor(spec):
    m = next(x for x in _apps(spec) if x["name"] == HTTP_NAME)
    assert m["type"] == "http"
    assert m["url"] == "https://arcane.example.ts.net/api/health"
    assert m["accepted_statuscodes_json"] == '["200"]'
    assert m["expiry_notification"] == 1
    assert (m["interval"], m["retry_interval"], m["maxretries"]) == (60, 60, 3)


def test_spec_arcane_push_monitor(spec):
    m = next(x for x in _apps(spec) if x["name"] == PUSH_NAME)
    assert m["type"] == "push" and m["push_key"] == "PUSH_ARCANE"
    assert (m["interval"], m["retry_interval"], m["maxretries"]) == (60, 60, 3)


def test_env_example_has_arcane_placeholder():
    t = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^ARCANE_FQDN=arcane\.<tailnet>\.ts\.net$", t, re.M)


def test_push_script_checks_both_arcane_containers_healthy():
    t = (ROOT / "scripts" / "kuma_push.sh").read_text(encoding="utf-8")
    assert "State.Health.Status" in t
    assert re.search(r"^push arcane \"\$\{PUSH_ARCANE\}\" \S+ arcane ts-arcane$", t, re.M)
    assert "healthy" in t


def test_push_script_wraps_every_check_in_timeout():
    t = (ROOT / "scripts" / "kuma_push.sh").read_text(encoding="utf-8")
    assert re.search(r"CHECK_TIMEOUT=\"\$\{CHECK_TIMEOUT:-10\}\"", t)
    assert re.search(r"timeout \"\$CHECK_TIMEOUT\" bash -c", t)
    assert "export -f" in t


def test_cron_runs_under_flock():
    c = (ROOT / "scripts" / "uptime-kuma-push.cron").read_text(encoding="utf-8")
    assert re.search(r"^\* \* \* \* \* root flock -n /run/lock/uptime-kuma-push\.lock "
                     r"/opt/uptime-kuma/scripts/kuma_push\.sh >/dev/null 2>&1$", c, re.M)


# A stand-in with the Kuma tables kuma_apply touches, including notifications and status pages.
SCHEMA = """
CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE monitor (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, active INT, user_id INT, type TEXT,
  interval INT, retry_interval INT, maxretries INT, hostname TEXT, port INT, url TEXT, timeout REAL,
  dns_resolve_server TEXT, dns_resolve_type TEXT, expiry_notification INT, push_token TEXT, parent INT,
  accepted_statuscodes_json TEXT);
CREATE TABLE status_page (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE, title TEXT, icon TEXT, theme TEXT,
  published INT, search_engine_index INT, show_tags INT, show_powered_by INT, show_certificate_expiry INT);
CREATE TABLE "group" (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, public INT, active INT, weight INT, status_page_id INT);
CREATE TABLE monitor_group (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INT, group_id INT, weight INT);
CREATE TABLE notification (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, active INT, user_id INT, is_default INT, config TEXT);
CREATE TABLE monitor_notification (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INT, notification_id INT);
INSERT INTO user VALUES (7, 'owner');
INSERT INTO notification (name, active, user_id, is_default, config) VALUES ('tg', 1, 7, 1, '{}');
INSERT INTO notification (name, active, user_id, is_default, config) VALUES ('other', 1, 7, 0, '{}');
"""
ARCANE_NAMES = {HTTP_NAME, PUSH_NAME}


def old_spec(spec):
    """The plan as it was before Arcane: the same spec without the two Arcane monitors."""
    out = json.loads(json.dumps(spec))
    for g in out["groups"]:
        g["monitors"] = [m for m in g["monitors"] if m["name"] not in ARCANE_NAMES]
    return out


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "k.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return p


def q(p, sql):
    con = sqlite3.connect(p)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_apply_adds_arcane_to_existing_status_page_and_keeps_the_rest(db, spec):
    first = kuma_apply.apply(str(db), old_spec(spec))
    assert not ARCANE_NAMES & set(first["created"])
    before_monitors = q(db, "select id,name,push_token from monitor order by id")
    before_pages = q(db, "select * from status_page")
    again = kuma_apply.apply(str(db), spec)
    assert set(again["created"]) == ARCANE_NAMES
    assert "status page vps" in again["kept"]
    # nothing existing changed (same ids, names, tokens), page row kept
    after = q(db, "select id,name,push_token from monitor order by id")
    assert after[:len(before_monitors)] == before_monitors and len(after) == len(before_monitors) + 2
    assert q(db, "select * from status_page") == before_pages
    # new monitors sit in the Apps group of the page, once, after the old members
    rows = q(db, "select m.name, g.name from monitor_group mg join monitor m on m.id=mg.monitor_id "
                 "join \"group\" g on g.id=mg.group_id where g.name='Apps' order by mg.weight")
    assert [r[0] for r in rows] == ["hermes-agent active", "vn-stock-scanner container running",
                                    HTTP_NAME, PUSH_NAME]
    assert q(db, "select count(*) from monitor_group")[0][0] == 14
    assert q(db, "select count(*) from \"group\"")[0][0] == 3
    # a third run changes nothing
    third = kuma_apply.apply(str(db), spec)
    assert third["created"] == []
    assert q(db, "select count(*) from monitor_group")[0][0] == 14
    assert again["push"]["PUSH_ARCANE"] == third["push"]["PUSH_ARCANE"]


def test_apply_links_new_monitors_to_default_notifications_only(db, spec):
    kuma_apply.apply(str(db), old_spec(spec))
    assert q(db, "select count(*) from monitor_notification")[0][0] == 3 + 12  # default 'tg' on every new row
    # a monitor of the owner's, with no link: must stay unlinked
    con = sqlite3.connect(db)
    con.execute("INSERT INTO monitor (name, active, user_id, type) VALUES ('Mine', 1, 7, 'http')")
    con.commit()
    con.close()
    kuma_apply.apply(str(db), spec)
    linked = q(db, "select m.name from monitor_notification mn join monitor m on m.id=mn.monitor_id "
                   "where mn.notification_id=(select id from notification where name='tg')")
    names = {r[0] for r in linked}
    assert ARCANE_NAMES <= names
    assert "Mine" not in names
    assert q(db, "select count(*) from monitor_notification where notification_id="
                 "(select id from notification where name='other')")[0][0] == 0
    assert q(db, "select count(*) from monitor_notification")[0][0] == 3 + 12 + 2


def test_apply_without_notification_tables_still_works(tmp_path, spec):
    p = tmp_path / "k.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA.replace("CREATE TABLE notification", "CREATE TABLE _n1")
                      .replace("CREATE TABLE monitor_notification", "CREATE TABLE _n2")
                      .replace("INSERT INTO notification", "INSERT INTO _n1"))
    con.commit()
    con.close()
    assert len(kuma_apply.apply(str(p), spec)["created"]) == 3 + 14 + 1


# ---------------------------------------------------------------- part B: live VPS

def sql(vps, query):
    out = vps.run(f"sudo sqlite3 -readonly -json {DB} \"{query}\"")
    return json.loads(out) if out else []


def test_live_arcane_monitors_in_apps_group(vps):
    apps = sql(vps, "select id from monitor where name='Apps' and type='group'")[0]["id"]
    rows = {r["name"]: r for r in sql(
        vps, f"select name,type,url,interval,retry_interval,maxretries,expiry_notification,"
             f"accepted_statuscodes_json,push_token,active,parent from monitor where parent={apps}")}
    h = rows[HTTP_NAME]
    assert h["type"] == "http" and h["url"].startswith("https://") and h["url"].endswith("/api/health")
    assert h["accepted_statuscodes_json"] == '["200"]' and h["expiry_notification"] == 1
    p = rows[PUSH_NAME]
    assert p["type"] == "push" and len(p["push_token"]) == 32
    for r in (h, p):
        assert (r["interval"], r["retry_interval"], r["maxretries"], r["active"]) == (60, 60, 3, 1)


def test_live_every_monitor_has_the_telegram_link(vps):
    out = sql(vps, "select count(*) as n from monitor where id not in "
                   "(select monitor_id from monitor_notification where notification_id="
                   "(select id from notification where name='Telegram (Hermes chat)'))")
    assert out[0]["n"] == 0


def test_live_status_page_lists_arcane_monitors(vps):
    rows = sql(vps, "select m.name as name, g.name as grp from monitor_group mg join monitor m on m.id=mg.monitor_id "
                    "join \\\"group\\\" g on g.id=mg.group_id join status_page s on s.id=g.status_page_id "
                    "where s.slug='vps'")
    got = {r["name"]: r["grp"] for r in rows}
    assert got.get(HTTP_NAME) == "Apps" and got.get(PUSH_NAME) == "Apps"
    assert len(rows) == 14


def test_live_push_conf_and_cron(vps):
    assert vps.run("sudo stat -c '%U:%G %a' /etc/uptime-kuma-push.env") == "root:root 600"
    keys = vps.run("sudo sed 's/=.*//' /etc/uptime-kuma-push.env").split()
    assert "PUSH_ARCANE" in keys
    cron = vps.run("sudo cat /etc/cron.d/uptime-kuma-push")
    assert "flock -n /run/lock/uptime-kuma-push.lock /opt/uptime-kuma/scripts/kuma_push.sh" in cron
    assert vps.run("sudo stat -c '%U:%G %a' /etc/cron.d/uptime-kuma-push") == "root:root 644"


def test_live_arcane_check_passes_now(vps):
    out = vps.run(f"sudo KUMA_PUSH_DRYRUN=1 {REMOTE}/scripts/kuma_push.sh")
    assert "arcane pass" in out.splitlines()


def _fake_docker(vps, body):
    """A fake `docker` in a temp dir, so the real push script can be tried against a bad Arcane."""
    d = vps.run("mktemp -d /tmp/fakedocker.XXXXXX")
    vps.run(f"printf '%s\\n' '#!/bin/sh' '{body}' > {d}/docker && chmod 755 {d}/docker")
    return d


def _dry(vps, path_dir, extra=""):
    out = vps.run(f"sudo env PATH={path_dir}:$PATH KUMA_PUSH_DRYRUN=1 CHECK_TIMEOUT=2 {extra} "
                  f"{REMOTE}/scripts/kuma_push.sh", check=False)
    return dict(l.rsplit(" ", 1) for l in out.splitlines() if " " in l)


@pytest.mark.parametrize("body,expected", [
    ("echo true starting", "fail"),      # running but not healthy yet
    ("echo true unhealthy", "fail"),
    ("echo false none", "fail"),         # not running
    ("exit 1", "fail"),                  # container missing
    ("sleep 30", "fail"),                # docker hangs: timeout turns it into a fail
    ("echo true healthy", "pass"),
])
def test_live_push_script_arcane_states(vps, body, expected):
    d = _fake_docker(vps, body)
    try:
        status = _dry(vps, d)
    finally:
        vps.run(f"rm -rf {d}")
    assert status.get("arcane") == expected, status
    # one hanging or failing check never blocks the others
    assert status.get("disk") == "pass" and status.get("memory") == "pass"


def test_live_push_script_one_bad_container_is_enough_to_fail(vps):
    d = vps.run("mktemp -d /tmp/fakedocker.XXXXXX")
    script = ("case \"$*\" in *ts-arcane*) echo true starting;; *) echo true healthy;; esac")
    vps.run(f"printf '%s\\n' '#!/bin/sh' '{script}' > {d}/docker && chmod 755 {d}/docker")
    try:
        status = _dry(vps, d)
    finally:
        vps.run(f"rm -rf {d}")
    assert status.get("arcane") == "fail", status


def test_live_arcane_health_answers_200_from_kuma(vps, env):
    code = vps.run(f"docker exec uptime-kuma curl -s -o /dev/null -w '%{{http_code}}' --max-time 15 "
                   f"https://{env['ARCANE_FQDN']}/api/health")
    assert code == "200"


def test_live_no_important_down_beat_for_arcane_monitors(vps):
    """Since the first Arcane beat, neither new monitor has had a DOWN beat, and nobody got a false alert."""
    out = sql(vps, f"select count(*) as n from heartbeat where important=1 and status=0 and monitor_id in "
                   f"(select id from monitor where name in ('{HTTP_NAME}','{PUSH_NAME}'))")
    assert out[0]["n"] == 0


def test_live_no_important_down_beat_since_the_change(vps):
    first = sql(vps, f"select min(time) as t from heartbeat where monitor_id in "
                     f"(select id from monitor where name in ('{HTTP_NAME}','{PUSH_NAME}'))")[0]["t"]
    assert first, "Arcane monitors have no heartbeat yet"
    out = sql(vps, f"select count(*) as n from heartbeat where important=1 and status=0 and time >= '{first}'")
    assert out[0]["n"] == 0


def test_preflight_status_matching():
    import seed_monitors
    assert seed_monitors.accepts(200, ['200'])
    assert not seed_monitors.accepts(204, ['200'])
    assert seed_monitors.accepts(204, ['200-299'])
    assert not seed_monitors.accepts(502, ['200-299', '300-399'])
    assert not seed_monitors.accepts(200, ['junk'])
