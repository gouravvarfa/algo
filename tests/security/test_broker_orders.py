"""Real orders: every check, the once-only preview, and the outcomes (placed, refused, unknown). Angel One is replaced by stubs; nothing here can reach a broker.
All accounts, prices and ids are made up."""
import datetime as dt

import pytest

from app.broker import angel_orders, api as broker_api, orders, store
from app.ops import flags
from app.options import master
from app.providers import angelone_client as ao
from tests.security.helpers import PASSWORD, api, login, make_owner, make_user

BODY = {"client_code": "A123456", "api_key": "KEY12345", "pin": "1234", "totp_secret": "JBSWY3DPEHPK3PXP"}
SYM = "NIFTY10NOV2625000CE"
CONTRACT = master.OptionContract(symbol=SYM, token="111", name="NIFTY", strike=25000.0, expiry=dt.date(2026, 11, 10), option_type="CE", lot_size=65, tick_size=0.05)
ORDER = {"symbol": "NIFTY", "expiry": "2026-11-10", "strike": 25000, "opt": "CE", "side": "BUY", "lots": 1, "price": 100.0, "product": "CARRYFORWARD"}


class World:
    """What the stubbed Angel One says. Tests change these."""
    positions: list = []
    funds: dict = {}
    ltp: float | None = 100.0
    margin: float | None = 20000.0
    placed: list = []
    cancelled: list = []
    place_result = "OID1"


@pytest.fixture(autouse=True)
def world(monkeypatch):
    W = World
    W.positions, W.funds, W.ltp, W.margin, W.placed, W.cancelled, W.place_result = [], {"availablecash": "500000", "m2mrealized": "0", "m2munrealized": "0"}, 100.0, 20000.0, [], [], "OID1"

    async def fake_login(**k):
        return ao.AngelOneSession(k["api_key"], "jwt", "", "", k["client_code"])

    async def noop(*a, **k):
        return None

    async def positions(s):
        return W.positions

    async def funds(s):
        return W.funds

    async def ltp(s, ex, sym, tok):
        return W.ltp

    async def margin(s, basket):
        return {"totalMarginRequired": W.margin} if W.margin is not None else (_ for _ in ()).throw(ao.AngelOneApiError("margin down", "data"))

    async def place(s, **k):
        W.placed.append(k)
        if isinstance(W.place_result, Exception):
            raise W.place_result
        return W.place_result

    async def cancel(s, oid):
        W.cancelled.append(oid)

    info = master.UnderlyingInfo(name="NIFTY", spot_token="1", spot_symbol="Nifty 50", vix_token=None, contracts=[CONTRACT], source="test")
    monkeypatch.setattr(ao, "login", fake_login)
    monkeypatch.setattr(ao, "logout", noop)
    monkeypatch.setattr(ao, "get_positions", positions)
    monkeypatch.setattr(ao, "get_funds", funds)
    monkeypatch.setattr(ao, "get_ltp", ltp)
    monkeypatch.setattr(ao, "get_margin", margin)
    monkeypatch.setattr(angel_orders, "place_limit", place)
    monkeypatch.setattr(angel_orders, "cancel", cancel)
    monkeypatch.setattr(master, "load_underlying", lambda sym, *a, **k: info)
    monkeypatch.setattr(orders, "market_open", lambda now: True)
    monkeypatch.setattr(orders, "MIN_GAP_S", 0.0)
    monkeypatch.setattr(store, "MIN_GAP_S", 0.0)
    monkeypatch.setitem(flags.DEFAULTS, "real.options", True)        # the option ticket is off by default; these tests exercise it
    orders._sessions.clear()
    orders._last_send.clear()
    broker_api._bad_pw.clear()
    flags._cache = None
    yield W
    flags._cache = None


def setup(db, new_client, real=True, **grant):
    owner = make_owner(db)
    admin = make_user(db, "admino", "admin", created_by=owner.id)
    user = make_user(db, "tradero", "user")
    c_owner, c_admin, c_user = new_client(), new_client(), new_client()
    for c, a in ((c_owner, owner), (c_admin, admin), (c_user, user)):
        assert login(c, a).status_code == 200
    api(c_user, "PUT", "/api/me/broker", json=BODY)
    db.execute("UPDATE user_broker SET status='connected' WHERE user_id=?", (user.id,))
    db.commit()
    if real:
        api(c_owner, "POST", "/api/admin/broker/master", json={"enabled": True})
        r = api(c_admin, "PUT", f"/api/admin/users/{user.id}/broker/real", json={"allowed": True, "max_lots": 2, "max_daily_loss": 5000, **grant})
        assert r.status_code == 200
        assert api(c_user, "PUT", "/api/me/broker/mode", json={"mode": "real", "password": PASSWORD}).status_code == 200
    return user, c_owner, c_admin, c_user


def preview(c_user, **over):
    return api(c_user, "POST", "/api/me/broker/orders/preview", json={**ORDER, **over})


