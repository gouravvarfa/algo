"""scripts/create_owner.py: the very first user (the owner) and the link for them."""
import importlib.util
import sys
import time
from pathlib import Path

import pyotp
import pytest

from app.security import users

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_owner.py"


def run(monkeypatch, capsys, *argv):
    spec = importlib.util.spec_from_file_location("create_owner_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["create_owner.py", *argv])
    mod.main()
    return capsys.readouterr().out


def link_token(out):
    return out.split("/setup/")[1].split()[0]


def test_creates_the_owner_and_prints_a_working_link(db, monkeypatch, capsys):
    out = run(monkeypatch, capsys, "Owner", "Gourav Varfa", "--base-url", "https://algo.example.com/")
    assert "https://algo.example.com/setup/" in out and "24 hours" in out
    owner = db.execute("SELECT * FROM users").fetchone()
    assert owner["username"] == "owner" and owner["role"] == "developer" and owner["created_by"] is None and owner["status"] == "INVITED"
    secret = users.begin_setup(db, link_token(out))["secret"].replace(" ", "")
    user, _ = users.complete_setup(db, link_token(out), "owner passphrase 1234", pyotp.TOTP(secret).now())
    assert user["status"] == "ACTIVE"
    assert db.execute("SELECT action FROM audit_log").fetchone()[0] == "owner.create"


def test_running_it_again_before_setup_gives_a_new_link_for_the_same_owner(db, monkeypatch, capsys):
    first = link_token(run(monkeypatch, capsys, "owner", "Owner"))
    second = link_token(run(monkeypatch, capsys, "owner", "Owner"))
    assert first != second and db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    with pytest.raises(users.AuthError):
        users.begin_setup(db, first)                                    # the lost link no longer works
    assert users.begin_setup(db, second)["username"] == "owner"


def test_it_refuses_to_create_a_second_owner_or_touch_an_existing_user(db, monkeypatch, capsys):
    from tests.security.helpers import make_owner, make_user
    make_owner(db, "boss")
    with pytest.raises(SystemExit) as e:
        run(monkeypatch, capsys, "another", "Another Owner")
    assert "owner already exists" in str(e.value)
    make_user(db, "rahul")
    with pytest.raises(SystemExit):
        run(monkeypatch, capsys, "rahul", "Rahul")
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


# ---- scripts/reset_user.py: the way back in for someone nobody above them can reset ----
RESET_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reset_user.py"


def run_reset(monkeypatch, capsys, *argv):
    spec = importlib.util.spec_from_file_location("reset_user_script", RESET_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["reset_user.py", *argv])
    mod.main()
    return capsys.readouterr().out


def test_reset_gives_a_locked_out_owner_a_way_back_with_a_new_password(db, monkeypatch, capsys, client):
    from tests.security.helpers import login, make_owner, totp_now
    owner = make_owner(db)
    session_client = client
    assert login(session_client, owner).status_code == 200
    spare = owner.next_backup()

    out = run_reset(monkeypatch, capsys, "owner", "--base-url", "https://algo.example.com")
    assert "https://algo.example.com/setup/" in out and "no longer work" in out
    # everything old is dead at once
    assert session_client.get("/api/auth/me").status_code == 401
    assert db.execute("SELECT status FROM users WHERE id=?", (owner.id,)).fetchone()[0] == "INVITED"
    assert db.execute("SELECT COUNT(*) FROM backup_codes WHERE user_id=?", (owner.id,)).fetchone()[0] == 0
    assert login(client, owner, code=spare).status_code == 401 and login(client, owner, code=totp_now(owner.secret, +1)).status_code == 401
    # the new link: new password + new QR
    token = link_token(out)
    new_secret = users.begin_setup(db, token)["secret"].replace(" ", "")
    assert new_secret != owner.secret
    users.complete_setup(db, token, "my brand new passphrase", pyotp.TOTP(new_secret).now())
    assert users.authenticate(db, "owner", "my brand new passphrase", pyotp.TOTP(new_secret).at(time.time() + 30))["id"] == owner.id
    with pytest.raises(users.InvalidCredentials):
        users.authenticate(db, "owner", owner.password, pyotp.TOTP(new_secret).at(time.time() + 30))            # the old password is gone
    assert db.execute("SELECT detail_json FROM audit_log WHERE action='operator.reset'").fetchone()[0].count("scripts/reset_user.py") == 1


def test_reset_refuses_an_unknown_or_removed_user(db, monkeypatch, capsys):
    from tests.security.helpers import make_user
    acct = make_user(db, "gone")
    db.execute("UPDATE users SET status='DELETED' WHERE id=?", (acct.id,))
    db.commit()
    for name in ("nobody", "gone"):
        with pytest.raises(SystemExit) as e:
            run_reset(monkeypatch, capsys, name)
        assert "No user named" in str(e.value)
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='operator.reset'").fetchone()[0] == 0


def test_the_old_login_address_leads_to_the_react_login_page(client):
    # what the page tells people who forgot their password is tested in frontend/src/features/auth/LoginPage.test.tsx
    r = client.get("/login?why=idle", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/login?why=idle"
