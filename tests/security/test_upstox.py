"""Developer console > Upstox: the app details are saved encrypted, the login goes through Upstox's own page (OAuth) and comes back with a one-time code
that is exchanged ON THE SERVER, the token is kept encrypted until Upstox ends it (about 03:30 IST), and only today's trades can be read. No network:
the Upstox calls are replaced by fakes."""
import datetime as dt
import json
import urllib.parse

import pytest

from app.providers import upstox_client
from app.services import upstox_credential_store as store
from tests.security.helpers import api, login, make_owner, make_user

KEY, SECRET = "7c1f3e2a-5b8d-4f0e-9a21-abcdef123456", "s3cr3tvalue99"
TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.upstoxtokenpayload.sig"
REDIRECT = "http://127.0.0.1:8020/app/dev/upstox"
APP = {"api_key": KEY, "api_secret": SECRET, "redirect_uri": REDIRECT}
SECRETS = (SECRET, TOKEN, KEY)


@pytest.fixture(autouse=True)
def upstox_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CREDENTIAL_PATH", tmp_path / "upstox_credentials.json")
    monkeypatch.setattr("app.ops.upstox_api._states", {})


@pytest.fixture
def fake_upstox(monkeypatch):
    s = type("S", (), {})()
    s.exchanges, s.fail, s.trades, s.trade_fail = [], None, [{"trade_id": "T1", "trading_symbol": "BANKNIFTY26OCT55000CE", "transaction_type": "BUY",
                                                              "quantity": 30, "average_price": 101.5, "access_token": "must-not-leak"}], None

    async def exchange_code(code, api_key, api_secret, redirect_uri):
        s.exchanges.append((code, api_key, api_secret, redirect_uri))
        if s.fail:
            raise s.fail
        return {"access_token": TOKEN, "user_id": "AB1234", "user_name": "Tanisha Rathod", "email": "a@b.c"}

    async def get_trades_today(token):
        assert token == TOKEN
        if s.trade_fail:
            raise s.trade_fail
        return s.trades

    monkeypatch.setattr(upstox_client, "exchange_code", exchange_code)
    monkeypatch.setattr(upstox_client, "get_trades_today", get_trades_today)
    return s


@pytest.fixture
def dev(db, new_client):
    c = new_client()
    assert login(c, make_owner(db)).status_code == 200
    return c


def clean(text: str) -> None:
    for s in SECRETS:
        assert s not in text


def start_login(dev) -> str:
    assert api(dev, "PUT", "/api/dev/upstox/app", json=APP).status_code == 200
    url = api(dev, "POST", "/api/dev/upstox/login").json()["url"]
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]


def connect(dev, fake_upstox):
    state = start_login(dev)
    r = api(dev, "POST", "/api/dev/upstox/callback", json={"code": "one-time-code", "state": state})
    assert r.status_code == 200
    return r


# ---- who may use it -------------------------------------------------------------------------------------------------------------------------
def test_only_a_developer_uses_the_upstox_page(db, new_client, fake_upstox):
    make_owner(db)
    admin, user = make_user(db, "adminx", "admin"), make_user(db, "userx", "user")
    calls = [("GET", "/api/dev/upstox"), ("PUT", "/api/dev/upstox/app"), ("POST", "/api/dev/upstox/login"), ("POST", "/api/dev/upstox/callback"),
             ("GET", "/api/dev/upstox/trades"), ("DELETE", "/api/dev/upstox")]
    anon = new_client()
    for method, url in calls:
        assert api(anon, method, url, json=APP).status_code == 401, url
    for who in (admin, user):
        c = new_client()
        assert login(c, who).status_code == 200
        for method, url in calls:
            assert api(c, method, url, json=APP).status_code == 403, (who.role, url)
    assert not store.CREDENTIAL_PATH.exists() and fake_upstox.exchanges == []


def test_changing_anything_needs_the_csrf_token(dev):
    assert dev.put("/api/dev/upstox/app", json=APP).status_code == 403
    assert dev.delete("/api/dev/upstox").status_code == 403


