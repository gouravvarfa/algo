"""Server-side saved strategies (Strategy Builder). Separate SQLite file, so the Bhavcopy market-data DB is never touched.

Migration policy: tables are created with IF NOT EXISTS and a schema_version row; future versions add columns/tables
only (never drop or rewrite rows). Every write runs in a transaction. Browser-saved strategies are never deleted:
they can be imported (copied) here; an import never overwrites an existing name.

Version 2 makes every strategy private to one user. The version-1 table is left exactly as it was; its rows are COPIED into strategy_items.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "strategies.db"
SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS strategies (          -- version 1 (kept as it was, never rewritten)
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  state TEXT NOT NULL,          -- builder form state (JSON) so the page can reopen it exactly
  strategy TEXT NOT NULL,       -- the engine strategy JSON that was validated when saved
  source TEXT NOT NULL,         -- 'builder' | 'browser-import'
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""
# version 2: owner = platform user id ('' = the single local user, or a row from before accounts existed); a name only has to be unique per user
V2 = """
CREATE TABLE IF NOT EXISTS strategy_items (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT '', name TEXT NOT NULL COLLATE NOCASE,
  state TEXT NOT NULL, strategy TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (owner, name)
);
INSERT OR IGNORE INTO strategy_items SELECT id, '', name, state, strategy, source, created_at, updated_at FROM strategies;
DELETE FROM schema_version;
INSERT INTO schema_version VALUES (2);
"""


def connect(db: Path | None = None) -> sqlite3.Connection:
    db = db or DB
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    with c:
        c.executescript(SCHEMA)
        version = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        if version < SCHEMA_VERSION:
            c.executescript("BEGIN;\n" + V2 + "\nCOMMIT;")
    return c


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row, full: bool) -> dict:
    d = {"id": r["id"], "name": r["name"], "source": r["source"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if full:
        d["state"], d["strategy"] = json.loads(r["state"]), json.loads(r["strategy"])
    return d


def adopt(c: sqlite3.Connection, owner: str) -> int:
    """Strategies saved before accounts existed belong to the first account (the owner). A name the owner already uses is left untouched."""
    with c:
        return c.execute("UPDATE OR IGNORE strategy_items SET owner=? WHERE owner=''", (owner,)).rowcount


def list_all(c: sqlite3.Connection, owner: str = "") -> list[dict]:
    return [_row(r, False) for r in c.execute("SELECT * FROM strategy_items WHERE owner=? ORDER BY name COLLATE NOCASE", (owner,))]


def get(c: sqlite3.Connection, sid: str, owner: str = "") -> dict | None:
    r = c.execute("SELECT * FROM strategy_items WHERE id=? AND owner=?", (sid, owner)).fetchone()
    return _row(r, True) if r else None


def by_name(c: sqlite3.Connection, name: str, owner: str = "") -> dict | None:
    r = c.execute("SELECT * FROM strategy_items WHERE name=? COLLATE NOCASE AND owner=?", (name, owner)).fetchone()
    return _row(r, True) if r else None


def save(c: sqlite3.Connection, name: str, state: dict, strategy: dict, overwrite: bool = False, source: str = "builder", owner: str = "") -> tuple[dict, bool]:
    """Returns (record, created). Raises KeyError if the owner already has that name and overwrite is False."""
    old = by_name(c, name, owner)
    if old and not overwrite:
        raise KeyError(name)
    now = _now()
    with c:
        if old:
            c.execute("UPDATE strategy_items SET name=?, state=?, strategy=?, updated_at=? WHERE id=? AND owner=?",
                      (name, json.dumps(state), json.dumps(strategy), now, old["id"], owner))
            sid = old["id"]
        else:
            sid = uuid.uuid4().hex[:12]
            c.execute("INSERT INTO strategy_items VALUES (?,?,?,?,?,?,?,?)", (sid, owner, name, json.dumps(state), json.dumps(strategy), source, now, now))
    return get(c, sid, owner), not old


def delete(c: sqlite3.Connection, sid: str, owner: str = "") -> bool:
    with c:
        return c.execute("DELETE FROM strategy_items WHERE id=? AND owner=?", (sid, owner)).rowcount > 0
