"""The React app is served under /app. Its shell (HTML) is public so the login page can load; every data call still needs a session."""
from app import main
from tests.security.helpers import make_user


def test_the_shell_is_public_but_the_data_is_not(db, client, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!doctype html><title>Algo Trading</title><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path)
    make_user(db, "anil")                                                          # platform mode is on: login required everywhere else
    for path in ("/app", "/app/", "/app/login", "/app/top-bottom", "/app/setup/sometoken"):
        r = client.get(path)
        assert r.status_code == 200 and "<div id=root>" in r.text, path
        assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/top-bottom", follow_redirects=False).status_code == 303   # the old pages are still protected


def test_a_missing_build_says_how_to_build_it(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path / "nothing")
    r = client.get("/app")
    assert r.status_code == 503 and "npm" in r.text
