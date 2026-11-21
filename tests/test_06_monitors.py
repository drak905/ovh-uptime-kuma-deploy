"""Tier 6: essential monitors, groups, status page 'vps' and the host push cron.

Part A is offline (spec, cron file, push script). Part B reads the VPS and Kuma.
Assertion messages never echo IPs, tailnet names, tokens or keys.
"""
import json
import re
import shutil
import subprocess
import time

import pytest
import requests

from conftest import ROOT, REMOTE

import monitors  # scripts/monitors.py (conftest puts scripts/ on sys.path)

DB = f"{REMOTE}/data/kuma.db"
FAKE_ENV = {
    "VPS_TS_IP": "10.0.0.1", "VPS_TS_FQDN": "host.example.ts.net",
    "KUMA_FQDN": "kuma.example.ts.net", "KUMA_TS_IP": "10.0.0.2",
}
GROUPS = ["VPS", "UniFi", "Apps"]


# ---------------------------------------------------------------- part A: offline

@pytest.fixture(scope="module")
def spec():
    return monitors.build_spec(FAKE_ENV)


def _all(spec):
    return [m for g in spec["groups"] for m in g["monitors"]]


def test_spec_groups_and_status_page(spec):
    assert [g["name"] for g in spec["groups"]] == GROUPS
    assert spec["status_page"]["slug"] == "vps"
    assert spec["status_page"]["title"] == "VPS"


def test_spec_has_twelve_unique_monitors(spec):
    names = [m["name"] for m in _all(spec)]
    assert len(names) == 12
    assert len(set(names)) == 12
    assert not set(names) & set(GROUPS), "leaf names must not collide with group names"


def test_spec_interval_and_retries(spec):
    for m in _all(spec):
        assert m["interval"] == 60, m["name"]
        assert m["maxretries"] == 3, m["name"]
        assert m["retry_interval"] == 60, m["name"]


def test_spec_group_contents(spec):
    by = {g["name"]: [m["type"] for m in g["monitors"]] for g in spec["groups"]}
    assert sorted(by["VPS"]) == sorted(["ping", "port", "port", "http", "dns", "push", "push"])
    assert sorted(by["UniFi"]) == sorted(["http", "port", "push"])
    assert sorted(by["Apps"]) == ["push", "push"]


def test_spec_targets_come_from_env(spec):
    ms = {m["name"]: m for m in _all(spec)}
    ports = sorted(m["port"] for m in ms.values() if m["type"] == "port")
    assert ports == [22, 3389, 8080]
    assert any(m["type"] == "ping" and m["hostname"] == FAKE_ENV["VPS_TS_IP"] for m in ms.values())
    unifi = next(m for m in ms.values() if m["type"] == "http" and FAKE_ENV["VPS_TS_FQDN"] in m["url"])
    assert unifi["url"].startswith("https://")
    assert unifi["expiry_notification"] == 1
    assert next(m for m in ms.values() if m["type"] == "dns")["hostname"] == "google.com"
    assert any(m["type"] == "http" and m["url"] == "https://www.google.com" for m in ms.values())


def test_spec_requires_env_keys():
    with pytest.raises(KeyError):
        monitors.build_spec({})


def test_spec_push_keys_unique_and_in_script(spec):
    keys = [m["push_key"] for m in _all(spec) if m["type"] == "push"]
    assert len(keys) == 5 and len(set(keys)) == 5
    script = (ROOT / "scripts" / "kuma_push.sh").read_text(encoding="utf-8")
    for k in keys:
        assert re.fullmatch(r"PUSH_[A-Z_]+", k), k
        assert f"${{{k}" in script or f"${k}" in script, f"{k} not used in kuma_push.sh"


def test_spec_has_no_notifications_or_docker(spec):
    blob = json.dumps(spec).lower()
    assert "docker_host" not in blob and "notification" not in blob.replace("expiry_notification", "")


def test_push_cron_file():
    c = (ROOT / "scripts" / "uptime-kuma-push.cron").read_bytes().decode("utf-8")
    assert "\r" not in c and c.endswith("\n")
    assert re.search(r"^\* \* \* \* \* root /opt/uptime-kuma/scripts/kuma_push\.sh\b", c, re.M)


def test_push_script_static():
    sh = ROOT / "scripts" / "kuma_push.sh"
    t = sh.read_bytes().decode("utf-8")
    assert "\r" not in t
    assert t.startswith("#!/usr/bin/env bash")
    for needle in ("umask 077", "--max-time", "--resolve", "KUMA_PUSH_DRYRUN", "systemctl is-active",
                   "docker inspect", "logger"):
        assert needle in t, needle
    assert "docker.sock" not in t
    assert not re.search(r"tskey-|\b100\.\d+\.\d+\.\d+\b|\.ts\.net", t), "script must hold no real values"
    bash = shutil.which("bash")
    if bash:
        assert subprocess.run([bash, "-n", str(sh)], capture_output=True).returncode == 0


