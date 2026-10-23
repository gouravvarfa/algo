"""Every address of the old pages leads to the same page in the React app; the React app itself is tested in frontend/ (vitest)."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

OLD = {"/top-bottom": "/app/top-bottom", "/journal": "/app/top-bottom", "/simulator": "/app/simulator", "/builder": "/app/builder", "/backtesting": "/app/backtesting",
       "/history": "/app/history", "/options": "/app/options", "/settings": "/app/settings", "/": "/app/"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app)          # no lifespan: no broker session, no background engine


@pytest.mark.parametrize("old,new", OLD.items())
def test_old_addresses_lead_to_the_react_app(client, old, new):
    r = client.get(old, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == new


def test_the_query_string_survives_the_redirect(client):
    assert client.get("/simulator?x=1", follow_redirects=False).headers["location"] == "/app/simulator?x=1"


def test_the_removed_dashboard_replay_and_breakout_pages_stay_gone(client):
    for path in ("/replay", "/breakout15"):
        assert client.get(path, follow_redirects=False).status_code == 404


def test_health_check_needs_no_login(client):
    assert client.get("/healthz").status_code == 200
