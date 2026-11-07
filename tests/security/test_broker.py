"""A user connects their own Angel One account. Secrets are stored encrypted and never returned; an admin sees status only. Read-only: no order can be placed.
All credentials below are made up; the Angel One calls are replaced by stubs."""
import pytest

from app.broker import account, store
from app.providers import angelone_client as ao
from tests.security.helpers import api, login, make_owner, make_user

BODY = {"client_code": "A123456", "api_key": "KEY12345", "pin": "1234", "totp_secret": "JBSWY3DPEHPK3PXP"}
SECRETS = ["KEY12345", "1234", "JBSWY3DPEHPK3PXP"]


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(store, "MIN_GAP_S", 0.0)


def _two(db, new_client):
    owner = make_owner(db)
    admin = make_user(db, "adminb", "admin", created_by=owner.id if hasattr(owner, "id") else "seed")
    user = make_user(db, "traderb", "user")
    c_admin, c_user = new_client(), new_client()
    assert login(c_admin, admin).status_code == 200 and login(c_user, user).status_code == 200
    return user, c_admin, c_user


def test_a_user_saves_their_own_details_and_no_response_ever_contains_a_secret(db, new_client):
    user, c_admin, c_user = _two(db, new_client)
    assert c_user.get("/api/me/broker").json()["configured"] is False
    r = api(c_user, "PUT", "/api/me/broker", json=BODY)
    assert r.status_code == 200 and r.json()["configured"] is True and r.json()["mode"] == "paper" and r.json()["status"] == "untested"
    blobs = [r.text, c_user.get("/api/me/broker").text, c_admin.get("/api/admin/broker").text, c_admin.get(f"/api/admin/users/{user.id}/broker").text,
             c_admin.get("/api/admin/audit?limit=50").text]
    for text in blobs:
        for s in SECRETS:
            assert s not in text
        assert "A123456" not in text                                                                          # the client code is masked too
    row = db.execute("SELECT api_key_enc, pin_enc, totp_enc FROM user_broker WHERE user_id=?", (user.id,)).fetchone()
    assert all(s not in "".join(row) for s in SECRETS)                                                       # encrypted at rest
    assert store.secrets(store.row(db, user.id))["api_key"] == "KEY12345"


def test_bad_details_are_refused_with_a_reason(db, new_client):
    _, _, c_user = _two(db, new_client)
    for patch, code in (({"totp_secret": "not base32!!"}, "BAD_TOTP"), ({"pin": "12ab"}, "BAD_PIN"), ({"client_code": "x y"}, "BAD_CLIENT_CODE"), ({"api_key": "abc"}, "BAD_API_KEY")):
        r = api(c_user, "PUT", "/api/me/broker", json={**BODY, **patch})
        assert r.status_code == 400 and r.json()["error"] == code, (patch, r.text)


def test_the_admin_can_set_up_and_remove_for_a_user_but_a_user_cannot_touch_another(db, new_client):
    user, c_admin, c_user = _two(db, new_client)
    assert api(c_user, "PUT", f"/api/admin/users/{user.id}/broker", json=BODY).status_code == 403
    assert api(c_admin, "PUT", f"/api/admin/users/{user.id}/broker", json=BODY).status_code == 200
    assert c_user.get("/api/me/broker").json()["configured"] is True
    listed = c_admin.get("/api/admin/broker").json()["accounts"]
    assert [a["username"] for a in listed] == ["traderb"] and "pin" not in str(listed)
    assert c_user.get("/api/admin/broker").status_code == 403
    assert api(c_admin, "DELETE", f"/api/admin/users/{user.id}/broker").status_code == 200
    assert c_user.get("/api/me/broker").json()["configured"] is False
    acts = [e["action"] for e in c_admin.get("/api/admin/audit?limit=20").json()["entries"]]
    assert "broker.save" in acts and "broker.delete" in acts


