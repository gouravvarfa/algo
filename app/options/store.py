"""Server-side saved strategies (Strategy Builder). Separate SQLite file, so the Bhavcopy market-data DB is never touched.

Migration policy: tables are created with IF NOT EXISTS and a schema_version row; future versions add columns/tables
only (never drop or rewrite rows). Every write runs in a transaction. Browser-saved strategies are never deleted:
they can be imported (copied) here; an import never overwrites an existing name.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "strategies.db"
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS strategies (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  state TEXT NOT NULL,          -- builder form state (JSON) so the page can reopen it exactly
  strategy TEXT NOT NULL,       -- the engine strategy JSON that was validated when saved
  source TEXT NOT NULL,         -- 'builder' | 'browser-import'
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def connect(db: Path | None = None) -> sqlite3.Connection:
    db = db or DB
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    with c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
            c.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    return c


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row, full: bool) -> dict:
    d = {"id": r["id"], "name": r["name"], "source": r["source"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if full:
        d["state"], d["strategy"] = json.loads(r["state"]), json.loads(r["strategy"])
    return d


def list_all(c: sqlite3.Connection) -> list[dict]:
    return [_row(r, False) for r in c.execute("SELECT * FROM strategies ORDER BY name COLLATE NOCASE")]


def get(c: sqlite3.Connection, sid: str) -> dict | None:
    r = c.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    return _row(r, True) if r else None


def by_name(c: sqlite3.Connection, name: str) -> dict | None:
    r = c.execute("SELECT * FROM strategies WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return _row(r, True) if r else None


def save(c: sqlite3.Connection, name: str, state: dict, strategy: dict, overwrite: bool = False, source: str = "builder") -> tuple[dict, bool]:
    """Returns (record, created). Raises KeyError if the name exists and overwrite is False."""
    old = by_name(c, name)
    if old and not overwrite:
        raise KeyError(name)
    now = _now()
    with c:
        if old:
            c.execute("UPDATE strategies SET name=?, state=?, strategy=?, updated_at=? WHERE id=?",
                      (name, json.dumps(state), json.dumps(strategy), now, old["id"]))
            sid = old["id"]
        else:
            sid = uuid.uuid4().hex[:12]
            c.execute("INSERT INTO strategies VALUES (?,?,?,?,?,?,?)", (sid, name, json.dumps(state), json.dumps(strategy), source, now, now))
    return get(c, sid), not old


def delete(c: sqlite3.Connection, sid: str) -> bool:
    with c:
        return c.execute("DELETE FROM strategies WHERE id=?", (sid,)).rowcount > 0
