"""Developer console > Angel One connection: a developer types the broker details into the console; they are tested with a real login first
(here a fake one: no network), saved ENCRYPTED, handed to the running app and the live feed reconnects. Nothing secret is ever sent back."""
import json
import types

import pytest

from app.ops import context
from app.ops import api as ops_api
from app.providers import angelone_client
from app.services import angelone_credential_store as store
from tests.security.helpers import api, login, make_owner, make_user

API_KEY, CODE, PIN = "fdizvl3Y", "G56798406", "9424"
SECRET = "7VQCXK44UJO3742SCNVFKQD2CY"
SECRETS = (API_KEY, PIN, SECRET)
GOOD = {"api_key": API_KEY, "client_code": CODE, "pin": PIN, "totp_secret": SECRET}


@pytest.fixture(autouse=True)
def broker_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CREDENTIAL_PATH", tmp_path / "angelone_credentials.json")
    for k in ("api_key", "client_code", "pin", "totp_secret"):
        monkeypatch.setattr(store.settings, f"angelone_{k}", "")
    monkeypatch.setattr(ops_api, "BROKER_COOLDOWN_S", 0.0)
    monkeypatch.setattr(ops_api, "_broker_last_try", 0.0)
    monkeypatch.setattr(context, "engine", None)


class FakeFeed:
    def __init__(self):
        self.restarts = 0

    async def restart(self):
        self.restarts += 1

    def status(self):
        return {"connected": True, "last_tick_age_s": 0.4, "reconnects": 0, "subscribed": 2, "error": None}


class FakeProvider:
    def __init__(self):
        self.adopted, self.resets = [], 0

    def adopt_session(self, s):
        self.adopted.append(s)

    def reset_session(self):
        self.resets += 1


@pytest.fixture
def engine(monkeypatch):
    e = types.SimpleNamespace(provider=FakeProvider(), feed=FakeFeed())
    monkeypatch.setattr(context, "engine", e)
    return e


@pytest.fixture
def broker_login(monkeypatch):
    """Replace the real Angel One login: records what it was given and answers like Angel One would."""
    state = types.SimpleNamespace(calls=[], fail=None)

    async def fake_login(api_key, client_code, pin, totp_secret):
        state.calls.append((api_key, client_code, pin, totp_secret))
        if state.fail:
            raise state.fail
        return angelone_client.AngelOneSession(api_key=api_key, jwt_token="jwt", feed_token="feed", refresh_token="r", client_code=client_code)

    monkeypatch.setattr(angelone_client, "login", fake_login)
    return state


@pytest.fixture
def dev(db, new_client):
    owner = make_owner(db)
    c = new_client()
    assert login(c, owner).status_code == 200
    return c


def no_secret_in(text: str) -> None:
    for s in SECRETS:
        assert s not in text


# ---- who may use it -----------------------------------------------------------------------------------------------------------------------
def test_only_a_developer_sees_or_changes_the_broker_details(db, new_client, broker_login):
    owner, admin, user = make_owner(db), make_user(db, "adminx", "admin"), make_user(db, "userx", "user")
    anon = new_client()
    assert anon.get("/api/dev/broker").status_code == 401
    assert api(anon, "PUT", "/api/dev/broker", json=GOOD).status_code == 401
    for who in (admin, user):
        c = new_client()
        assert login(c, who).status_code == 200
        assert c.get("/api/dev/broker").status_code == 403
        assert api(c, "PUT", "/api/dev/broker", json=GOOD).status_code == 403
        assert api(c, "DELETE", "/api/dev/broker").status_code == 403
    assert broker_login.calls == [] and not store.CREDENTIAL_PATH.exists()          # refused before anything happened


def test_changing_the_details_needs_the_csrf_token(dev, broker_login):
    assert dev.put("/api/dev/broker", json=GOOD).status_code == 403
    assert broker_login.calls == []


# ---- connecting ---------------------------------------------------------------------------------------------------------------------------
def test_not_configured_at_first(dev):
    r = dev.get("/api/dev/broker").json()
    assert r["configured"] is False and r["source"] == "none" and r["client_code"] is None and r["feed"] is None


def test_a_good_login_is_saved_encrypted_and_never_echoed(dev, broker_login):
    r = api(dev, "PUT", "/api/dev/broker", json=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] and body["source"] == "saved" and body["encrypted"] is True and body["connected_at"] and body["updated_by"] == "owner"
    assert body["client_code"] == "G••••••06"
    no_secret_in(r.text)
    assert broker_login.calls == [(API_KEY, CODE, PIN, SECRET)]
    on_disk = store.CREDENTIAL_PATH.read_text()
    no_secret_in(on_disk)
    assert CODE not in on_disk and set(json.loads(on_disk)) == {"enc"}
    creds = store.get_credentials()
    assert (creds.api_key, creds.client_code, creds.pin, creds.totp_secret) == (API_KEY, CODE, PIN, SECRET)
    again = dev.get("/api/dev/broker")
    no_secret_in(again.text)
    assert again.json()["client_code"] == "G••••••06"


