"""Append-only, hash-chained audit log: who did what and when.

Every row stores the hash of the row before it, so editing or removing a row breaks the chain and verify_chain() reports the first row
that no longer matches. (The table also refuses UPDATE and DELETE; see platform_db.) Never put passwords, TOTP codes, session ids,
setup links or backup codes in `detail`.

append() opens its own write transaction, so call it AFTER the transaction that caused the event has been committed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

GENESIS = "0" * 64


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _hash(prev_hash: str, ts: str, actor_id, actor_role, action: str, target_type, target_id, detail_json) -> str:
    payload = json.dumps([prev_hash, ts, actor_id, actor_role, action, target_type, target_id, detail_json],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append(c: sqlite3.Connection, action: str, actor_id: str | None = None, actor_role: str | None = None,
           target_type: str | None = None, target_id: str | None = None, detail: dict | None = None, ts: str | None = None) -> int:
    """Add one entry and return its id. Reading the previous hash and inserting are one write transaction, so two simultaneous
    writers can never both chain onto the same previous row."""
    detail_json = json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False) if detail is not None else None
    ts = ts or _now()
    c.execute("BEGIN IMMEDIATE")
    try:
        last = c.execute("SELECT this_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = last[0] if last else GENESIS
        this_hash = _hash(prev_hash, ts, actor_id, actor_role, action, target_type, target_id, detail_json)
        cur = c.execute(
            "INSERT INTO audit_log (ts, actor_id, actor_role, action, target_type, target_id, detail_json, prev_hash, this_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, actor_id, actor_role, action, target_type, target_id, detail_json, prev_hash, this_hash))
        c.commit()
    except Exception:
        c.rollback()
        raise
    return cur.lastrowid


def verify_chain(c: sqlite3.Connection) -> tuple[bool, int | None]:
    """(True, None) if the whole log is intact, else (False, id of the first broken row).
    Limit of any hash chain: cutting off the newest rows cannot be seen from the chain alone (that is what an export is for)."""
    prev = GENESIS
    for r in c.execute("SELECT * FROM audit_log ORDER BY id"):
        expected = _hash(prev, r["ts"], r["actor_id"], r["actor_role"], r["action"], r["target_type"], r["target_id"], r["detail_json"])
        if r["prev_hash"] != prev or r["this_hash"] != expected:
            return False, r["id"]
        prev = r["this_hash"]
    return True, None