# ---- the app details ----------------------------------------------------------------------------------------------------------------------
def test_nothing_is_saved_at_first(dev):
    s = dev.get("/api/dev/upstox").json()
    assert s["state"] == "none" and s["api_key"] is None and s["redirect_uri"] is None


def test_the_app_details_are_saved_encrypted_and_only_a_masked_key_comes_back(dev):
    r = api(dev, "PUT", "/api/dev/upstox/app", json=APP)
    assert r.status_code == 200
    s = r.json()
    assert s["state"] == "app_only" and s["redirect_uri"] == REDIRECT and s["updated_by"] == "owner" and s["api_key"] != KEY and s["api_key"].startswith("7c1")
    clean(r.text)
    clean(dev.get("/api/dev/upstox").text)
    on_disk = store.CREDENTIAL_PATH.read_text()
    clean(on_disk)
    assert set(json.loads(on_disk)) == {"enc"}
    assert store.get().api_secret == SECRET


@pytest.mark.parametrize("body,code", [({**APP, "api_key": "short"}, "BAD_DETAILS"), ({**APP, "api_secret": " "}, "BAD_DETAILS"),
                                       ({**APP, "redirect_uri": "127.0.0.1:8020/x"}, "BAD_REDIRECT"), ({**APP, "redirect_uri": "http://a b/x"}, "BAD_REDIRECT"),
                                       ({**APP, "redirect_uri": "http://127.0.0.1/x#frag"}, "BAD_REDIRECT")])
def test_bad_app_details_get_a_sentence_and_save_nothing(dev, body, code):
    r = api(dev, "PUT", "/api/dev/upstox/app", json=body)
    assert r.status_code == 400 and r.json()["error"] == code
    assert not store.CREDENTIAL_PATH.exists()


# ---- the login --------------------------------------------------------------------------------------------------------------------------
def test_the_login_needs_the_app_details_first(dev):
    r = api(dev, "POST", "/api/dev/upstox/login")
    assert r.status_code == 409 and r.json()["error"] == "NO_APP"


def test_the_login_address_is_upstoxs_own_page_with_the_key_and_the_way_back(dev):
    assert api(dev, "PUT", "/api/dev/upstox/app", json=APP).status_code == 200
    url = api(dev, "POST", "/api/dev/upstox/login").json()["url"]
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    assert f"{p.scheme}://{p.netloc}{p.path}" == "https://api.upstox.com/v2/login/authorization/dialog"
    assert q["client_id"] == [KEY] and q["redirect_uri"] == [REDIRECT] and q["response_type"] == ["code"] and len(q["state"][0]) >= 20
    assert SECRET not in url


def test_coming_back_with_the_code_logs_in_and_the_token_never_reaches_the_browser(dev, fake_upstox):
    r = connect(dev, fake_upstox)
    s = r.json()
    assert s["state"] == "connected" and s["user_id"] == "AB1234" and s["user_name"] == "Tanisha Rathod" and s["connected_at"] and s["updated_by"] == "owner"
    clean(r.text)
    assert fake_upstox.exchanges == [("one-time-code", KEY, SECRET, REDIRECT)]       # the secret is used here, on the server
    clean(store.CREDENTIAL_PATH.read_text())
    assert store.get().access_token == TOKEN
    clean(dev.get("/api/dev/upstox").text)


def test_the_token_ends_at_the_next_0330_ist():
    ist = store.IST
    assert store.next_token_expiry(dt.datetime(2026, 10, 26, 10, 0, tzinfo=ist)) == dt.datetime(2026, 10, 27, 3, 30, tzinfo=ist)
    assert store.next_token_expiry(dt.datetime(2026, 10, 26, 2, 0, tzinfo=ist)) == dt.datetime(2026, 10, 26, 3, 30, tzinfo=ist)
    assert store.next_token_expiry(dt.datetime(2026, 10, 26, 3, 30, tzinfo=ist)) == dt.datetime(2026, 10, 27, 3, 30, tzinfo=ist)
    utc_night = dt.datetime(2026, 10, 26, 20, 0, tzinfo=dt.timezone.utc)                # 01:30 IST the next day
    assert store.next_token_expiry(utc_night) == dt.datetime(2026, 10, 27, 3, 30, tzinfo=ist)


