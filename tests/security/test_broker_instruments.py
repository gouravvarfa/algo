"""Top-Bottom: the person's own real-order switch for each instrument. It can only be on while the account is in effective real mode, and it never survives the account
leaving real mode (paper, admin withdrawal, kill switch). No order is sent here; the order step asks store.instrument_real() first. Made-up data."""
from types import SimpleNamespace

import pytest

from app import main as appmain
from app.broker import api as broker_api, store
from app.ops import flags
from tests.security.helpers import PASSWORD, api, login, make_owner, make_user

BODY = {"client_code": "A123456", "api_key": "KEY12345", "pin": "1234", "totp_secret": "JBSWY3DPEHPK3PXP"}


class StubEngine:
    def __init__(self):
        self.instruments = {}

    def add(self, owner, iid, symbol):
        self.instruments[iid] = SimpleNamespace(id=iid, symbol=symbol, trading_symbol=f"{symbol}26NOVFUT", owner=owner)

    def owned(self, iid, owner):
        i = self.instruments.get(iid)
        return i if i is not None and i.owner == owner else None


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    flags._cache = None
    broker_api._bad_pw.clear()
    stub = StubEngine()
    monkeypatch.setattr(appmain, "engine", stub)
    yield stub
    flags._cache = None


def setup(db, new_client, stub, real=True):
    owner = make_owner(db)
    admin = make_user(db, "admini", "admin", created_by=owner.id)
    user = make_user(db, "traderi", "user")
    c_owner, c_admin, c_user = new_client(), new_client(), new_client()
    for c, a in ((c_owner, owner), (c_admin, admin), (c_user, user)):
        assert login(c, a).status_code == 200
    stub.add(user.id, "i-bank", "BANKNIFTY")
    stub.add(user.id, "i-reliance", "RELIANCE")
    stub.add(admin.id, "i-other", "TCS")
    api(c_user, "PUT", "/api/me/broker", json=BODY)
    db.execute("UPDATE user_broker SET status='connected' WHERE user_id=?", (user.id,))
    db.commit()
    if real:
        api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True})
        assert api(c_admin, "PUT", f"/api/admin/users/{user.id}/broker/real", json={"allowed": True, "max_lots": 1}).status_code == 200
        assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD}).status_code == 200
    return user, c_owner, c_admin, c_user


def switch(c, iid, on):
    return api(c, "PUT", f"/api/me/broker/instruments/{iid}", json={"real": on})


def test_every_instrument_starts_off_and_can_only_be_switched_on_in_real_mode(db, new_client, _reset):
    user, _, _, c_user = setup(db, new_client, _reset, real=False)
    listed = c_user.get("/api/me/broker/instruments").json()
    assert listed["account_mode"] == "paper" and {i["symbol"]: i["real_on"] for i in listed["instruments"]} == {"BANKNIFTY": False, "RELIANCE": False}
    r = switch(c_user, "i-bank", True)
    assert r.status_code == 403 and r.json()["error"] == "REAL_BLOCKED"
    assert switch(c_user, "i-bank", False).status_code == 200                                            # off is always allowed


def test_the_switch_is_per_instrument_and_listed_with_its_effect(db, new_client, _reset):
    user, _, _, c_user = setup(db, new_client, _reset)
    r = switch(c_user, "i-bank", True)
    assert r.status_code == 200
    got = {i["symbol"]: (i["real_on"], i["real_effective"]) for i in r.json()["instruments"]}
    assert got == {"BANKNIFTY": (True, True), "RELIANCE": (False, False)}
    assert store.instrument_real(db, user.id, "i-bank") is True and store.instrument_real(db, user.id, "i-reliance") is False
    switch(c_user, "i-bank", False)
    assert store.instrument_real(db, user.id, "i-bank") is False


def test_nobody_can_switch_another_persons_instrument(db, new_client, _reset):
    user, _, _, c_user = setup(db, new_client, _reset)
    assert switch(c_user, "i-other", True).status_code == 404
    assert switch(c_user, "nope", True).status_code == 404
    assert "TCS" not in c_user.get("/api/me/broker/instruments").text


def test_going_back_to_paper_clears_every_switch_and_real_later_does_not_rearm_them(db, new_client, _reset):
    user, _, _, c_user = setup(db, new_client, _reset)
    switch(c_user, "i-bank", True)
    switch(c_user, "i-reliance", True)
    assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "paper"}).status_code == 200
    assert not any(i["real_on"] for i in c_user.get("/api/me/broker/instruments").json()["instruments"])
    assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD}).status_code == 200
    assert not any(i["real_on"] for i in c_user.get("/api/me/broker/instruments").json()["instruments"])
    assert store.instrument_real(db, user.id, "i-bank") is False


def test_the_kill_switch_and_an_admin_withdrawal_clear_the_switches(db, new_client, _reset):
    user, c_owner, c_admin, c_user = setup(db, new_client, _reset)
    switch(c_user, "i-bank", True)
    assert api(c_admin, "POST", "/api/admin/broker/master", json={"enabled": False}).status_code == 200
    assert not any(i["real_on"] for i in c_user.get("/api/me/broker/instruments").json()["instruments"])
    api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True})
    api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD})
    switch(c_user, "i-bank", True)
    api(c_admin, "PUT", f"/api/admin/users/{user.id}/broker/real", json={"allowed": False, "max_lots": 1})
    assert not any(i["real_on"] for i in c_user.get("/api/me/broker/instruments").json()["instruments"])


def test_a_failed_login_check_makes_a_switched_on_instrument_not_effective(db, new_client, _reset):
    user, _, _, c_user = setup(db, new_client, _reset)
    switch(c_user, "i-bank", True)
    store.set_status(db, user.id, "error", "Invalid totp")
    got = c_user.get("/api/me/broker/instruments").json()
    assert got["account_mode"] == "paper" and store.instrument_real(db, user.id, "i-bank") is False
    assert [i["real_effective"] for i in got["instruments"] if i["symbol"] == "BANKNIFTY"] == [False]


def test_switching_is_audited(db, new_client, _reset):
    user, _, c_admin, c_user = setup(db, new_client, _reset)
    switch(c_user, "i-bank", True)
    acts = [e["action"] for e in c_admin.get("/api/admin/audit?limit=30").json()["entries"]]
    assert "broker.instrument" in acts
