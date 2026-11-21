"""Tier 7: one Telegram notification (same bot and chat as Hermes Agent), default, on every monitor.

Part A is offline (config builder, insert script on a stand-in database, no token in tracked files).
Part B reads the VPS. Assertion messages never echo the bot token, the chat id, IPs or tailnet names.
The bot is only ever used for sendMessage (through Kuma) and getMe: never getUpdates or a webhook.
"""
import json
import re
import sqlite3

import pytest

from conftest import ROOT, REMOTE, git_files

import kuma_notify  # scripts/kuma_notify.py (conftest puts scripts/ on sys.path)

DB = f"{REMOTE}/data/kuma.db"
NAME = "Telegram (Hermes chat)"
FAKE_TOKEN = "123456789:" + "A" * 35  # not a real token, built here so no token-like text is in the file
FAKE_CHAT = "987654321"
TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")

# The real Kuma v2 schema of the three tables the script touches (sqlite3 .schema on the VPS).
SCHEMA = """
CREATE TABLE user (id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, username VARCHAR(255), password VARCHAR(255));
CREATE TABLE monitor (id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, name VARCHAR(150), user_id INTEGER);
CREATE TABLE [notification](
  [id] INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
  [name] VARCHAR(255),
  [active] BOOLEAN NOT NULL DEFAULT 1,
  [user_id] INTEGER NOT NULL, is_default BOOLEAN default 0 NOT NULL, config TEXT);
CREATE TABLE [monitor_notification](
  [id] INTEGER PRIMARY KEY NOT NULL,
  [monitor_id] INTEGER NOT NULL REFERENCES [monitor]([id]) ON DELETE CASCADE ON UPDATE CASCADE,
  [notification_id] INTEGER NOT NULL REFERENCES [notification]([id]) ON DELETE CASCADE ON UPDATE CASCADE);
CREATE INDEX [monitor_notification_index] ON [monitor_notification]([monitor_id], [notification_id]);
"""


def make_db(path, monitors=3, with_user=True):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    if with_user:
        con.execute("INSERT INTO user (username, password) VALUES ('owner', 'hash-1')")
    for i in range(monitors):
        con.execute("INSERT INTO monitor (name, user_id) VALUES (?, 1)", (f"m{i}",))
    con.commit()
    con.close()


def rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- part A: offline

def test_build_config_matches_kuma_telegram_provider():
    cfg = kuma_notify.build_config(NAME, FAKE_TOKEN, FAKE_CHAT)
    assert cfg["type"] == "telegram"
    assert cfg["name"] == NAME
    assert cfg["telegramBotToken"] == FAKE_TOKEN
    assert cfg["telegramChatID"] == FAKE_CHAT
    assert cfg["isDefault"] is True
    assert cfg["active"] is True
    assert cfg["applyExisting"] is False  # one-time flag, Kuma never stores it as true
    assert cfg["telegramSendSilently"] is False


@pytest.mark.parametrize("token,chat", [("", FAKE_CHAT), (FAKE_TOKEN, ""), ("not-a-token", FAKE_CHAT),
                                        (FAKE_TOKEN, "abc")])
def test_build_config_rejects_bad_input(token, chat):
    with pytest.raises(ValueError) as e:
        kuma_notify.build_config(NAME, token, chat)
    assert token not in str(e.value) or token == ""


def test_apply_inserts_default_active_notification_linked_to_all(tmp_path):
    db = str(tmp_path / "kuma.db")
    make_db(db, monitors=3)
    out = kuma_notify.apply(db, kuma_notify.build_config(NAME, FAKE_TOKEN, FAKE_CHAT))
    assert out == {"created": True, "linked": 3, "monitors": 3}
    n = rows(db, "SELECT name, active, is_default, user_id, config FROM notification")
    assert len(n) == 1
    assert n[0][:4] == (NAME, 1, 1, 1)
    cfg = json.loads(n[0][4])
    assert cfg["type"] == "telegram" and cfg["telegramBotToken"] == FAKE_TOKEN
    assert rows(db, "SELECT COUNT(DISTINCT monitor_id) FROM monitor_notification")[0][0] == 3


