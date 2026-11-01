"""Page loading: hashed assets are cached for good (a reload no longer asks for them again), a missing file is never cached, the nginx gzip file
covers the types the app sends, and the system pill names the part that is not fine. No network."""
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient

from app import main
from app.ops import api as ops_api, health


def _app():
    a = FastAPI()
    a.middleware("http")(main.static_revalidate)

    @a.get("/app/assets/index-abc123.js")
    def js():
        return PlainTextResponse("x", media_type="text/javascript")

    @a.get("/app/fonts/fonts.css")
    def fonts():
        return PlainTextResponse("x", media_type="text/css")

    @a.get("/app/login")
    def page():
        return PlainTextResponse("<html></html>", media_type="text/html")

    @a.get("/static/old.js")
    def old():
        return PlainTextResponse("x", media_type="text/javascript")
    return a


def test_hashed_assets_are_cached_for_good_and_a_missing_one_never_is():
    c = TestClient(_app())
    assert c.get("/app/assets/index-abc123.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    assert c.get("/app/fonts/fonts.css").headers["cache-control"] == "public, max-age=2592000"
    assert "cache-control" not in c.get("/app/login").headers or "immutable" not in c.get("/app/login").headers["cache-control"]    # the page itself is never pinned
    assert c.get("/static/old.js").headers["cache-control"] == "no-cache"
    missing = c.get("/app/assets/index-gone.js")
    assert missing.status_code == 404 and "immutable" not in missing.headers.get("cache-control", "")


def test_the_nginx_gzip_file_covers_scripts_styles_and_json_but_not_the_live_stream():
    text = open("deploy/nginx-gzip.conf", encoding="utf-8").read()
    types = next(line for line in text.splitlines() if line.startswith("gzip_types"))
    for t in ("application/javascript", "text/css", "application/json"):
        assert t in types
    assert "event-stream" not in types and "gzip_vary on;" in text


def test_the_system_summary_names_what_is_not_fine(monkeypatch):
    tiles = [health._tile("api", "API server", "OK", "fine"), health._tile("functions", "Tracked functions", "DOWN", "FAULTY: t.bad"),
             health._tile("feed", "Market feed (WebSocket)", "DEGRADED", "last tick is old"), health._tile("engine", "Strategy engine", "OK", "fine")]
    monkeypatch.setattr(health, "all_tiles", lambda: tiles)
    out = ops_api.admin_system_summary(None)
    assert out["overall"] == "DOWN"
    assert [p["key"] for p in out["problems"]] == ["functions", "feed"] and out["problems"][0]["reason"] == "FAULTY: t.bad"      # the worst first
    monkeypatch.setattr(health, "all_tiles", lambda: [health._tile("api", "API server", "OK", "fine")])
    assert ops_api.admin_system_summary(None)["problems"] == []


def test_scripts_and_json_are_compressed_but_the_live_stream_never_is():
    from app.gzip_mw import SelectiveGZip
    from fastapi.responses import StreamingResponse
    a = FastAPI()
    a.add_middleware(SelectiveGZip)
    big = "x" * 5000

    @a.get("/app/assets/index-abc.js")
    def js():
        return PlainTextResponse(big, media_type="text/javascript")

    @a.get("/api/data")
    def data():
        return {"rows": [big]}

    @a.get("/api/small")
    def small():
        return {"ok": True}

    @a.get("/api/stream")
    def stream():
        async def gen():
            yield b"event: snap\ndata: {}\n\n" + big.encode()
        return StreamingResponse(gen(), media_type="text/event-stream")

    c = TestClient(a)
    for path in ("/app/assets/index-abc.js", "/api/data"):
        r = c.get(path, headers={"accept-encoding": "gzip"})
        assert r.headers["content-encoding"] == "gzip" and "accept-encoding" in r.headers["vary"].lower(), path
    assert "content-encoding" not in c.get("/api/small", headers={"accept-encoding": "gzip"}).headers          # too small to be worth it
    assert "content-encoding" not in c.get("/api/data", headers={"accept-encoding": "identity"}).headers      # a client that does not ask gets plain bytes
    assert "content-encoding" not in c.get("/api/stream", headers={"accept-encoding": "gzip"}).headers        # a stream is never buffered for compression
