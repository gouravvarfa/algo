"""Creating a user and the one-time setup link: password + QR + first code -> ACTIVE (PDF 3.1)."""
import time

import pyotp
import pytest

from app.security import users
from app.security.users import AuthError
from tests.security.helpers import PASSWORD, make_user


def new_invite(db, username="asha", role="user", now=None):
    return users.create_user(db, name="Asha", username=username, email=None, role=role, created_by="admin1", now=now)


def test_a_new_user_is_invited_and_cannot_log_in_yet(db):
    user, token = new_invite(db)
    assert user["status"] == "INVITED" and user["password_hash"] is None and user["totp_secret_enc"] is None
    with pytest.raises(users.InvalidCredentials):
        users.authenticate(db, "asha", PASSWORD, "123456")


def test_only_the_hash_of_the_setup_link_is_stored(db):
    _, token = new_invite(db)
    stored = db.execute("SELECT token_hash FROM user_invites").fetchone()[0]
    assert token not in stored and len(stored) == 64


@pytest.mark.parametrize("username", ["ab", "Has Space", "x" * 33, "-start", "semi;colon", ""])
def test_bad_usernames_are_refused(db, username):
    with pytest.raises(AuthError) as e:
        new_invite(db, username=username)
    assert e.value.code == "INVALID_USERNAME" and e.value.status == 400


def test_username_is_stored_lowercase_and_must_be_unique(db):
    user, _ = new_invite(db, username="Asha.K")
    assert user["username"] == "asha.k"
    with pytest.raises(AuthError) as e:
        new_invite(db, username="ASHA.K")
    assert e.value.code == "USERNAME_TAKEN" and e.value.status == 409


def test_unknown_role_is_refused(db):
    with pytest.raises(AuthError) as e:
        new_invite(db, role="root")
    assert e.value.code == "INVALID_ROLE"


def test_setup_shows_a_qr_and_a_fresh_secret_every_time(db):
    _, token = new_invite(db)
    a, b = users.begin_setup(db, token), users.begin_setup(db, token)
    assert a["qr_svg"].lstrip().startswith("<svg") and a["username"] == "asha"
    assert a["secret"] != b["secret"]
    stored = db.execute("SELECT totp_secret_enc FROM users WHERE username='asha'").fetchone()[0]
    assert a["secret"].replace(" ", "") not in stored and b["secret"].replace(" ", "") not in stored       # encrypted at rest


def test_completing_setup_with_the_right_code_activates_the_user_and_gives_ten_backup_codes(db):
    _, token = new_invite(db)
    secret = users.begin_setup(db, token)["secret"].replace(" ", "")
    user, codes = users.complete_setup(db, token, PASSWORD, pyotp.TOTP(secret).now())
    assert user["status"] == "ACTIVE" and user["password_hash"].startswith("$argon2id$") and PASSWORD not in user["password_hash"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert db.execute("SELECT COUNT(*) FROM backup_codes WHERE user_id=?", (user["id"],)).fetchone()[0] == 10
    assert not any(c.replace("-", "") in row[0] for c in codes for row in db.execute("SELECT code_hash FROM backup_codes"))


def test_the_link_works_only_once(db):
    _, token = new_invite(db)
    secret = users.begin_setup(db, token)["secret"].replace(" ", "")
    users.complete_setup(db, token, PASSWORD, pyotp.TOTP(secret).now())
    for call in (lambda: users.begin_setup(db, token), lambda: users.complete_setup(db, token, PASSWORD, pyotp.TOTP(secret).now())):
        with pytest.raises(AuthError) as e:
            call()
        assert e.value.code == "INVALID_LINK" and e.value.status == 404


def test_the_link_expires_after_24_hours(db):
    now = time.time()
    _, token = new_invite(db, now=now)
    users.begin_setup(db, token, now + 24 * 3600 - 5)                          # still valid just before
    with pytest.raises(AuthError) as e:
        users.begin_setup(db, token, now + 24 * 3600 + 5)
    assert e.value.code == "INVALID_LINK"


@pytest.mark.parametrize("token", ["", "nonsense", "x" * 300])
def test_a_wrong_link_is_refused(db, token):
    with pytest.raises(AuthError) as e:
        users.begin_setup(db, token)
    assert e.value.code == "INVALID_LINK"


def test_wrong_code_weak_password_or_no_qr_yet_are_refused_and_the_link_stays_usable(db):
    _, token = new_invite(db)
    with pytest.raises(AuthError) as e:
        users.complete_setup(db, token, PASSWORD, "000000")
    assert e.value.code == "SETUP_NOT_STARTED"
    secret = users.begin_setup(db, token)["secret"].replace(" ", "")
    with pytest.raises(AuthError) as e:
        users.complete_setup(db, token, PASSWORD, "000000")
    assert e.value.code == "INVALID_CODE"
    with pytest.raises(AuthError) as e:
        users.complete_setup(db, token, "short", pyotp.TOTP(secret).now())
    assert e.value.code == "WEAK_PASSWORD"
    user, _ = users.complete_setup(db, token, PASSWORD, pyotp.TOTP(secret).now())          # still works afterwards
    assert user["status"] == "ACTIVE"


def test_a_qr_that_was_only_displayed_cannot_be_used_after_the_page_is_reloaded(db):
    _, token = new_invite(db)
    old = users.begin_setup(db, token)["secret"].replace(" ", "")
    users.begin_setup(db, token)                                               # the user reloaded the page: a new secret replaces it
    with pytest.raises(AuthError) as e:
        users.complete_setup(db, token, PASSWORD, pyotp.TOTP(old).now())
    assert e.value.code == "INVALID_CODE"


def test_a_new_link_replaces_the_old_one(db):
    user, old = new_invite(db)
    fresh = users.issue_invite(db, user["id"])
    with pytest.raises(AuthError):
        users.begin_setup(db, old)
    assert users.begin_setup(db, fresh)["username"] == "asha"


def test_make_user_helper_produces_a_user_that_can_log_in(db):
    acct = make_user(db)
    assert users.authenticate(db, acct.username, acct.password, pyotp.TOTP(acct.secret).now())["id"] == acct.id


def test_two_requests_racing_on_the_same_link_cannot_both_finish_setup(db, monkeypatch):
    """The second request already passed the 'link is valid' check before the first one finished. Its write must still be refused,
    and it must not overwrite the first user's password."""
    from app.security import passwords
    _, token = new_invite(db)
    secret = users.begin_setup(db, token)["secret"].replace(" ", "")
    code = pyotp.TOTP(secret).now()
    seen_by_second = users._find_invite(db, token, time.time())            # what the second request saw before the first finished
    user, _ = users.complete_setup(db, token, PASSWORD, code)               # the first request completes
    monkeypatch.setattr(users, "_find_invite", lambda *a, **k: seen_by_second)
    with pytest.raises(AuthError) as e:
        users.complete_setup(db, token, "attacker chosen passphrase", code)
    assert e.value.code == "INVALID_LINK"
    stored = users.get_user(db, user["id"])["password_hash"]
    assert passwords.verify_password(stored, PASSWORD) and not passwords.verify_password(stored, "attacker chosen passphrase")
