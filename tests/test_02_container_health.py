"""Tier 2: the deployed stack on the VPS, and the apps that must keep running."""
import json
import re

import pytest

REMOTE = "/opt/uptime-kuma"


def inspect(vps, name):
    out = vps.run(f"docker inspect {name}", check=False)
    try:
        return json.loads(out)[0]
    except (ValueError, IndexError):
        pytest.fail(f"container {name} not found")


@pytest.mark.parametrize("name", ["ts-uptime-kuma", "uptime-kuma"])
def test_container_running_healthy(vps, name):
    c = inspect(vps, name)
    assert c["State"]["Status"] == "running"
    assert c["State"]["Health"]["Status"] == "healthy"
    assert c["RestartCount"] <= 1
    assert c["HostConfig"]["Privileged"] is False


def test_compose_project_name(vps):
    out = vps.run("docker inspect -f '{{index .Config.Labels \"com.docker.compose.project\"}}' uptime-kuma ts-uptime-kuma")
    assert set(out.split()) == {"uptime-kuma"}


def test_data_mount_on_local_disk(vps):
    c = inspect(vps, "uptime-kuma")
    m = [x for x in c["Mounts"] if x["Destination"] == "/app/data"]
    assert m and m[0]["Source"] == f"{REMOTE}/data"
    fstype = vps.run(f"stat -f -c %T {REMOTE}/data")
    assert fstype not in {"nfs", "nfs4", "cifs", "smb2", "fuseblk"}


def test_no_docker_socket_mounted(vps):
    for n in ("uptime-kuma", "ts-uptime-kuma"):
        assert all("docker.sock" not in x["Source"] for x in inspect(vps, n)["Mounts"])


def test_sidecar_online_with_expected_name(vps, sidecar_status, env):
    assert sidecar_status.get("BackendState") == "Running"
    me = sidecar_status["Self"]
    assert me["Online"] is True
    assert me["HostName"] == env.get("TS_HOSTNAME", "uptime-kuma")
    host = vps.run("tailscale status --json")
    assert json.loads(host)["Self"]["HostName"] != me["HostName"]


def test_sidecar_serve_maps_https_to_kuma(vps):
    cfg = json.loads(vps.run("docker exec ts-uptime-kuma tailscale serve status --json"))
    assert cfg["TCP"]["443"]["HTTPS"] is True
    proxies = [h["Proxy"] for w in cfg["Web"].values() for h in w["Handlers"].values()]
    assert proxies == ["http://127.0.0.1:3001"]
    assert not cfg.get("AllowFunnel") or not any(cfg["AllowFunnel"].values())


def test_kuma_answers_inside_namespace(vps):
    code = vps.run("docker exec uptime-kuma node -e \"fetch('http://127.0.0.1:3001/').then(r=>console.log(r.status))\"")
    assert code in {"200", "302"}


def test_kuma_page_over_tailnet_tls(vps, kuma_fqdn, kuma_ip):
    """Real TLS check from the VPS: the host reaches the sidecar tailnet IP, certificate verified by name."""
    out = vps.run(f"curl -sS --max-time 40 --retry 3 --retry-delay 5 --resolve {kuma_fqdn}:443:{kuma_ip} "
                  f"-o /dev/null -w '%{{http_code}} %{{ssl_verify_result}}' https://{kuma_fqdn}/")
    code, verify = out.split()
    assert code in {"200", "302"}
    assert verify == "0"


def test_kuma_page_content_is_kuma(vps, kuma_fqdn, kuma_ip):
    body = vps.run(f"curl -sSL --max-time 40 --resolve {kuma_fqdn}:443:{kuma_ip} https://{kuma_fqdn}/")
    assert "Uptime Kuma" in body or "uptime-kuma" in body.lower()


def test_socketio_open_packet_over_tailnet(vps, kuma_fqdn, kuma_ip):
    body = vps.run(f"curl -sS --max-time 40 --resolve {kuma_fqdn}:443:{kuma_ip} "
                   f"'https://{kuma_fqdn}/socket.io/?EIO=4&transport=polling'")
    assert body.startswith("0{")


# ---- Isolation: nothing on the host, nothing public ----

def test_nothing_listens_on_3001_on_host(vps):
    assert vps.run("sudo ss -tlnH | grep -c ':3001 ' ; true").strip() == "0"


def test_no_published_ports(vps):
    assert vps.run("docker port ts-uptime-kuma; docker port uptime-kuma; true") == ""


def test_kuma_not_reachable_on_bridge_ip(vps):
    ip = vps.run("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ts-uptime-kuma")
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip)
    rc = vps.run(f"curl -sS --max-time 5 -o /dev/null http://{ip}:3001/ ; echo $?").splitlines()[-1]
    assert rc != "0"


def test_kuma_not_reachable_on_tailnet_ip_plain_http(vps, kuma_ip):
    rc = vps.run(f"curl -sS --max-time 5 -o /dev/null http://{kuma_ip}:3001/ ; echo $?").splitlines()[-1]
    assert rc != "0"


# ---- Coexistence: existing workloads untouched ----

def test_host_serve_unchanged(vps):
    baseline = json.loads(vps.run(f"sudo cat {REMOTE}/baseline/host-serve.json"))
    now = json.loads(vps.run("sudo tailscale serve status --json"))
    assert now == baseline
    assert "443" in now["TCP"] and "3389" in now["TCP"]
    assert "8443" not in now["TCP"]


def test_existing_apps_running(vps):
    assert "vn-stock-scanner" in vps.run("docker ps --format '{{.Names}}'").split()
    assert vps.run("systemctl is-active hermes-agent") == "active"
    assert vps.run("systemctl is-active uosserver") == "active"
    assert vps.run("systemctl is-active tailscaled") == "active"


def test_docker_daemon_json_still_absent(vps):
    assert vps.run("test -e /etc/docker/daemon.json && echo yes || echo no") == "no"


def test_host_node_name_unchanged(vps):
    base = vps.run(f"sudo cat {REMOTE}/baseline/host-name.txt")
    assert json.loads(vps.run("tailscale status --json"))["Self"]["HostName"] == base
