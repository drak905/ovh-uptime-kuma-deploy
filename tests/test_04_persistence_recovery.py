"""Tier 4: restarts and database integrity."""
import time

import pytest

from conftest import COMPOSE, REMOTE


def wait_healthy(vps, names, secs):
    end = time.time() + secs
    while time.time() < end:
        out = vps.run("docker inspect -f '{{.State.Health.Status}}' " + " ".join(names), check=False).split()
        if len(out) == len(names) and all(x == "healthy" for x in out):
            return True
        time.sleep(3)
    return False


def https_ok(vps, fqdn, ip, secs=60):
    end = time.time() + secs
    while time.time() < end:
        out = vps.run(f"curl -sS --max-time 15 --resolve {fqdn}:443:{ip} -o /dev/null -w '%{{http_code}}' https://{fqdn}/", check=False)
        if out.endswith("200") or out.endswith("302"):
            return True
        time.sleep(3)
    return False


def test_restart_kuma_recovers(vps, kuma_fqdn, kuma_ip):
    vps.run(f"{COMPOSE} restart uptime-kuma", timeout=90)
    assert wait_healthy(vps, ["uptime-kuma"], 90)
    assert https_ok(vps, kuma_fqdn, kuma_ip)


def test_force_recreate_both_recovers(vps, kuma_fqdn, kuma_ip):
    """The documented upgrade path: recreate both so the shared namespace is fresh."""
    vps.run(f"{COMPOSE} up -d --force-recreate", timeout=180)
    assert wait_healthy(vps, ["ts-uptime-kuma", "uptime-kuma"], 150)
    assert https_ok(vps, kuma_fqdn, kuma_ip, 90)


def test_node_identity_survives_recreate(vps, env):
    import json
    st = json.loads(vps.run("docker exec ts-uptime-kuma tailscale status --json"))
    assert st["Self"]["HostName"] == env.get("TS_HOSTNAME", "uptime-kuma")
    assert st["BackendState"] == "Running"


def test_sqlite_integrity(vps):
    assert vps.run(f"sudo test -f {REMOTE}/data/kuma.db && echo yes || echo no") == "yes"
    assert vps.run(f"sudo sqlite3 {REMOTE}/data/kuma.db 'PRAGMA integrity_check;'") == "ok"


def test_ts_state_persisted_and_private(vps):
    assert vps.run(f"sudo stat -c %a {REMOTE}/ts-state") in {"700", "750"}
    assert int(vps.run(f"sudo find {REMOTE}/ts-state -type f | wc -l")) > 0


def test_auth_key_removed_after_first_login(vps):
    """Run after the first successful login. The deploy script drops the key from the VPS .env."""
    assert vps.run(f"grep -c '^TS_AUTHKEY=.' {REMOTE}/.env; true").splitlines()[-1] == "0"
    assert vps.run(f"stat -c %a {REMOTE}/.env") == "600"