def send(c_user, pid):
    return api(c_user, "POST", f"/api/me/broker/orders/{pid}/send")


def failed(resp):
    return [c["name"] for c in resp.json().get("checks") or [] if not c["ok"]]


def test_a_good_order_is_previewed_sent_once_and_recorded(db, new_client, world):
    user, _, c_admin, c_user = setup(db, new_client)
    p = preview(c_user)
    assert p.status_code == 200 and p.json()["ok"] is True and p.json()["intent"] == "entry" and p.json()["qty"] == 65 and p.json()["margin_required"] == 6500.0
    r = send(c_user, p.json()["id"])
    assert r.status_code == 200 and r.json()["status"] == "placed" and r.json()["broker_order_id"] == "OID1"
    assert world.placed == [{"trading_symbol": SYM, "token": "111", "side": "BUY", "qty": 65, "price": 100.0, "product": "CARRYFORWARD"}]
    again = send(c_user, p.json()["id"])
    assert again.status_code == 409 and again.json()["error"] == "ALREADY_USED" and len(world.placed) == 1       # never twice
    assert [o["status"] for o in api(c_user, "GET", "/api/me/broker/orders").json()["orders"]] == ["placed"]
    acts = [e["action"] for e in c_admin.get("/api/admin/audit?limit=40").json()["entries"]]
    assert "broker.order" in acts
    assert "KEY12345" not in r.text and "JBSWY3DPEHPK3PXP" not in r.text


def test_paper_accounts_cannot_send_and_the_reason_is_shown(db, new_client, world):
    user, _, _, c_user = setup(db, new_client, real=False)
    p = preview(c_user)
    assert p.status_code == 200 and p.json()["ok"] is False and "Real mode" in failed(p)
    r = send(c_user, p.json()["id"])
    assert r.status_code == 409 and r.json()["error"] == "CHECK_FAILED" and world.placed == []


def test_each_safety_check_blocks_an_entry(db, new_client, world, monkeypatch):
    user, _, _, c_user = setup(db, new_client)
    cases = [
        (dict(price=130.0), "Price near market"),                  # 30% away
        (dict(price=100.03), "Price tick"),                        # not a multiple of 0.05
        (dict(lots=3), "Lots per order"),                          # limit is 2
    ]
    for over, name in cases:
        p = preview(c_user, **over)
        assert p.status_code == 200 and p.json()["ok"] is False and name in failed(p), (over, p.text)
        assert send(c_user, p.json()["id"]).status_code == 409
    world.positions = [{"tradingsymbol": "NIFTY10NOV2624500PE", "netqty": "-130"}]                 # two lots already open
    p = preview(c_user)
    assert "Total open lots" in failed(p)
    world.positions = []
    world.funds = {"availablecash": "3000", "m2mrealized": "0", "m2munrealized": "0"}
    assert "Margin" in failed(preview(c_user))
    world.funds = {"availablecash": "500000", "m2mrealized": "-3000", "m2munrealized": "-2500"}
    assert "Daily loss" in failed(preview(c_user))
    world.funds = {"availablecash": "500000", "m2mrealized": "0", "m2munrealized": "0"}
    world.ltp = None
    assert "Price near market" in failed(preview(c_user))
    world.ltp = 100.0
    monkeypatch.setattr(orders, "market_open", lambda now: False)
    assert "Market hours" in failed(preview(c_user))
    assert world.placed == []


