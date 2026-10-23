"""Platform database: users, invites, backup codes, sessions, login failures and the audit log (the login system's storage).

Separate SQLite file (data/platform.db), so the market-data databases are never touched. Same migration policy as
app/options/store.py: versioned and additive. A migration only adds tables/indexes/columns, never drops or rewrites rows, and runs in
one transaction together with the schema_version bump, so a failed migration leaves the database at the old version. To change the
schema, append a new (version, sql) to MIGRATIONS; never edit an applied one.

All times in the tables are Unix seconds (REAL), except audit_log.ts which is an ISO-8601 UTC string a person can read.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB =Path(__file__).resolve().parents[2] / "data" / "platform.db"

_V1 = """
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  email TEXT,
  role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin', 'developer')),
  status TEXT NOT NULL DEFAULT 'INVITED' CHECK (status IN ('INVITED', 'ACTIVE', 'DISABLED', 'DELETED')),
  password_hash TEXT,
  totp_secret_enc TEXT,           -- Fernet-encrypted; never stored or logged in clear
  totp_last_used_step INTEGER,    -- replay protection: a TOTP time-step can be used once
  created_by TEXT,                -- NULL = the owner (created by scripts/create_owner.py)
  created_at REAL NOT NULL,
  disabled_at REAL,
  deleted_at REAL                 -- soft delete
);

CREATE TABLE user_invites (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  token_hash TEXT NOT NULL UNIQUE,   -- only the SHA-256 of the one-time setup link is stored
  expires_at REAL NOT NULL,
  used_at REAL
);

CREATE TABLE backup_codes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  code_hash TEXT NOT NULL,           -- Argon2id, like a password
  used_at REAL
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,               -- SHA-256 of the cookie token; a leaked database cannot be replayed as cookies
  user_id TEXT NOT NULL REFERENCES users(id),
  csrf_token TEXT NOT NULL,
  started_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,        -- last heartbeat: "is a page still open?"
  last_activity_at REAL NOT NULL,    -- last heartbeat that reported real user input: "is a person still there?"
  ended_at REAL,
  end_reason TEXT,                   -- logout | idle | expired | forced | disabled | replaced | reset
  device_label TEXT,
  user_agent_hash TEXT,
  ip_hash TEXT
);
-- one ACTIVE session per user, guaranteed by the database and not only by code
CREATE UNIQUE INDEX one_active_session_per_user ON sessions(user_id) WHERE ended_at IS NULL;

CREATE TABLE login_failures (        -- kept in the database so a restart does not reset the lock-out
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,                 -- 'user:<username>' or 'ip:<address>'
  ts REAL NOT NULL
);
CREATE INDEX login_failures_key_ts ON login_failures(key, ts);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor_id TEXT,
  actor_role TEXT,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  detail_json TEXT,
  prev_hash TEXT NOT NULL,           -- hash chain: every row stores the hash of the row before it
  this_hash TEXT NOT NULL
);
-- append only: the database itself refuses to change or remove a row
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

_V2 = """
CREATE TABLE feature_flags (          -- developer-controlled switches and kill switches (app/ops/flags.py)
  key TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'global',   -- 'global' or 'user:<id>'
  value_json TEXT NOT NULL,
  updated_by TEXT,
  updated_at REAL NOT NULL,
  PRIMARY KEY (key, scope)
);

CREATE TABLE incidents (              -- every isolated failure: what broke, what the system did about it (app/ops/incidents.py)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  component TEXT NOT NULL,
  user_id TEXT,
  severity TEXT NOT NULL DEFAULT 'error',
  message TEXT NOT NULL,
  detail_json TEXT,
  auto_action TEXT,
  resolved_at REAL
);
CREATE INDEX incidents_ts ON incidents(ts);
"""

_V3 = """
CREATE TABLE user_sim_positions (     -- the Options Simulator's positions, one list per user and index (moved from the browser to the server)
  user_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  data_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (user_id, symbol)
);

CREATE TABLE user_settings (          -- small per-user preferences (LIVE mode on/off, theme, ...)
  user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (user_id, key)
);
"""

_V4 = """
CREATE TABLE user_limits (            -- what an account may do in the paper simulator; set by an admin, enforced on the server
  user_id TEXT PRIMARY KEY,
  max_lots INTEGER,                   -- total lots across open positions (NULL = no limit)
  allowed_symbols TEXT,               -- JSON list, e.g. ["NIFTY"] (NULL = all)
  updated_by TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE user_notes (             -- internal notes about an account, visible to admins only
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  author_id TEXT NOT NULL,
  author_name TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX idx_user_notes_user ON user_notes (user_id, id);

CREATE TABLE announcement (           -- one banner shown to everybody who is signed in
  id INTEGER PRIMARY KEY CHECK (id = 1),
  message TEXT NOT NULL,
  level TEXT NOT NULL,
  set_by TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""

_V5 = """
CREATE TABLE option_trades (          -- the options journal: one row per simulated leg, kept forever (clearing the simulator never deletes it)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  pos_id TEXT NOT NULL,
  side TEXT NOT NULL, opt TEXT NOT NULL, strike REAL NOT NULL, expiry TEXT,
  lots INTEGER NOT NULL, lot_size INTEGER NOT NULL,
  entry REAL NOT NULL, entry_date TEXT, entry_time TEXT,
  exit REAL, exit_date TEXT, exit_time TEXT, exit_reason TEXT,
  sl REAL,
  realized REAL,
  status TEXT NOT NULL,               -- OPEN, CLOSED, or REMOVED (taken off the screen without an exit)
  opened_at REAL NOT NULL, closed_at REAL,
  note TEXT, tags TEXT, rating INTEGER,
  updated_at REAL NOT NULL
);
CREATE INDEX idx_option_trades_user ON option_trades (user_id, symbol, id);
CREATE UNIQUE INDEX idx_option_trades_key ON option_trades (user_id, symbol, pos_id, side, opt, strike, entry_date, entry);
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _V1), (2, _V2), (3, _V3), (4, _V4), (5, _V5)]
LATEST_VERSION = MIGRATIONS[-1][0]


def version(c: sqlite3.Connection) -> int:
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    return c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0


def migrate(c: sqlite3.Connection) -> int:
    """Apply every migration newer than the database's version. Returns the version afterwards."""
    current = version(c)
    for number, sql in MIGRATIONS:
        if number <= current:
            continue
        try:
            c.executescript(f"BEGIN;\n{sql}\nDELETE FROM schema_version;\nINSERT INTO schema_version VALUES ({number});\nCOMMIT;")
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        current = number
    return current


@contextmanager
def write_tx(c: sqlite3.Connection):
    """One write transaction that takes the write lock up front (BEGIN IMMEDIATE): everything inside commits together or not at all,
    and "check, then change" cannot interleave with another writer."""
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise


def connect(db: Path | None = None) -> sqlite3.Connection:
    """Open the platform database (creating and migrating it if needed). Rows come back as sqlite3.Row."""
    db = db or DB
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db, timeout=10, check_same_thread=False)    # one connection per request, used by one request at a time
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")      # readers (every request checks its session) do not block the writers
    migrate(c)
    return c
