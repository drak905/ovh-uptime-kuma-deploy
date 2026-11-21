"""The monitor plan: groups, monitors and the status page. Real targets come from .env.

Pure data, no I/O, so tests can check it offline. Kuma column names are used as keys
(`interval`, `maxretries`, ...), so kuma_apply.py can insert each dict as one row.
"""

INTERVAL = 60
RETRIES = 3
REQUIRED = ("VPS_TS_IP", "VPS_TS_FQDN", "KUMA_FQDN", "KUMA_TS_IP", "ARCANE_FQDN")


def _m(name, type_, **kw):
    base = {"name": name, "type": type_, "interval": INTERVAL, "retry_interval": INTERVAL, "maxretries": RETRIES}
    base.update(kw)
    return base


def _push(name, key):
    return _m(name, "push", push_key=key)


def build_spec(env: dict) -> dict:
    """Return {"groups": [...], "status_page": {...}}. Raises KeyError when a .env key is missing."""
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise KeyError(", ".join(missing))
    ip, vps_host, arcane_host = env["VPS_TS_IP"], env["VPS_TS_FQDN"], env["ARCANE_FQDN"]
    return {
        "groups": [
            {"name": "VPS", "monitors": [
                _m("VPS ping", "ping", hostname=ip, timeout=10),
                _m("VPS SSH (TCP 22)", "port", hostname=ip, port=22, timeout=48),
                _m("VPS RDP (TCP 3389, tailnet)", "port", hostname=ip, port=3389, timeout=48),
                _m("Outbound HTTP (google.com)", "http", url="https://www.google.com", timeout=48),
                _m("DNS lookup (google.com)", "dns", hostname="google.com", port=53,
                   dns_resolve_server="1.1.1.1", dns_resolve_type="A", timeout=48),
                _push("Disk below 90%", "PUSH_DISK"),
                _push("Memory below 90%", "PUSH_MEM"),
            ]},
            {"name": "UniFi", "monitors": [
                _m("UniFi console (HTTPS)", "http", url=f"https://{vps_host}", expiry_notification=1, timeout=48),
                _m("UniFi inform port (TCP 8080)", "port", hostname=ip, port=8080, timeout=48),
                _push("uosserver active", "PUSH_UOSSERVER"),
            ]},
            {"name": "Apps", "monitors": [
                _push("hermes-agent active", "PUSH_HERMES"),
                _push("vn-stock-scanner container running", "PUSH_SCANNER"),
                _m("Arcane (HTTPS /api/health)", "http", url=f"https://{arcane_host}/api/health",
                   accepted_statuscodes_json='["200"]', expiry_notification=1, timeout=48),
                _push("arcane containers running", "PUSH_ARCANE"),
            ]},
        ],
        "status_page": {"slug": "vps", "title": "VPS"},
    }