def test_seed_scripts_exist_and_hold_no_real_values():
    for n in ("seed_monitors.py", "kuma_apply.py", "monitors.py"):
        p = ROOT / "scripts" / n
        assert p.is_file(), n
        t = p.read_text(encoding="utf-8")
        assert not re.search(r"tskey-|\b100\.\d+\.\d+\.\d+\b|[a-z0-9-]+\.ts\.net", t.replace("example.ts.net", "")), n


def test_env_example_has_placeholders_only():
    t = (ROOT / ".env.example").read_text(encoding="utf-8")
    for k in FAKE_ENV:
        assert re.search(rf"^{k}=", t, re.M), k
    assert not re.search(r"\b100\.\d+\.\d+\.\d+\b", t)


def test_apply_is_idempotent_and_keeps_existing(tmp_path, spec):
    """kuma_apply on a tiny stand-in schema: second run adds nothing, owner's monitor and user stay."""
    import sqlite3
    import kuma_apply
    p = tmp_path / "k.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE monitor (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, active INT, user_id INT, type TEXT,
          interval INT, retry_interval INT, maxretries INT, hostname TEXT, port INT, url TEXT, timeout REAL,
          dns_resolve_server TEXT, dns_resolve_type TEXT, expiry_notification INT, push_token TEXT, parent INT);
        CREATE TABLE status_page (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE, title TEXT, icon TEXT, theme TEXT,
          published INT, search_engine_index INT, show_tags INT, show_powered_by INT, show_certificate_expiry INT);
        CREATE TABLE "group" (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, public INT, active INT, weight INT, status_page_id INT);
        CREATE TABLE monitor_group (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INT, group_id INT, weight INT);
        INSERT INTO user VALUES (7, 'owner');
        INSERT INTO monitor (name, active, user_id, type, interval) VALUES ('Mine', 1, 7, 'http', 20);
    """)
    con.commit()
    con.close()
    first = kuma_apply.apply(str(p), spec)
    assert len(first["created"]) == 3 + 12 + 1 and first["kept"] == []
    assert len(first["push"]) == 5 and all(len(t) == 32 for t in first["push"].values())
    second = kuma_apply.apply(str(p), spec)
    assert second["created"] == [] and second["push"] == first["push"]
    con = sqlite3.connect(p)
    assert con.execute("select count(*) from monitor").fetchone()[0] == 1 + 3 + 12
    assert con.execute("select interval from monitor where name='Mine'").fetchone()[0] == 20
    assert con.execute("select count(*) from monitor_group").fetchone()[0] == 12
    assert con.execute("select count(distinct user_id) from monitor").fetchone()[0] == 1
    assert con.execute("select * from user").fetchall() == [(7, "owner")]


def test_apply_refuses_without_owner_account(tmp_path, spec):
    import sqlite3
    import kuma_apply
    p = tmp_path / "k.db"
    sqlite3.connect(p).executescript("CREATE TABLE user (id INTEGER PRIMARY KEY); CREATE TABLE monitor (id INTEGER, name TEXT);")
    with pytest.raises(SystemExit):
        kuma_apply.apply(str(p), spec)


# ---------------------------------------------------------------- part B: live VPS


def sql(vps, query, db=DB):
    """Run one read-only query on the VPS and return rows as dicts."""
    out = vps.run(f"sudo sqlite3 -readonly -json {db} \"{query}\"", check=True)
    return json.loads(out) if out else []


def latest_backup(vps):
    """Newest pre-monitors backup path on the VPS (the backups dir is root-only, so glob as root)."""
    path = vps.run(f"sudo bash -c 'ls -1 {REMOTE}/backups/kuma-pre-monitors-*.db | tail -n 1'", check=False)
    assert path.endswith(".db"), "no pre-monitors backup found"
    return path


def test_backup_exists_mode_600(vps):
    out = vps.run(f"sudo find {REMOTE}/backups -maxdepth 1 -name 'kuma-pre-monitors-*.db' -printf '%m %u %s\\n'")
    lines = out.splitlines()
    assert lines, "no pre-monitors backup"
    for line in lines:
        mode, owner, size = line.split()
        assert mode == "600" and owner == "root" and int(size) > 100_000


def test_backup_is_intact(vps):
    out = vps.run(f"sudo sqlite3 -readonly '{latest_backup(vps)}' 'PRAGMA integrity_check;' 'select count(*) from user;'")
    assert out.splitlines() == ["ok", "1"]


def test_db_group_monitors_and_children(vps, spec):
    rows = sql(vps, "select id,name,type,parent,user_id,active,interval,maxretries from monitor")
    owner = sql(vps, "select id from user order by id limit 1")[0]["id"]
    by = {r["name"]: r for r in rows}
    for g in spec["groups"]:
        gr = by[g["name"]]
        assert gr["type"] == "group" and gr["parent"] is None and gr["user_id"] == owner and gr["active"] == 1
        for m in g["monitors"]:
            r = by[m["name"]]
            assert r["type"] == m["type"], m["name"]
            assert r["parent"] == gr["id"], m["name"]
            assert r["user_id"] == owner and r["active"] == 1
            assert r["interval"] == 60 and r["maxretries"] == 3


def test_db_push_tokens_are_set_and_unique(vps):
    rows = sql(vps, "select push_token from monitor where type='push'")
    toks = [r["push_token"] for r in rows]
    assert len(toks) == 5 and len(set(toks)) == 5
    assert all(t and len(t) == 32 for t in toks)


def test_db_status_page(vps, spec):
    sp = sql(vps, "select id,slug,title,published,icon from status_page where slug='vps'")
    assert len(sp) == 1 and sp[0]["title"] == "VPS" and sp[0]["published"] == 1
    groups = sql(vps, f"select id,name,public from \\\"group\\\" where status_page_id={sp[0]['id']} order by weight")
    assert [g["name"] for g in groups] == GROUPS
    assert all(g["public"] == 1 for g in groups)
    for g, sg in zip(groups, spec["groups"]):
        n = sql(vps, f"select count(*) as n from monitor_group where group_id={g['id']}")[0]["n"]
        assert n == len(sg["monitors"]), g["name"]


def test_owner_user_row_untouched(vps):
    live = vps.run(f"sudo sqlite3 -readonly {DB} 'select id,username,password,twofa_secret,twofa_status from user' | sha256sum | cut -d' ' -f1")
    old = vps.run(f"sudo sqlite3 -readonly '{latest_backup(vps)}' 'select id,username,password,twofa_secret,twofa_status from user' | sha256sum | cut -d' ' -f1")
    if live != old:
        pytest.skip("owner changed the account after seeding (for example turned on 2FA)")


def test_push_cron_installed_on_vps(vps):
    out = vps.run("sudo stat -c '%U:%G %a' /etc/cron.d/uptime-kuma-push && sudo grep -c $'\\r' /etc/cron.d/uptime-kuma-push || true")
    assert out.splitlines()[0] == "root:root 644"
    assert not any(l.strip() == "1" for l in out.splitlines()[1:])
    assert "kuma_push.sh" in vps.run("sudo cat /etc/cron.d/uptime-kuma-push")


def test_push_conf_mode_600_and_dry_run_all_pass(vps):
    assert vps.run("sudo stat -c '%U:%G %a' /etc/uptime-kuma-push.env") == "root:root 600"
    out = vps.run(f"sudo KUMA_PUSH_DRYRUN=1 {REMOTE}/scripts/kuma_push.sh")
    lines = out.splitlines()
    assert len(lines) == 5 and all(l.endswith(" pass") for l in lines), lines


def test_push_script_does_not_push_when_check_fails(vps):
    out = vps.run(f"sudo KUMA_PUSH_DRYRUN=1 DISK_MAX=0 MEM_MAX=0 {REMOTE}/scripts/kuma_push.sh")
    status = dict(l.rsplit(" ", 1) for l in out.splitlines())
    fails = [k for k, v in status.items() if v == "fail"]
    assert len(fails) == 2, status
    assert sum(1 for v in status.values() if v == "pass") == 3


def test_containers_still_running(vps):
    out = vps.run("docker ps --format '{{.Names}}'").split()
    for n in ("uptime-kuma", "ts-uptime-kuma", "vn-stock-scanner"):
        assert n in out, n
    assert vps.run("systemctl is-active uosserver hermes-agent").split() == ["active", "active"]


def _heartbeats(vps, env, fqdn, ip):
    """Fetch the public status page JSON through the VPS host (curl --resolve), so no PC VPN is needed."""
    def get(path):
        out = vps.run(f"curl -sS --max-time 20 --resolve {fqdn}:443:{ip} https://{fqdn}{path}", check=False)
        return json.loads(out)
    return get("/api/status-page/vps"), get("/api/status-page/heartbeat/vps")


def test_status_page_all_monitors_up(vps, env):
    fqdn, ip = env["KUMA_FQDN"], env["KUMA_TS_IP"]
    deadline = time.time() + 420
    last = "no data"
    while time.time() < deadline:
        try:
            page, hb = _heartbeats(vps, env, fqdn, ip)
            ids = [m["id"] for g in page["publicGroupList"] for m in g["monitorList"]]
            lists = hb["heartbeatList"]
            states = {i: (lists.get(str(i)) or [{}])[-1].get("status") for i in ids}
            last = f"{sum(1 for s in states.values() if s == 1)}/{len(ids)} up"
            if len(ids) == 12 and all(s == 1 for s in states.values()):
                assert [g["name"] for g in page["publicGroupList"]] == GROUPS
                return
        except Exception as e:  # not ready yet
            last = f"error {type(e).__name__}"
        time.sleep(20)
    pytest.fail(f"status page not all UP: {last}")


def test_status_page_reachable_from_pc(env):
    try:
        r = requests.get(f"https://{env['KUMA_FQDN']}/api/status-page/heartbeat/vps", timeout=30)
    except requests.RequestException:
        pytest.skip("PC cannot reach the tailnet (Tailscale off or exit node)")
    assert r.status_code == 200
    data = r.json()["heartbeatList"]
    assert len(data) == 12
    assert all((v or [{}])[-1].get("status") == 1 for v in data.values())
