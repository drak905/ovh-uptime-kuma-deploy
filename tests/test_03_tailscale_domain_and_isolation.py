"""Tier 3: from the PC, over Tailscale, plus a public-WAN negative check.

Skips with a reason when the PC is not on the tailnet or uses an exit node.
The tests never change the PC's Tailscale settings.
"""
import ipaddress
import socket

import pytest
import requests
import websocket

from conftest import pc_tailscale


@pytest.fixture(scope="module")
def pc():
    st = pc_tailscale()
    if not st or st.get("BackendState") != "Running":
        pytest.skip("PC Tailscale is not Running; start it and re-run tier 3")
    if st.get("ExitNodeStatus") or any(p.get("ExitNode") for p in (st.get("Peer") or {}).values()):
        pytest.skip("PC uses an exit node; run `tailscale set --exit-node=` and re-run tier 3")
    return st


@pytest.fixture
def kuma_fqdn(pc, request):
    """Resolved after `pc`, so a stopped PC Tailscale skips instead of erroring."""
    return request.getfixturevalue("kuma_fqdn_remote")


def test_domain_resolves_to_cgnat(pc, kuma_fqdn):
    ip = ipaddress.ip_address(socket.gethostbyname(kuma_fqdn))
    assert ip in ipaddress.ip_network("100." + "64.0.0/10")


def test_https_valid_certificate(pc, kuma_fqdn):
    r = requests.get(f"https://{kuma_fqdn}/", timeout=45, verify=True)
    assert r.status_code == 200
    assert "kuma" in r.text.lower()


def test_websocket_handshake_first_frame(pc, kuma_fqdn):
    ws = websocket.create_connection(f"wss://{kuma_fqdn}/socket.io/?EIO=4&transport=websocket", timeout=30)
    try:
        assert ws.recv().startswith("0{")
    finally:
        ws.close()


@pytest.mark.parametrize("port", [3001])
def test_public_ip_port_closed(pc, env, port):
    s = socket.socket()
    s.settimeout(8)
    try:
        rc = s.connect_ex((env["OVH_VPS_IP"], port))
    finally:
        s.close()
    assert rc != 0, "public port is open"
