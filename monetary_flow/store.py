"""SQLite journal + encrypted secrets. Secrets are encrypted with MF_MASTER_KEY and never returned."""
import base64
import hashlib
import json
import sqlite3
import time
from dataclasses import asdict

from cryptography.fernet import Fernet

from .config import Settings

SCHEMA = """
create table if not exists kv(key text primary key, value text);
create table if not exists secrets(name text primary key, value blob);
create table if not exists log(ts real, level text, msg text);
create table if not exists equity(ts real, equity real);
create table if not exists trades(
  id integer primary key, mode text, symbol text, status text, qty int,
  plan_entry real, stop real, target real, why text, risk_amount real,
  entry_order text, entry_price real, stop_order text, exit_order text,
  exit_price real, exit_reason text, fees real default 0, pnl real,
  opened_at real, closed_at real);
"""
SECRET_NAMES = ("groww_api_key", "groww_totp_secret", "angel_api_key", "angel_client_code",
                "angel_mpin", "angel_totp_secret", "llm_api_key")
BROKER_SECRETS = {"groww": ("groww_api_key", "groww_totp_secret"),
                  "angel": ("angel_api_key", "angel_client_code", "angel_mpin", "angel_totp_secret")}


class Store:
    def __init__(self, path: str, master_key: str):
        if len(master_key or "") < 32:
            raise SystemExit("MF_MASTER_KEY must be set to 32+ random characters")
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(master_key.encode()).digest()))
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # settings / state
    def get(self, key, default=None):
        row = self.db.execute("select value from kv where key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("insert or replace into kv values(?,?)", (key, json.dumps(value)))

    def settings(self) -> Settings:
        return Settings(**self.get("settings", {})).clamped()

    def save_settings(self, s: Settings):
        self.put("settings", asdict(s.clamped()))

    # secrets
    def set_secret(self, name, value):
        assert name in SECRET_NAMES
        if value:
            self.db.execute("insert or replace into secrets values(?,?)", (name, self.fernet.encrypt(value.encode())))
        else:
            self.db.execute("delete from secrets where name=?", (name,))

    def secret(self, name) -> str:
        row = self.db.execute("select value from secrets where name=?", (name,)).fetchone()
        return self.fernet.decrypt(row[0]).decode() if row else ""

    def secrets_present(self) -> dict:
        have = {r[0] for r in self.db.execute("select name from secrets")}
        return {n: n in have for n in SECRET_NAMES}

    # journal
    def log(self, msg, level="info"):
        self.db.execute("insert into log values(?,?,?)", (time.time(), level, msg))

    def logs(self, n=60):
        return [dict(r) for r in self.db.execute("select * from log order by ts desc limit ?", (n,))]

    def trades(self, where="1=1", args=()):
        return [dict(r) for r in self.db.execute(f"select * from trades where {where} order by id", args)]

    def add_trade(self, **t) -> int:
        cols = ",".join(t)
        return self.db.execute(f"insert into trades({cols}) values({','.join('?' * len(t))})", tuple(t.values())).lastrowid

    def update_trade(self, tid, **t):
        self.db.execute(f"update trades set {','.join(f'{k}=?' for k in t)} where id=?", (*t.values(), tid))

    def snapshot(self, equity):
        self.db.execute("insert into equity values(?,?)", (time.time(), equity))

    def equity_curve(self):
        return [tuple(r) for r in self.db.execute("select ts, equity from equity order by ts")]
