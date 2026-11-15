"""Resetting a person's authenticator, end to end (the owner, 15 Nov 2026: after Yogesh's reset he could not sign in: the setup was not finished,
the login page said only "invalid", and his failed tries locked the whole family's internet address)."""
import pyotp

from tests.security.conftest import FIREFOX_LINUX
from tests.security.helpers import PASSWORD, api, login, make_owner, make_user, totp_now

NEW_PW = "a brand new long password"


def signed_in_admin(db, new_client):
    owner = make_owner(db)
    c = new_client()
    assert login(c, owner).status_code == 200
    return c


def reset(c_admin, acct) -> str:
    r = api(c_admin, "POST", f"/api/admin/users/{acct.id}/reset-totp")
    assert r.status_code == 200 and r.json()["setup_url"].endswith(r.json()["setup_path"])
    return r.json()["setup_path"].rsplit("/", 1)[1]


def finish(browser, token, password=NEW_PW):
    secret = browser.get(f"/api/auth/setup/{token}").json()["secret"].replace(" ", "")
    code = pyotp.TOTP(secret).now()
    return secret, code, browser.post(f"/api/auth/setup/{token}", json={"password": password, "totp": code})


def sign_in(client, password, code):
    return client.post("/api/auth/login", json={"username": "yogesh", "password": password, "totp": code})


def test_the_whole_reset_flow(db, new_client):
    c_admin = signed_in_admin(db, new_client)
    acct = make_user(db, "yogesh")
    ipad = new_client(FIREFOX_LINUX)
    assert login(ipad, acct).status_code == 200
    token = reset(c_admin, acct)
    assert ipad.get("/api/auth/me").status_code == 401                                         # every device is signed out
    assert login(new_client(), acct, code=acct.next_backup()).status_code == 401              # the old backup codes are gone
    assert login(new_client(), acct, code=totp_now(acct.secret, 1)).status_code == 401        # the old authenticator entry no longer works
    browser = new_client()
    first = browser.get(f"/api/auth/setup/{token}").json()["secret"].replace(" ", "")
    assert first != acct.secret
    secret, code, r = finish(browser, token)
    assert secret == first                                                                     # reloading the page shows the same QR
    assert r.status_code == 200 and r.json()["signed_in"] is True and len(r.json()["backup_codes"]) == 10
    assert browser.get("/api/auth/me").json()["user"]["username"] == "yogesh"                 # signed in at once: no second code needed
    other = new_client()
    codes = r.json()["backup_codes"]
    assert sign_in(other, NEW_PW, code).status_code == 401                                     # the code used for the setup is spent
    assert sign_in(other, PASSWORD, codes[0]).status_code == 401                               # the old password no longer works
    assert sign_in(other, NEW_PW, codes[1]).status_code == 200
    assert browser.get(f"/api/auth/setup/{token}").status_code == 404                          # the link works once


def test_a_reset_lifts_the_persons_lock(db, new_client):
    c_admin = signed_in_admin(db, new_client)
    acct = make_user(db, "yogesh")
    for _ in range(5):
        login(new_client(), acct, password="wrong password here")
    assert login(new_client(), acct).status_code == 423
    token = reset(c_admin, acct)
    _, _, r = finish(new_client(), token)
    assert r.status_code == 200
    assert sign_in(new_client(), NEW_PW, r.json()["backup_codes"][0]).status_code == 200


def test_failed_logins_while_the_setup_is_waiting_do_not_block_finishing_it(db, new_client):
    c_admin = signed_in_admin(db, new_client)
    acct = make_user(db, "yogesh")
    token = reset(c_admin, acct)
    for _ in range(6):                                                                         # what happened: the login page was tried first
        login(new_client(), acct, password="wrong password here")
    browser = new_client()
    _, _, r = finish(browser, token)
    assert r.status_code == 200 and browser.get("/api/auth/me").status_code == 200
    assert sign_in(new_client(), NEW_PW, r.json()["backup_codes"][0]).status_code == 200


def test_a_wrong_code_on_the_setup_page_keeps_the_link_usable_and_signs_nobody_in(db, new_client):
    c_admin = signed_in_admin(db, new_client)
    acct = make_user(db, "yogesh")
    token = reset(c_admin, acct)
    browser = new_client()
    browser.get(f"/api/auth/setup/{token}")
    bad = browser.post(f"/api/auth/setup/{token}", json={"password": NEW_PW, "totp": "000000"})
    assert bad.status_code == 400 and "algo_sid" not in browser.cookies
    _, _, r = finish(browser, token)
    assert r.status_code == 200


def test_a_second_reset_makes_the_first_link_and_its_qr_useless(db, new_client):
    c_admin = signed_in_admin(db, new_client)
    acct = make_user(db, "yogesh")
    t1 = reset(c_admin, acct)
    s1 = new_client().get(f"/api/auth/setup/{t1}").json()["secret"].replace(" ", "")
    t2 = reset(c_admin, acct)
    assert new_client().get(f"/api/auth/setup/{t1}").status_code == 404
    s2 = new_client().get(f"/api/auth/setup/{t2}").json()["secret"].replace(" ", "")
    assert s1 != s2
    r = new_client().post(f"/api/auth/setup/{t2}", json={"password": NEW_PW, "totp": pyotp.TOTP(s1).now()})
    assert r.status_code == 400                                                                # a code from the first QR is refused


def test_an_admin_who_finishes_someone_elses_setup_in_his_own_browser_stays_signed_in(db, new_client):
    """The owner, 15 Nov 2026: Gaurav finished Yogesh's setup in another tab of his own browser; Gaurav's admin session was replaced by Yogesh's,
    and the admin page then answered "You do not have permission"."""
    c_admin = signed_in_admin(db, new_client)
    acct = make_user(db, "yogesh")
    token = reset(c_admin, acct)
    _, _, r = finish(c_admin, token)                                                           # the same browser, the same cookies
    assert r.status_code == 200 and r.json()["signed_in"] is False and len(r.json()["backup_codes"]) == 10
    assert c_admin.get("/api/auth/me").json()["user"]["username"] == "owner"                   # still the admin
    assert api(c_admin, "POST", f"/api/admin/users/{acct.id}/reset-totp").status_code == 200   # and the admin can act again