def test_an_ended_token_shows_as_expired(dev, fake_upstox):
    connect(dev, fake_upstox)
    c = store.get()
    c.token_expires_at = (dt.datetime.now(store.IST) - dt.timedelta(minutes=1)).isoformat()
    store.save(c)
    assert dev.get("/api/dev/upstox").json()["state"] == "expired"
    r = dev.get("/api/dev/upstox/trades")
    assert r.status_code == 409 and r.json()["error"] == "NOT_CONNECTED" and "03:30" in r.json()["message"]


def test_a_state_that_was_not_started_here_is_refused_and_nothing_is_exchanged(dev, fake_upstox):
    assert api(dev, "PUT", "/api/dev/upstox/app", json=APP).status_code == 200
    r = api(dev, "POST", "/api/dev/upstox/callback", json={"code": "c", "state": "made-up-by-someone-else"})
    assert r.status_code == 400 and r.json()["error"] == "BAD_STATE"
    assert fake_upstox.exchanges == [] and store.get().access_token is None


def test_a_state_works_once_only(dev, fake_upstox):
    state = start_login(dev)
    assert api(dev, "POST", "/api/dev/upstox/callback", json={"code": "c", "state": state}).status_code == 200
    again = api(dev, "POST", "/api/dev/upstox/callback", json={"code": "c", "state": state})
    assert again.status_code == 400 and len(fake_upstox.exchanges) == 1


def test_a_state_of_another_developer_is_refused(db, new_client, fake_upstox):
    owner = make_owner(db)
    c1 = new_client()
    assert login(c1, owner).status_code == 200
    state = start_login(c1)
    other = make_user(db, "dev2", "developer", created_by=owner.id)
    c2 = new_client()
    assert login(c2, other).status_code == 200
    assert api(c2, "POST", "/api/dev/upstox/callback", json={"code": "c", "state": state}).json()["error"] == "BAD_STATE"
    assert fake_upstox.exchanges == []


def test_an_old_state_is_refused(dev, fake_upstox, monkeypatch):
    state = start_login(dev)
    monkeypatch.setattr("app.ops.upstox_api.STATE_TTL_S", -1)
    from app.ops import upstox_api
    owner_id, _ = upstox_api._states[state]
    upstox_api._states[state] = (owner_id, 0.0)
    assert api(dev, "POST", "/api/dev/upstox/callback", json={"code": "c", "state": state}).json()["error"] == "BAD_STATE"


def test_upstox_refusing_the_code_saves_no_token_and_says_why(dev, fake_upstox):
    state = start_login(dev)
    fake_upstox.fail = upstox_client.UpstoxApiError("Invalid Auth code", "auth")
    r = api(dev, "POST", "/api/dev/upstox/callback", json={"code": "stale", "state": state})
    assert r.status_code == 400 and r.json()["error"] == "UPSTOX_REJECTED" and "Invalid Auth code" in r.json()["message"]
    assert store.get().access_token is None and dev.get("/api/dev/upstox").json()["state"] == "app_only"
    assert "dev.upstox.connect_failed" in dev.get("/api/admin/audit?limit=20").text


def test_upstox_being_unreachable_is_a_different_answer(dev, fake_upstox):
    state = start_login(dev)
    fake_upstox.fail = upstox_client.UpstoxApiError("Could not reach Upstox: timeout", "network")
    r = api(dev, "POST", "/api/dev/upstox/callback", json={"code": "c", "state": state})
    assert r.status_code == 502 and r.json()["error"] == "UPSTOX_UNREACHABLE"