def test_the_values_are_cleaned_up_before_the_login(dev, broker_login):
    r = api(dev, "PUT", "/api/dev/broker", json={"api_key": f" {API_KEY} ", "client_code": " g56798406 ", "pin": " 9424 ",
                                                 "totp_secret": "7vqc xk44 ujo3 742s cnvf kqd2 cy"})
    assert r.status_code == 200
    assert broker_login.calls == [(API_KEY, CODE, PIN, SECRET)]


def test_saving_gives_the_running_app_the_new_session_and_reconnects_the_feed_once(dev, broker_login, engine):
    r = api(dev, "PUT", "/api/dev/broker", json=GOOD)
    assert r.status_code == 200 and r.json()["feed"]["connected"] is True
    assert len(broker_login.calls) == 1                         # ONE Angel One login for the whole change
    assert len(engine.provider.adopted) == 1 and engine.provider.adopted[0].jwt_token == "jwt"
    assert engine.feed.restarts == 1


def test_the_audit_log_records_who_and_which_client_but_no_secret(dev, broker_login):
    assert api(dev, "PUT", "/api/dev/broker", json=GOOD).status_code == 200
    text = dev.get("/api/admin/audit?limit=20").text
    assert "dev.broker.connect" in text and "G••••••06" in text.replace("\\u2022", "•")
    no_secret_in(text)


# ---- refusals leave everything as it was --------------------------------------------------------------------------------------------------
def test_details_angel_one_refuses_are_not_saved_and_the_old_ones_stay(dev, broker_login, engine):
    assert api(dev, "PUT", "/api/dev/broker", json=GOOD).status_code == 200
    before = store.CREDENTIAL_PATH.read_text()
    engine.feed.restarts, engine.provider.adopted = 0, []
    broker_login.fail = angelone_client.AngelOneApiError("Invalid totp", "auth")
    r = api(dev, "PUT", "/api/dev/broker", json={**GOOD, "pin": "1111"})
    assert r.status_code == 400 and r.json()["error"] == "BROKER_REJECTED" and "Invalid totp" in r.json()["message"] and "Nothing was saved" in r.json()["message"]
    no_secret_in(r.text)
    assert store.CREDENTIAL_PATH.read_text() == before
    assert store.get_credentials().pin == PIN
    assert engine.feed.restarts == 0 and engine.provider.adopted == []
    assert "dev.broker.connect_failed" in dev.get("/api/admin/audit?limit=20").text


def test_angel_one_being_unreachable_is_a_different_answer_and_saves_nothing(dev, broker_login):
    broker_login.fail = angelone_client.AngelOneApiError("Could not reach Angel One: timeout", "network", retryable=True)
    r = api(dev, "PUT", "/api/dev/broker", json=GOOD)
    assert r.status_code == 502 and r.json()["error"] == "BROKER_UNREACHABLE"
    assert not store.CREDENTIAL_PATH.exists()


@pytest.mark.parametrize("secret", ["123456", "not base32 !!", "ABC"])
def test_a_totp_secret_that_is_not_the_long_key_is_refused_before_any_login(dev, broker_login, secret):
    r = api(dev, "PUT", "/api/dev/broker", json={**GOOD, "totp_secret": secret})
    assert r.status_code in (400, 422)
    assert broker_login.calls == [] and not store.CREDENTIAL_PATH.exists()


def test_missing_fields_are_refused(dev, broker_login):
    assert api(dev, "PUT", "/api/dev/broker", json={"api_key": API_KEY}).status_code == 422
    assert broker_login.calls == []


def test_tries_cannot_be_hammered(dev, broker_login, monkeypatch):
    monkeypatch.setattr(ops_api, "BROKER_COOLDOWN_S", 30.0)
    assert api(dev, "PUT", "/api/dev/broker", json=GOOD).status_code == 200
    r = api(dev, "PUT", "/api/dev/broker", json=GOOD)
    assert r.status_code == 429 and r.json()["error"] == "TOO_FAST"
    assert len(broker_login.calls) == 1