def test_the_test_button_logs_in_and_records_the_verdict(db, new_client, monkeypatch):
    user, _, c_user = _two(db, new_client)
    api(c_user, "PUT", "/api/me/broker", json=BODY)
    seen = {}

    async def fake_login(api_key, client_code, pin, totp_secret):
        seen.update(api_key=api_key, client_code=client_code, pin=pin, totp=totp_secret)
        return ao.AngelOneSession(api_key, "jwt", "feed", "refresh", client_code)

    async def fake_logout(s):
        seen["logout"] = True

    monkeypatch.setattr(ao, "login", fake_login)
    monkeypatch.setattr(ao, "logout", fake_logout)
    r = api(c_user, "POST", "/api/me/broker/test")
    assert r.status_code == 200 and r.json()["status"] == "connected" and seen["client_code"] == "A123456" and seen["pin"] == "1234" and seen["logout"]
    assert "A123456" not in r.text

    async def bad_login(**_):
        raise ao.AngelOneApiError("Invalid totp", "auth")

    monkeypatch.setattr(ao, "login", lambda **k: bad_login(**k))
    r = api(c_user, "POST", "/api/me/broker/test")
    assert r.status_code == 400 and r.json()["error"] == "LOGIN_FAILED"
    me = c_user.get("/api/me/broker").json()
    assert me["status"] == "error" and "Invalid totp" in me["status_message"]


def test_the_summary_reads_funds_positions_and_orders_and_returns_no_secret(db, new_client, monkeypatch):
    _, _, c_user = _two(db, new_client)
    api(c_user, "PUT", "/api/me/broker", json=BODY)

    async def fake_login(**k):
        return ao.AngelOneSession(k["api_key"], "jwt", "feed", "refresh", k["client_code"])

    async def noop(s):
        return None

    async def funds(s):
        return {"availablecash": "125000.50", "net": "125000.5", "utilisedspan": "40000"}

    async def positions(s):
        return [{"tradingsymbol": "NIFTY10NOV2625000CE", "exchange": "NFO", "producttype": "CARRYFORWARD", "netqty": "-65", "netprice": "100", "ltp": "90", "pnl": "650"},
                {"tradingsymbol": "OLD", "netqty": "0"}]

    async def orders(s):
        return [{"orderid": "1", "tradingsymbol": "NIFTY10NOV2625000CE", "transactiontype": "SELL", "quantity": "65", "status": "complete", "ordertype": "MARKET"}]

    monkeypatch.setattr(ao, "login", fake_login)
    monkeypatch.setattr(ao, "logout", noop)
    monkeypatch.setattr(ao, "get_funds", funds)
    monkeypatch.setattr(ao, "get_positions", positions)
    monkeypatch.setattr(ao, "get_order_book", orders)
    r = api(c_user, "GET", "/api/me/broker/summary")
    j = r.json()
    assert r.status_code == 200 and j["funds"]["available_cash"] == 125000.5 and len(j["positions"]) == 2 and len(j["open_positions"]) == 1
    assert j["open_positions"][0]["net_qty"] == -65 and j["orders"][0]["status"] == "complete"
    for s in SECRETS:
        assert s not in r.text


def test_logins_are_spaced_out_per_user(db, new_client, monkeypatch):
    _, _, c_user = _two(db, new_client)
    api(c_user, "PUT", "/api/me/broker", json=BODY)
    monkeypatch.setattr(store, "MIN_GAP_S", 60.0)

    async def fake_login(**k):
        return ao.AngelOneSession(k["api_key"], "jwt", "", "", k["client_code"])

    async def noop(s):
        return None

    monkeypatch.setattr(ao, "login", fake_login)
    monkeypatch.setattr(ao, "logout", noop)
    assert api(c_user, "POST", "/api/me/broker/test").status_code == 200
    r = api(c_user, "POST", "/api/me/broker/test")
    assert r.status_code == 429 and r.json()["error"] == "TOO_FAST"


def test_nothing_in_the_account_module_can_place_an_order():
    src = open(account.__file__, encoding="utf-8").read() + open(ao.__file__, encoding="utf-8").read()
    for word in ("placeOrder", "modifyOrder", "cancelOrder", "order/v1/place", "order/v1/modify", "order/v1/cancel"):
        assert word not in src


def test_the_order_routes_of_angel_one_appear_in_one_file_only():
    """Sending an order is isolated in app/broker/angel_orders.py so it can be audited in one read."""
    import pathlib
    root = pathlib.Path(account.__file__).resolve().parents[1]
    hits = [p.relative_to(root).as_posix() for p in root.rglob("*.py") if "order/v1/placeOrder" in p.read_text(encoding="utf-8") or "order/v1/cancelOrder" in p.read_text(encoding="utf-8")]
    assert hits == ["broker/angel_orders.py"]