# ---- trades -----------------------------------------------------------------------------------------------------------------------------
def test_todays_trades_are_read_only_and_only_known_fields_are_passed_on(dev, fake_upstox):
    assert dev.get("/api/dev/upstox/trades").json()["error"] == "NOT_CONNECTED"
    connect(dev, fake_upstox)
    r = dev.get("/api/dev/upstox/trades")
    assert r.status_code == 200 and r.json()["count"] == 1
    t = r.json()["trades"][0]
    assert t["trading_symbol"] == "BANKNIFTY26OCT55000CE" and t["quantity"] == 30 and "access_token" not in t
    clean(r.text)
    assert "must-not-leak" not in r.text


def test_when_upstox_ends_the_login_early_the_token_is_forgotten(dev, fake_upstox):
    connect(dev, fake_upstox)
    fake_upstox.trade_fail = upstox_client.UpstoxApiError("Invalid token used to access API", "auth")
    r = dev.get("/api/dev/upstox/trades")
    assert r.status_code == 409 and "Connect with Upstox" in r.json()["message"]
    assert dev.get("/api/dev/upstox").json()["state"] == "app_only"


def test_upstox_trouble_while_reading_trades_keeps_the_login(dev, fake_upstox):
    connect(dev, fake_upstox)
    fake_upstox.trade_fail = upstox_client.UpstoxApiError("Could not reach Upstox: timeout", "network")
    assert dev.get("/api/dev/upstox/trades").status_code == 502
    assert dev.get("/api/dev/upstox").json()["state"] == "connected"


# ---- disconnect, audit, the old file ---------------------------------------------------------------------------------------------------
def test_disconnect_removes_everything(dev, fake_upstox):
    connect(dev, fake_upstox)
    r = api(dev, "DELETE", "/api/dev/upstox")
    assert r.status_code == 200 and r.json()["state"] == "none" and not store.CREDENTIAL_PATH.exists()


def test_the_audit_log_has_who_and_what_but_no_secret_or_token(dev, fake_upstox):
    connect(dev, fake_upstox)
    api(dev, "DELETE", "/api/dev/upstox")
    text = dev.get("/api/admin/audit?limit=30").text
    for a in ("dev.upstox.app_saved", "dev.upstox.login_started", "dev.upstox.connect", "dev.upstox.disconnect"):
        assert a in text, a
    clean(text)


def test_saving_new_app_details_drops_the_old_login(dev, fake_upstox):
    connect(dev, fake_upstox)
    assert api(dev, "PUT", "/api/dev/upstox/app", json={**APP, "api_secret": "anothersecret1"}).json()["state"] == "app_only"
    assert store.get().access_token is None


def test_a_file_that_cannot_be_read_is_ignored_not_a_crash(dev):
    store.CREDENTIAL_PATH.write_text(json.dumps({"enc": "garbage"}))
    assert store.get() is None and dev.get("/api/dev/upstox").json()["state"] == "none"
    store.CREDENTIAL_PATH.write_text("{ damaged")
    assert store.get() is None


# ---- the Upstox client itself (HTTP faked) ------------------------------------------------------------------------------------------------
def test_the_client_posts_the_secret_as_a_form_and_reads_the_token():
    import asyncio

    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), request.content.decode()
        return httpx.Response(200, json={"access_token": "tok", "user_id": "U1"})

    real = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        out = asyncio.run(upstox_client.exchange_code("the-code", KEY, SECRET, REDIRECT))
    finally:
        httpx.AsyncClient = real
    assert out["access_token"] == "tok" and seen["url"] == upstox_client.TOKEN_URL
    form = urllib.parse.parse_qs(seen["body"])
    assert form["code"] == ["the-code"] and form["client_secret"] == [SECRET] and form["grant_type"] == ["authorization_code"]


@pytest.mark.parametrize("status,body,kind", [(401, {"errors": [{"message": "Invalid token"}]}, "auth"), (200, {"status": "error"}, "data"), (500, {}, "data")])
def test_the_client_tells_a_refused_login_from_a_broken_answer(status, body, kind):
    import asyncio

    import httpx

    real = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)), **kw)
    try:
        with pytest.raises(upstox_client.UpstoxApiError) as e:
            asyncio.run(upstox_client.get_trades_today("tok"))
    finally:
        httpx.AsyncClient = real
    assert e.value.kind == kind
