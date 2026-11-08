"""Market calendar (holidays close the market, NSE list is added automatically, notices ahead of time), a user's activity list, Angel One API health."""
import datetime as dt

from app.ops import market_calendar as mc
from app.paper_trading.engine import market_open
from app.providers import api_health
from app.strategy.top_bottom.gap_protocol import MCX, NSE
from tests.security.helpers import api, login, make_owner, make_user

import pytest


@pytest.fixture(autouse=True)
def _fresh_calendar_cache():
    mc.invalidate()
    yield
    mc.invalidate()                                                           # never leak a test holiday into another test's market hours


NSE_ANSWER = {"FO": [{"tradingDate": "10-Nov-2026", "description": "Diwali-Balipratipada"}, {"tradingDate": "08-Nov-2026", "description": "Diwali Laxmi Pujan*"},
                     {"tradingDate": "bad", "description": "x"}], "CM": []}


def test_a_holiday_closes_the_market_for_its_exchange_only(db):
    mc.put(db, "2026-11-10", "NSE", "holiday", None, None, "Diwali", "admin")
    tue = dt.datetime(2026, 11, 10, 11, 0)
    assert not market_open(tue, NSE)
    assert market_open(tue, MCX)                                              # MCX keeps its own calendar
    assert market_open(dt.datetime(2026, 11, 9, 11, 0), NSE)                  # the day before is a normal Monday
    mc.delete(db, "2026-11-10", "NSE")
    assert market_open(tue, NSE)


def test_the_nse_list_is_parsed_and_never_overwrites_a_hand_entry(db):
    days = mc.parse_nse(NSE_ANSWER)
    assert days == [("2026-11-10", "Diwali-Balipratipada"), ("2026-11-08", "Diwali Laxmi Pujan (Muhurat trading session)")]
    mc.put(db, "2026-11-10", "NSE", "holiday", None, None, "my own note", "admin")
    mc.apply_nse(db, days)
    rows = {r["date"]: dict(r) for r in db.execute("SELECT * FROM market_calendar")}
    assert rows["2026-11-10"]["note"] == "my own note" and rows["2026-11-08"]["created_by"] == mc.AUTO_BY


def test_a_special_session_needs_valid_times(db):
    import pytest
    with pytest.raises(ValueError):
        mc.put(db, "2026-11-08", "NSE", "special", "15:15", "14:15", "Muhurat", "admin")
    mc.put(db, "2026-11-08", "NSE", "special", "14:15", "15:15", "Muhurat", "admin")
    assert not market_open(dt.datetime(2026, 11, 8, 14, 30), NSE)              # the strategy stays off in a special session


def test_notices_come_on_the_day_and_ahead(db, new_client, monkeypatch):
    from app import admin_extra
    u = make_user(db, "noticeuser", "user")
    cl = new_client()
    assert login(cl, u).status_code == 200
    today = dt.date.today()
    mc.put(db, (today + dt.timedelta(days=1)).isoformat(), "NSE", "holiday", None, None, "Test holiday", "admin")
    cal = cl.get("/api/announcement").json()["calendar"]
    assert len(cal) == 1 and cal[0]["ahead"] and cal[0]["text"].startswith("Kal") and "band rahega" in cal[0]["text"]


def test_only_an_admin_edits_the_calendar(db, new_client):
    owner, user = make_owner(db), make_user(db, "plainuser", "user")
    c_owner, c_user = new_client(), new_client()
    assert login(c_owner, owner).status_code == 200 and login(c_user, user).status_code == 200
    body = {"date": "2026-12-25", "exchange": "NSE", "kind": "holiday", "note": "Christmas"}
    assert api(c_user, "PUT", "/api/admin/calendar", json=body).status_code in (401, 403)
    r = api(c_owner, "PUT", "/api/admin/calendar", json=body)
    assert r.status_code == 200 and any(e["date"] == "2026-12-25" for e in r.json()["entries"])
    assert api(c_owner, "DELETE", "/api/admin/calendar/2026-12-25/NSE").status_code == 200


def test_a_users_activity_lists_orders_and_changes_without_secrets(db, new_client):
    owner, user = make_owner(db), make_user(db, "actuser", "user")
    c_owner, c_user = new_client(), new_client()
    assert login(c_owner, owner).status_code == 200 and login(c_user, user).status_code == 200
    api(c_user, "PUT", "/api/me/broker", json={"client_code": "A123456", "api_key": "KEY12345", "pin": "1234", "totp_secret": "JBSWY3DPEHPK3PXP"})
    db.execute("INSERT INTO real_tb_orders (id, user_id, instrument_id, symbol, contract, role, side, qty, order_type, price, trigger_price, broker_order_id, status, why, message, created_at, updated_at) "
               "VALUES ('o1', ?, 'i1', 'BANKNIFTY', 'BANKNIFTY25NOV26FUT', 'entry', 'BUY', 30, 'LIMIT', 50003, NULL, 'B1', 'rejected', 'x', 'RMS: margin', 1794100000, 1794100000)", (user.id,))
    db.commit()
    r = c_owner.get(f"/api/admin/users/{user.id}/activity")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["type"] == "order" and i["status"] == "rejected" for i in items) and any(i["title"] == "broker.save" for i in items)
    text = str(items)
    assert "JBSWY3DPEHPK3PXP" not in text and "KEY12345" not in text and "1234," not in text
    assert c_user.get(f"/api/admin/users/{user.id}/activity").status_code in (401, 403)


def test_api_health_counts_times_errors_and_rate_limits():
    api_health.clear()
    import time
    now = time.time()
    api_health._EVENTS.extend([(now, "getLtpData", 120.0, 200, None), (now, "getLtpData", 300.0, 200, None), (now, "getLtpData", 50.0, 403, None),
                               (now, "placeOrder", 900.0, None, "ReadTimeout"), (now - 7200, "getRMS", 10.0, 200, None)])
    s = api_health.summary(3600)
    ltp = next(e for e in s["endpoints"] if e["endpoint"] == "getLtpData")
    assert s["calls"] == 4 and s["errors"] == 2 and s["rate_limited"] == 1 and s["status"] == "degraded"
    assert ltp["calls"] == 3 and ltp["p50_ms"] == 120.0 and ltp["rate_limited"] == 1
    assert not any(e["endpoint"] == "getRMS" for e in s["endpoints"])          # older than the window
    api_health.clear()


def test_live_logs_come_from_the_apps_own_copy_with_secrets_masked(db, new_client):
    import logging
    from app.ops import logbuffer
    logbuffer.install()
    logging.getLogger("app.test").warning("login ok token=abc123SECRET for user x")
    owner = make_owner(db)
    cl = new_client()
    assert login(cl, owner).status_code == 200
    d = cl.get("/api/dev/live-logs?lines=50").json()
    assert d["available"] and any("login ok" in x for x in d["lines"]) and not any("abc123SECRET" in x for x in d["lines"])
