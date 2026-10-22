"""Platform database: migrations, table constraints, and the append-only hash-chained audit log."""
import sqlite3

import pytest

from app.db import audit, platform_db

NOW = 1_800_000_000.0


def add_user(c, uid="u1", username="rahul", **kw):
    row = {"id": uid, "username": username, "name": "Rahul", "created_at": NOW, **kw}
    c.execute(f"INSERT INTO users ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    c.commit()


def add_session(c, sid, uid="u1", ended_at=None):
    c.execute("INSERT INTO sessions (id, user_id, csrf_token, started_at, last_seen_at, last_activity_at, ended_at) VALUES (?,?,?,?,?,?,?)",
              (sid, uid, "csrf", NOW, NOW, NOW, ended_at))
    c.commit()


def test_fresh_database_is_migrated_to_the_latest_version(db):
    assert platform_db.version(db) == platform_db.LATEST_VERSION
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "user_invites", "backup_codes", "sessions", "login_failures", "audit_log", "schema_version"} <= tables


def test_migrating_again_changes_nothing(db):
    platform_db.migrate(db)
    assert db.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_failed_migration_keeps_the_old_version_and_leaves_no_half_applied_table(db, monkeypatch):
    bad = "CREATE TABLE half_done (id INTEGER); CREATE TABLE users (id INTEGER);"          # the second statement fails: users exists
    monkeypatch.setattr(platform_db, "MIGRATIONS", platform_db.MIGRATIONS + [(platform_db.LATEST_VERSION + 1, bad)])
    with pytest.raises(sqlite3.OperationalError):
        platform_db.migrate(db)
    assert platform_db.version(db) == platform_db.LATEST_VERSION
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='half_done'").fetchone() is None


def test_only_one_active_session_per_user_and_a_new_one_after_the_old_ended(db):
    add_user(db)
    add_session(db, "s1")
    with pytest.raises(sqlite3.IntegrityError):
        add_session(db, "s2")
    db.execute("UPDATE sessions SET ended_at=?, end_reason='logout' WHERE id='s1'", (NOW,))
    db.commit()
    add_session(db, "s2")
    add_session(db, "s3", ended_at=NOW)               # any number of ended sessions can sit in the history


def test_different_users_may_each_be_logged_in(db):
    add_user(db, "u1", "rahul")
    add_user(db, "u2", "asha")
    add_session(db, "s1", "u1")
    add_session(db, "s2", "u2")


def test_username_is_unique_ignoring_case(db):
    add_user(db, "u1", "Rahul")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(db, "u2", "rahul")


@pytest.mark.parametrize("field,value", [("role", "superuser"), ("status", "BANNED")])
def test_role_and_status_only_accept_known_values(db, field, value):
    with pytest.raises(sqlite3.IntegrityError):
        add_user(db, **{field: value})


def test_a_session_needs_an_existing_user(db):
    with pytest.raises(sqlite3.IntegrityError):
        add_session(db, "s1", uid="nobody")


# ---- audit log ----
def fill(c, n=3):
    return [audit.append(c, "user.create", actor_id="a1", actor_role="admin", target_type="user", target_id=f"u{i}", detail={"i": i}) for i in range(n)]


def tamper(c, sql):
    """Someone with direct database access drops the guard triggers first; the hash chain must still expose the edit."""
    c.execute("DROP TRIGGER audit_log_no_update")
    c.execute("DROP TRIGGER audit_log_no_delete")
    c.execute(sql)
    c.commit()


def test_audit_entries_chain_from_genesis_and_verify(db):
    assert audit.verify_chain(db) == (True, None)
    fill(db)
    rows = list(db.execute("SELECT * FROM audit_log ORDER BY id"))
    assert rows[0]["prev_hash"] == audit.GENESIS and rows[1]["prev_hash"] == rows[0]["this_hash"] and rows[2]["prev_hash"] == rows[1]["this_hash"]
    assert audit.verify_chain(db) == (True, None)


def test_the_database_refuses_to_update_or_delete_an_audit_entry(db):
    fill(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_log SET action='nothing happened' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log WHERE id=1")


def test_editing_an_audit_entry_breaks_the_chain_at_that_entry(db):
    fill(db, 4)
    tamper(db, "UPDATE audit_log SET actor_id='someone else' WHERE id=2")
    assert audit.verify_chain(db) == (False, 2)


def test_removing_an_audit_entry_is_detected(db):
    fill(db, 4)
    tamper(db, "DELETE FROM audit_log WHERE id=2")
    assert audit.verify_chain(db) == (False, 3)


def test_detail_is_stable_json_and_optional(db):
    audit.append(db, "login.failed", detail={"b": 2, "a": 1})
    audit.append(db, "logout")
    rows = list(db.execute("SELECT detail_json FROM audit_log ORDER BY id"))
    assert rows[0][0] == '{"a":1,"b":2}' and rows[1][0] is None
    assert audit.verify_chain(db) == (True, None)


def test_a_failed_audit_insert_does_not_break_the_chain(db):
    audit.append(db, "ok.first")
    with pytest.raises(sqlite3.IntegrityError):
        audit.append(db, None)                                       # action is NOT NULL
    audit.append(db, "ok.second")
    assert audit.verify_chain(db) == (True, None)
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2