def test_apply_is_idempotent_and_links_new_monitors(tmp_path):
    db = str(tmp_path / "kuma.db")
    make_db(db, monitors=2)
    cfg = kuma_notify.build_config(NAME, FAKE_TOKEN, FAKE_CHAT)
    kuma_notify.apply(db, cfg)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO monitor (name, user_id) VALUES ('later', 1)")
    con.commit()
    con.close()
    out = kuma_notify.apply(db, cfg)
    assert out == {"created": False, "linked": 1, "monitors": 3}
    assert rows(db, "SELECT COUNT(*) FROM notification")[0][0] == 1
    assert rows(db, "SELECT COUNT(*) FROM monitor_notification")[0][0] == 3


def test_apply_leaves_user_table_alone(tmp_path):
    db = str(tmp_path / "kuma.db")
    make_db(db)
    before = rows(db, "SELECT * FROM user")
    kuma_notify.apply(db, kuma_notify.build_config(NAME, FAKE_TOKEN, FAKE_CHAT))
    assert rows(db, "SELECT * FROM user") == before


def test_apply_needs_an_admin_account(tmp_path):
    db = str(tmp_path / "kuma.db")
    make_db(db, with_user=False)
    with pytest.raises(SystemExit):
        kuma_notify.apply(db, kuma_notify.build_config(NAME, FAKE_TOKEN, FAKE_CHAT))
    assert rows(db, "SELECT COUNT(*) FROM notification")[0][0] == 0


def test_no_bot_token_pattern_in_tracked_files():
    hits = []
    for p in git_files():
        if p.is_file() and TOKEN_RE.search(p.read_text(encoding="utf-8", errors="ignore")):
            hits.append(p.name)
    assert not hits, f"bot-token-like text in: {hits}"


def test_env_example_has_placeholders_only():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        m = re.search(rf"^{key}=(.*)$", text, re.M)
        assert m, f"{key} missing from .env.example"
        assert m.group(1).strip() in ("", "<bot-token>", "<chat-id>"), f"{key} must be a placeholder"


def test_seed_script_never_calls_getupdates_or_webhooks():
    src = (ROOT / "scripts" / "seed_telegram.py").read_text(encoding="utf-8").lower()
    src += (ROOT / "scripts" / "kuma_notify.py").read_text(encoding="utf-8").lower()
    for bad in ("getupdates", "setwebhook", "deletewebhook", "logout"):
        assert bad not in src, f"forbidden bot call: {bad}"


# ---------------------------------------------------------------- part B: live VPS

def _sql(vps, sql):
    return vps.run(f"sudo sqlite3 -readonly {DB} \"{sql}\"")


def test_live_one_telegram_notification_default_active(vps, env):
    assert _sql(vps, "SELECT COUNT(*) FROM notification") == "1"
    assert _sql(vps, f"SELECT name || '|' || active || '|' || is_default FROM notification") == f"{NAME}|1|1"
    assert _sql(vps, "SELECT json_extract(config, '$.type') FROM notification") == "telegram"


def test_live_config_has_token_and_chat_id_of_expected_size(vps, env):
    tok = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    assert tok and chat, "TELEGRAM_* missing from .env"
    assert _sql(vps, "SELECT length(json_extract(config, '$.telegramBotToken')) FROM notification") == str(len(tok))
    assert _sql(vps, "SELECT length(json_extract(config, '$.telegramChatID')) FROM notification") == str(len(chat))
    # the stored value equals the .env value: compare by hash computed on the VPS, never by echo
    import hashlib
    want = hashlib.sha256(tok.encode()).hexdigest()
    got = vps.run(f"sudo sqlite3 -readonly {DB} \"SELECT json_extract(config, '$.telegramBotToken') FROM notification\" "
                  "| tr -d '\\n' | sha256sum | cut -c1-64")
    assert got == want


def test_live_linked_to_every_monitor(vps):
    monitors = _sql(vps, "SELECT COUNT(*) FROM monitor")
    linked = _sql(vps, "SELECT COUNT(DISTINCT monitor_id) FROM monitor_notification")
    assert int(monitors) >= 15
    assert linked == monitors
    assert _sql(vps, "SELECT COUNT(*) FROM monitor m WHERE NOT EXISTS (SELECT 1 FROM monitor_notification n "
                     "WHERE n.monitor_id = m.id)") == "0"


def test_live_kuma_healthy_and_hermes_active(vps):
    assert vps.run("docker inspect -f '{{.State.Health.Status}}' uptime-kuma") == "healthy"
    assert vps.run("systemctl is-active hermes-agent", check=False) == "active"