def test_margin_that_cannot_be_computed_blocks_a_sell_entry(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    world.margin = None
    p = preview(c_user, side="SELL")
    assert p.status_code == 200 and p.json()["ok"] is False and "Margin" in failed(p)
    world.margin = 30000.0
    ok = preview(c_user, side="SELL")
    assert ok.json()["ok"] is True and ok.json()["margin_required"] == 30000.0


def test_an_exit_is_allowed_even_when_the_loss_cap_and_margin_would_block_an_entry(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    world.positions = [{"tradingsymbol": SYM, "netqty": "-65"}]                                    # short one lot
    world.funds = {"availablecash": "10", "m2mrealized": "-9000", "m2munrealized": "0"}            # over the loss cap, no cash
    p = preview(c_user, side="BUY")
    assert p.json()["intent"] == "exit" and p.json()["ok"] is True
    assert send(c_user, p.json()["id"]).status_code == 200
    assert preview(c_user, side="SELL").json()["ok"] is False                                      # adding to the short is an entry: blocked


def test_an_order_that_flips_the_position_is_refused(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    world.positions = [{"tradingsymbol": SYM, "netqty": "-65"}]
    p = preview(c_user, side="BUY", lots=2)
    assert p.json()["intent"] == "flip" and "Position" in failed(p)


def test_the_master_switch_pulled_between_preview_and_send_stops_the_order(db, new_client, world):
    user, _, c_admin, c_user = setup(db, new_client)
    p = preview(c_user)
    assert p.json()["ok"] is True
    api(c_admin, "POST", "/api/admin/broker/master", json={"enabled": False})
    r = send(c_user, p.json()["id"])
    assert r.status_code == 409 and r.json()["error"] == "CHECK_FAILED" and "Real mode" in failed(r) and world.placed == []


def test_a_preview_expires_and_belongs_to_one_user(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    p = preview(c_user)
    db.execute("UPDATE broker_orders SET created_at=created_at-300 WHERE id=?", (p.json()["id"],))
    db.commit()
    r = send(c_user, p.json()["id"])
    assert r.status_code == 409 and r.json()["error"] == "EXPIRED" and world.placed == []
    other = make_user(db, "intruder", "user")
    c_other = new_client()
    assert login(c_other, other).status_code == 200
    p2 = preview(c_user)
    assert send(c_other, p2.json()["id"]).status_code == 404


def test_unknown_and_refused_outcomes_are_recorded_and_never_retried(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    world.place_result = angel_orders.Unknown("No answer from Angel One.")
    p = preview(c_user)
    r = send(c_user, p.json()["id"])
    assert r.status_code == 504 and r.json()["error"] == "UNKNOWN" and len(world.placed) == 1
    assert [o["status"] for o in api(c_user, "GET", "/api/me/broker/orders").json()["orders"]] == ["unknown"]
    assert send(c_user, p.json()["id"]).status_code == 409 and len(world.placed) == 1
    world.place_result = ao.AngelOneApiError("Insufficient funds", "data")
    p = preview(c_user)
    r = send(c_user, p.json()["id"])
    assert r.status_code == 502 and "Insufficient funds" in r.json()["message"] and len(world.placed) == 2


def test_cancel_a_placed_order_only(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    p = preview(c_user)
    assert api(c_user, "POST", f"/api/me/broker/orders/{p.json()['id']}/cancel").status_code == 409       # not placed yet
    send(c_user, p.json()["id"])
    r = api(c_user, "POST", f"/api/me/broker/orders/{p.json()['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and world.cancelled == ["OID1"]
    assert api(c_user, "POST", f"/api/me/broker/orders/{p.json()['id']}/cancel").status_code == 409


def test_cancel_still_works_with_the_master_switch_off(db, new_client, world):
    user, _, c_admin, c_user = setup(db, new_client)
    p = preview(c_user)
    send(c_user, p.json()["id"])
    api(c_admin, "POST", "/api/admin/broker/master", json={"enabled": False})
    assert api(c_user, "POST", f"/api/me/broker/orders/{p.json()['id']}/cancel").status_code == 200


def test_an_unlisted_contract_and_a_bad_order_are_refused(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    r = preview(c_user, strike=99999)
    assert r.status_code == 400 and r.json()["error"] == "NOT_LISTED"
    assert preview(c_user, side="HOLD").status_code == 400
    assert preview(c_user, lots=0).status_code == 400
    assert api(c_user, "POST", "/api/me/broker/orders/preview", json={"symbol": "NIFTY"}).status_code == 400


def test_the_daily_order_count_is_capped(db, new_client, world, monkeypatch):
    user, _, _, c_user = setup(db, new_client)
    monkeypatch.setattr(orders, "MAX_ORDERS_PER_DAY", 2)
    for _ in range(2):
        p = preview(c_user)
        assert send(c_user, p.json()["id"]).status_code == 200
    assert "Orders today" in failed(preview(c_user))


def test_the_admin_sees_every_order_and_a_user_does_not_see_the_admin_list(db, new_client, world):
    user, _, c_admin, c_user = setup(db, new_client)
    send(c_user, preview(c_user).json()["id"])
    rows = c_admin.get("/api/admin/broker/orders").json()["orders"]
    assert len(rows) == 1 and rows[0]["username"] == "tradero" and rows[0]["status"] == "placed"
    assert c_user.get("/api/admin/broker/orders").status_code == 403


def test_contract_list_for_the_ticket(db, new_client, world):
    user, _, _, c_user = setup(db, new_client)
    r = api(c_user, "GET", "/api/me/broker/contracts?symbol=NIFTY")
    assert r.status_code == 200 and r.json()["expiries"] == ["2026-11-10"] and r.json()["strikes"]["2026-11-10"] == [25000.0] and r.json()["lot_size"] == 65
    assert api(c_user, "GET", "/api/me/broker/contracts?symbol=SENSEX").status_code == 400


def test_option_orders_are_off_by_default_so_nothing_can_be_previewed_or_sent(db, new_client, world, monkeypatch):
    user, _, _, c_user = setup(db, new_client)
    monkeypatch.setitem(flags.DEFAULTS, "real.options", False)
    flags._cache = None
    r = preview(c_user)
    assert r.status_code == 403 and r.json()["error"] == "OPTIONS_OFF" and world.placed == []
    assert api(c_user, "POST", "/api/me/broker/orders/abc/send").status_code == 403
    assert c_user.get("/api/me/broker").json()["options_orders"] is False
