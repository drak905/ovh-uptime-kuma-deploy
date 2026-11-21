"""Tier 5: backup script, cron entry, outbound probes from inside Kuma."""
import json

from conftest import REMOTE


def test_backup_script_installed_root_owned(vps):
    assert vps.run(f"stat -c '%U %a' {REMOTE}/scripts/backup_kuma.sh") in {"root 755", "root 750", "root 700"}
    vps.run(f"bash -n {REMOTE}/scripts/backup_kuma.sh")


def test_backup_runs_and_archive_is_valid(vps):
    path = vps.run(f"sudo {REMOTE}/scripts/backup_kuma.sh", timeout=120).splitlines()[-1]
    assert path.startswith(f"{REMOTE}/backups/uptime-kuma-") and path.endswith(".tar.gz")
    assert vps.run(f"sudo stat -c %a {path}") == "600"
    vps.run(f"sudo tar -tzf {path}")
    chk = vps.run(f"d=$(mktemp -d) && sudo tar -xzf {path} -C $d && sudo sqlite3 $d/kuma.db 'PRAGMA integrity_check;'; sudo rm -rf $d")
    assert chk.splitlines()[0] == "ok"


def test_backup_dir_private_and_no_temp_leftovers(vps):
    assert vps.run(f"sudo stat -c %a {REMOTE}/backups") in {"700", "750"}
    assert vps.run(f"sudo ls {REMOTE}/backups | grep -c partial ; true").splitlines()[-1] == "0"


def test_backup_lock_prevents_overlap(vps):
    rc = vps.run(f"sudo bash -c 'exec 9>{REMOTE}/backups/.backup.lock; flock -n 9 && {REMOTE}/scripts/backup_kuma.sh >/dev/null 2>&1; echo $?' ; true")
    # holding the lock ourselves then running the script inside the lock cannot succeed twice: script must fail with 1
    assert rc.splitlines()[-1] == "1"


def test_cron_entry_installed(vps):
    assert vps.run("stat -c '%U %a' /etc/cron.d/uptime-kuma-backup") == "root 644"
    body = vps.run("cat /etc/cron.d/uptime-kuma-backup")
    assert "\r" not in body
    assert "15 3 * * * root /opt/uptime-kuma/scripts/backup_kuma.sh" in body


def node_fetch(vps, url):
    js = f"fetch('{url}',{{signal:AbortSignal.timeout(20000)}}).then(r=>console.log(r.status)).catch(e=>console.log('ERR '+(e.cause&&e.cause.code||e.message)))"
    return vps.run(f"docker exec uptime-kuma node -e \"{js}\"", timeout=60)


def test_outbound_ip_probe(vps):
    assert node_fetch(vps, "https://1.1.1.1/") in {"200", "301", "302"}


def test_outbound_dns_name_probe(vps):
    assert node_fetch(vps, "https://example.com/") == "200"


def test_tailnet_name_resolves_from_kuma(vps):
    host = json.loads(vps.run("tailscale status --json"))["Self"]["DNSName"].rstrip(".")
    js = f"require('dns').lookup('{host}',(e,a)=>console.log(e?'ERR '+e.code:a))"
    out = vps.run(f"docker exec uptime-kuma node -e \"{js}\"")
    assert out.startswith("100.")


def test_tailnet_https_probe_from_kuma(vps):
    host = json.loads(vps.run("tailscale status --json"))["Self"]["DNSName"].rstrip(".")
    assert node_fetch(vps, f"https://{host}/") in {"200", "302", "401", "404"}
