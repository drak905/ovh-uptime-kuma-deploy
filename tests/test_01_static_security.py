"""Tier 1: static checks on the repo. No VPS needed, except the real-value leak scan."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from conftest import ROOT, git_files

COMPOSE = ROOT / "docker" / "compose.yaml"
SERVE = ROOT / "docker" / "ts-config" / "ts-serve.json"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _svc(compose):
    return compose["services"]["uptime-kuma"], compose["services"]["ts-uptime-kuma"]


def test_compose_name_and_services(compose):
    assert compose["name"] == "uptime-kuma"
    assert set(compose["services"]) == {"uptime-kuma", "ts-uptime-kuma"}


def test_images_pinned_exact(compose):
    kuma, ts = _svc(compose)
    assert re.fullmatch(r"louislam/uptime-kuma:2\.\d+\.\d+-slim", kuma["image"])
    assert re.fullmatch(r"tailscale/tailscale:v\d+\.\d+\.\d+", ts["image"])


def test_no_published_ports(compose):
    for name, s in compose["services"].items():
        assert "ports" not in s, f"{name} publishes ports"


def test_sidecar_network_and_ordering(compose):
    kuma, ts = _svc(compose)
    assert kuma["network_mode"] == "service:ts-uptime-kuma"
    assert kuma["depends_on"]["ts-uptime-kuma"]["condition"] == "service_healthy"
    assert "healthcheck" in ts and "healthcheck" in kuma


def test_least_privilege(compose):
    for name, s in compose["services"].items():
        assert not s.get("privileged"), name
        assert s.get("network_mode") != "host", name
        assert s.get("pid") != "host", name
    kuma, ts = _svc(compose)
    assert ts["cap_add"] == ["NET_ADMIN"]
    assert "cap_add" not in kuma


def test_no_docker_socket(compose):
    assert "docker.sock" not in COMPOSE.read_text(encoding="utf-8")


def test_kuma_environment(compose):
    kuma, ts = _svc(compose)
    assert kuma["environment"]["UPTIME_KUMA_HOST"] == "127.0.0.1"
    assert kuma["environment"]["UPTIME_KUMA_DB_TYPE"] == "sqlite"
    assert str(ts["environment"]["TS_ACCEPT_DNS"]).lower() == "true"
    assert str(ts["environment"]["TS_USERSPACE"]).lower() == "false"
    assert "UK_INITIAL_ADMIN_USER" not in kuma["environment"]


def test_resource_and_log_limits(compose):
    kuma, ts = _svc(compose)
    assert kuma["mem_limit"] == "1g"
    assert ts["mem_limit"]
    for s in (kuma, ts):
        assert s["restart"] == "unless-stopped"
        assert s["logging"]["options"]["max-size"] and s["logging"]["options"]["max-file"]


def test_serve_config_directory_mount(compose):
    _, ts = _svc(compose)
    assert "./ts-config:/config:ro" in ts["volumes"]
    assert not any(v.endswith("ts-serve.json:/config/ts-serve.json:ro") for v in ts["volumes"])
    assert ts["environment"]["TS_SERVE_CONFIG"] == "/config/ts-serve.json"


def test_serve_json_proxies_to_loopback_and_never_funnel():
    cfg = json.loads(SERVE.read_text(encoding="utf-8"))
    assert cfg["TCP"]["443"]["HTTPS"] is True
    handlers = next(iter(cfg["Web"].values()))["Handlers"]
    assert handlers["/"]["Proxy"] == "http://127.0.0.1:3001"

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "AllowFunnel":
                    vals = list(v.values()) if isinstance(v, dict) else [v]
                    assert all(x is False for x in vals), "AllowFunnel must never be true"
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(cfg)
    assert "AllowFunnel" in cfg


def test_gitignore_blocks_secrets_and_state():
    probes = [".env", ".env.local", "a.key", "a.pem", "kuma.db", "kuma.db-wal", "ts-state/x", "backups/x",
              "data/x", "reports/x", "x.tar.gz", "tailscale-state/x"]
    for p in probes:
        r = subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT)
        assert r.returncode == 0, f"{p} is not ignored"
    r = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT)
    assert r.returncode == 1, ".env.example must stay tracked"


def test_env_never_tracked():
    names = {p.name for p in git_files()}
    assert ".env" not in names


def test_line_endings_lf_and_gitattributes():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "eol=lf" in attrs
    cfg = subprocess.run(["git", "config", "--local", "core.autocrlf"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert cfg == "false"
    bad = []
    for p in list(ROOT.glob("docker/**/*")) + list(ROOT.glob("scripts/*")) + list(ROOT.glob("*.toml")):
        if p.is_file() and p.suffix in {".sh", ".yaml", ".json", ".cron", ".py", ".toml"}:
            if b"\r" in p.read_bytes():
                bad.append(p.name)
    assert not bad, f"CR found in {bad}"


def test_scripts_syntax_and_backup_hardening():
    sh = ROOT / "scripts" / "backup_kuma.sh"
    text = sh.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    for needle in ("umask 077", "mktemp -d", "trap ", "integrity_check", "flock", "-mtime", ".backup", "chmod 600"):
        assert needle in text, needle
    assert "/tmp/kuma-backup.db" not in text
    bash = shutil.which("bash")
    if bash:
        r = subprocess.run([bash, "-n", str(sh)], capture_output=True)
        assert r.returncode == 0


def test_cron_file():
    c = (ROOT / "scripts" / "uptime-kuma-backup.cron").read_text(encoding="utf-8")
    assert re.search(r"^15 3 \* \* \* root /opt/uptime-kuma/scripts/backup_kuma\.sh", c, re.M)
    assert c.endswith("\n")


def test_no_custom_domain_leftovers():
    for p in ROOT.rglob("*"):
        if ".git" in p.parts or ".venv" in p.parts:
            continue
        assert "caddy" not in p.name.lower(), p.name


def _gitleaks(*args):
    exe = shutil.which("gitleaks")
    if not exe:
        pytest.skip("gitleaks not installed")
    return subprocess.run([exe, *args, "--no-banner", "--redact", "-c", str(ROOT / ".gitleaks.toml")],
                          cwd=ROOT, capture_output=True, text=True)


def test_gitleaks_working_tree_clean():
    r = _gitleaks("dir", ".")
    assert r.returncode == 0, "gitleaks found something in the working tree"


def test_gitleaks_history_clean():
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip("no commits yet")
    r = _gitleaks("git", ".")
    assert r.returncode == 0, "gitleaks found something in history"


def test_gitleaks_rules_catch_sample_leaks():
    """Positive control: strings are built at run time so this file itself stays clean."""
    samples = [
        "tskey-" + "auth-" + "kABCDEF1234567-" + "abcdefghijklmnopqrstuvwxyz",
        "host: tail" + "1234.ts" + ".net",
        "ip: 100." + "101.12.34",
        "ip6: fd7a:" + "115c:a1e0:ab12::1",
    ]
    exe = shutil.which("gitleaks")
    if not exe:
        pytest.skip("gitleaks not installed")
    for s in samples:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.txt").write_text(s + "\n", encoding="utf-8")
            r = subprocess.run([exe, "dir", d, "--no-banner", "--redact", "-c", str(ROOT / ".gitleaks.toml")],
                               capture_output=True, text=True)
            assert r.returncode == 1, "rule did not fire for a sample"


def test_gitleaks_allows_placeholders():
    exe = shutil.which("gitleaks")
    if not exe:
        pytest.skip("gitleaks not installed")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "x.txt").write_text("https://<tailnet>.ts" + ".net and https://example.ts" + ".net and 100.100.100.100\n", encoding="utf-8")
        r = subprocess.run([exe, "dir", d, "--no-banner", "--redact", "-c", str(ROOT / ".gitleaks.toml")],
                           capture_output=True, text=True)
        assert r.returncode == 0


def test_real_values_absent_from_tracked_files(env, vps):
    """Real VPS IP, tailnet suffix and account name must not appear in any tracked file."""
    needles = {"vps_ip": env.get("OVH_VPS_IP", "")}
    try:
        st = json.loads(vps.run("tailscale status --json", timeout=40))
        needles["tailnet_suffix"] = (st.get("MagicDNSSuffix") or "").strip()
        needles["tailnet_name"] = ((st.get("CurrentTailnet") or {}).get("Name") or "").strip()
        uid = str((st.get("Self") or {}).get("UserID", ""))
        needles["account"] = (((st.get("User") or {}).get(uid) or {}).get("LoginName") or "").strip()
        needles["host_ip"] = next(iter((st.get("Self") or {}).get("TailscaleIPs") or []), "")
    except Exception:
        pytest.skip("VPS not reachable, could only check the IP")
    needles = {k: v for k, v in needles.items() if len(v) >= 4}
    assert needles.get("vps_ip"), "no VPS IP to check"
    hits = []
    for p in git_files():
        if not p.is_file():
            continue
        data = p.read_text(encoding="utf-8", errors="ignore").lower()
        for k, v in needles.items():
            if v.lower() in data:
                hits.append(f"{p.name}:{k}")
    assert not hits, f"real values found: {hits}"