# ---- disconnecting, the old file, the environment ----------------------------------------------------------------------------------------
def test_disconnect_removes_the_saved_details_and_restarts_the_feed(dev, broker_login, engine):
    assert api(dev, "PUT", "/api/dev/broker", json=GOOD).status_code == 200
    engine.feed.restarts = 0
    r = api(dev, "DELETE", "/api/dev/broker")
    assert r.status_code == 200 and r.json()["configured"] is False and r.json()["source"] == "none"
    assert not store.CREDENTIAL_PATH.exists()
    assert engine.provider.resets == 1 and engine.feed.restarts == 1
    assert "dev.broker.disconnect" in dev.get("/api/admin/audit?limit=20").text


def test_after_a_disconnect_the_server_environment_is_used_again(dev, broker_login, monkeypatch):
    assert api(dev, "PUT", "/api/dev/broker", json=GOOD).status_code == 200
    for k, v in (("api_key", "ENVKEY99"), ("client_code", "E1234567"), ("pin", "1234"), ("totp_secret", "A" * 32)):
        monkeypatch.setattr(store.settings, f"angelone_{k}", v)
    r = api(dev, "DELETE", "/api/dev/broker").json()
    assert r["configured"] is True and r["source"] == "env" and r["client_code"] == "E•••••67"


def test_saved_details_win_over_the_environment(dev, broker_login, monkeypatch):
    for k, v in (("api_key", "ENVKEY99"), ("client_code", "E1234567"), ("pin", "1234"), ("totp_secret", "A" * 32)):
        monkeypatch.setattr(store.settings, f"angelone_{k}", v)
    assert dev.get("/api/dev/broker").json()["source"] == "env"
    assert api(dev, "PUT", "/api/dev/broker", json=GOOD).status_code == 200
    assert dev.get("/api/dev/broker").json()["source"] == "saved" and store.get_credentials().client_code == CODE


def test_a_file_from_the_older_version_is_still_read_and_replaced_by_an_encrypted_one(dev, broker_login):
    store.CREDENTIAL_PATH.write_text(json.dumps({"api_key": API_KEY, "client_code": CODE, "pin": PIN, "totp_secret": SECRET, "connected_at": None}))
    s = dev.get("/api/dev/broker").json()
    assert s["configured"] and s["source"] == "saved" and s["encrypted"] is False
    assert store.get_credentials().totp_secret == SECRET
    assert api(dev, "PUT", "/api/dev/broker", json=GOOD).json()["encrypted"] is True
    no_secret_in(store.CREDENTIAL_PATH.read_text())


def test_a_file_that_cannot_be_decrypted_is_ignored_not_a_crash(dev, monkeypatch):
    store.CREDENTIAL_PATH.write_text(json.dumps({"enc": "not-a-valid-token"}))
    assert store.get_credentials() is None
    assert dev.get("/api/dev/broker").json()["configured"] is False
    store.CREDENTIAL_PATH.write_text("{ damaged")
    assert store.get_credentials() is None


def test_no_endpoint_answers_with_a_secret_even_in_errors(dev, broker_login):
    broker_login.fail = angelone_client.AngelOneApiError("Invalid totp", "auth")
    for r in (api(dev, "PUT", "/api/dev/broker", json=GOOD), dev.get("/api/dev/broker"), dev.get("/api/dev/system-info")):
        no_secret_in(r.text)


# ---- the feed ----------------------------------------------------------------------------------------------------------------------------
def test_the_live_feed_can_be_restarted_and_keeps_its_subscriptions():
    import asyncio

    from app.live.ticks import TickFeed

    async def run():
        sessions = []

        async def get_session():
            sessions.append(1)
            raise RuntimeError("no network in tests")             # the loop keeps retrying; only the restart behaviour is checked

        feed = TickFeed(get_session)
        feed.subscribe("NSE", "99926009")
        feed.start()
        await asyncio.sleep(0.05)
        first = feed._task
        await feed.restart()
        await asyncio.sleep(0.05)
        assert feed._task is not first and first.cancelled() or first.done()
        assert not feed._task.done() and feed.subs == {1: {"99926009"}}
        assert feed.last_error is None or "no network" in feed.last_error
        await feed.stop()

    asyncio.run(run())


def test_a_wrong_looking_value_gets_a_sentence_not_a_bare_422(dev, broker_login):
    r = api(dev, "PUT", "/api/dev/broker", json={**GOOD, "totp_secret": "123456"})
    assert r.status_code == 400 and r.json()["error"] == "BAD_TOTP_SECRET" and "6-digit" in r.json()["message"]
    r = api(dev, "PUT", "/api/dev/broker", json={**GOOD, "pin": "  "})
    assert r.status_code == 400 and r.json()["error"] == "BAD_DETAILS"
    r = api(dev, "PUT", "/api/dev/broker", json={**GOOD, "api_key": "ab"})
    assert r.status_code == 400 and r.json()["error"] == "BAD_DETAILS"
    assert broker_login.calls == []
