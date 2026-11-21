"""Fixtures. Everything real (IP, key, tailnet) comes from .env or the VPS at run time.

Assertion messages must not echo IPs, tailnet names or keys.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from vps import Vps, load_env  # noqa: E402

REMOTE = "/opt/uptime-kuma"
COMPOSE = f"docker compose -p uptime-kuma --env-file {REMOTE}/.env -f {REMOTE}/docker/compose.yaml"


@pytest.fixture(scope="session")
def env():
    return load_env(ROOT / ".env")


@pytest.fixture(scope="session")
def vps(env):
    if "OVH_VPS_IP" not in env or "OVH_SSH_KEY_PATH" not in env:
        pytest.fail("missing OVH_* keys in .env")
    return Vps.from_env(env)


@pytest.fixture(scope="session")
def compose_cmd():
    return COMPOSE


def git_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
    return [ROOT / p for p in out.splitlines() if p]


def pc_tailscale():
    """Return the PC `tailscale status --json` dict, or None when unavailable."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=20)
        return json.loads(out.stdout)
    except Exception:
        return None


@pytest.fixture(scope="session")
def sidecar_status(vps):
    """tailscale status --json from inside the sidecar (empty dict if it is not running)."""
    out = vps.run("docker exec ts-uptime-kuma tailscale status --json", check=False)
    try:
        return json.loads(out)
    except ValueError:
        return {}


@pytest.fixture(scope="session")
def kuma_fqdn_remote(sidecar_status):
    name = (sidecar_status.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not name:
        pytest.fail("sidecar has no tailnet DNS name")
    return name


@pytest.fixture(scope="session")
def kuma_fqdn(kuma_fqdn_remote):
    return kuma_fqdn_remote


@pytest.fixture(scope="session")
def kuma_ip(sidecar_status):
    ips = (sidecar_status.get("Self") or {}).get("TailscaleIPs") or []
    v4 = [i for i in ips if re.match(r"^\d+\.\d+\.\d+\.\d+$", i)]
    if not v4:
        pytest.fail("sidecar has no tailnet IPv4")
    return v4[0]
