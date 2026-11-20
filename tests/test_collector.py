import datetime as dt

from app.options.collector import due


def test_due_only_after_close_on_weekdays_once_per_day():
    wed = dt.datetime(2026, 10, 21, 15, 50)
    assert due(wed, None) and due(wed, "2026-10-20")
    assert not due(wed, "2026-10-21")                                  # already done today
    assert not due(wed.replace(hour=15, minute=30), None)              # market still open
    assert not due(dt.datetime(2026, 10, 24, 16, 0), None)             # Saturday


def test_repair_fetches_again_only_the_contracts_whose_last_download_failed(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.options import intraday
    from app.providers.angelone_client import AngelOneApiError
    db = tmp_path / "i.db"
    c = intraday.connect(db)
    today = dt.datetime.now(intraday.IST).date()
    exp = (today + dt.timedelta(days=4)).isoformat()
    log_row = lambda strike, status, t: c.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("NIFTY", exp, strike, "CE", "t", "a", "b", 0, 0, status, "", t))
    log_row(22800, "error", "2026-10-23T16:00:00"); log_row(22850, "ok", "2026-10-23T16:00:00")
    log_row(22900, "error", "2026-10-23T15:00:00"); log_row(22900, "ok", "2026-10-23T16:30:00")        # failed once, fine since: not repaired again
    log_row(22950, "error", "2026-10-23T16:00:00")                                                       # keeps failing
    c.commit()
    mk = lambda strike: SimpleNamespace(expiry=dt.date.fromisoformat(exp), strike=strike, option_type="CE", token=f"tok{strike}", symbol=f"NIFTY{strike}CE")
    info = SimpleNamespace(name="NIFTY", contracts=[mk(22800), mk(22850), mk(22900), mk(22950)])
    asked = []

    async def fake_fetch(session, seg, token, start, end):
        asked.append(token)
        if token == "tok22950":
            raise AngelOneApiError("HTTP 403", "http")
        return [[dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%dT09:16:00+0530"), 100.0, 101.0, 99.0, 100.5, 10]]
    monkeypatch.setattr(intraday, "_fetch", fake_fetch)
    res = asyncio.run(intraday.repair_errors(None, info, 5, db=db, pause=0))
    assert res["tried"] == 2 and res["fixed"] == 1 and len(res["still_failing"]) == 1
    assert "tok22850" not in asked and "tok22900" not in asked and asked.count("tok22800") == 1 and asked.count("tok22950") == 3     # 3 tries, then reported


def test_a_symbol_refused_once_is_tried_again_and_saved(tmp_path, monkeypatch):
    """BANKNIFTY on 13 Nov: one HTTP 403 right after the close lost the whole day's download."""
    import asyncio
    from types import SimpleNamespace
    from app.options import collector
    from app.providers.angelone_client import AngelOneApiError
    monkeypatch.setattr(collector, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(collector, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(collector.master, "load_underlying", lambda sym: SimpleNamespace(name=sym))
    calls = {"BANKNIFTY": 0, "NIFTY": 0}

    async def ingest(session, info, *a, **k):
        calls[info.name] += 1
        if info.name == "BANKNIFTY" and calls["BANKNIFTY"] == 1:
            raise AngelOneApiError("Angel One returned a non-JSON response (HTTP 403).", "http")
        return {"contracts": 2, "contracts_with_data": 2, "rows": 10, "rejected": 0, "errors": []}

    async def repair(*a, **k):
        return {"still_failing": [], "fixed": 0}
    monkeypatch.setattr(collector.intraday, "ingest", ingest)
    monkeypatch.setattr(collector.intraday, "repair_errors", repair)

    async def get_session():
        return None
    st = asyncio.run(collector.run_once(SimpleNamespace(get_session=get_session)))
    assert st["BANKNIFTY"]["ok"] and st["NIFTY"]["ok"] and calls == {"BANKNIFTY": 2, "NIFTY": 1}


def test_a_symbol_that_keeps_failing_is_reported_after_three_tries(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.options import collector
    monkeypatch.setattr(collector, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(collector, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(collector.master, "load_underlying", lambda sym: SimpleNamespace(name=sym))
    n = []

    async def ingest(*a, **k):
        n.append(1)
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(collector.intraday, "ingest", ingest)

    async def get_session():
        return None
    st = asyncio.run(collector.run_once(SimpleNamespace(get_session=get_session), symbols=("BANKNIFTY",)))
    assert len(n) == 3 and st["BANKNIFTY"]["ok"] is False and "403" in st["BANKNIFTY"]["error"]


def test_an_mcx_symbol_refused_all_morning_tries_leaves_the_day_open_and_only_it_is_fetched_again(tmp_path, monkeypatch):
    """20 Nov 2026 05:02-05:08: Angel One refused GOLDM three times, SILVERM was saved, and the day was marked done: no further try that
    morning although the window lasts until 07:30. Now the day stays open, the next round fetches GOLDM only, and its success closes the day."""
    import asyncio
    from types import SimpleNamespace
    from app.options import collector
    monkeypatch.setattr(collector, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(collector, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(collector.master, "load_underlying", lambda sym: SimpleNamespace(name=sym))
    calls = []
    refuse = {"GOLDM": True}

    async def ingest(session, info, *a, **k):
        calls.append(info.name)
        if refuse.get(info.name):
            raise RuntimeError("circuit 'angelone.candles' is open; retry in 30s")
        return {"contracts": 2, "contracts_with_data": 2, "rows": 10, "rejected": 0, "errors": []}

    async def repair(*a, **k):
        return {"still_failing": [], "fixed": 0}
    monkeypatch.setattr(collector.intraday, "ingest", ingest)
    monkeypatch.setattr(collector.intraday, "repair_errors", repair)

    async def get_session():
        return None
    prov = SimpleNamespace(get_session=get_session)
    run = lambda: asyncio.run(collector.run_once(prov, collector.MCX_SYMBOLS, mark="last_mcx_run_date", retry_failed=True))
    st = run()
    assert calls == ["GOLDM"] * 3 + ["SILVERM"]
    assert st["GOLDM"]["ok"] is False and st["SILVERM"]["ok"] and "last_mcx_run_date" not in st          # the day stays open
    calls.clear(); refuse["GOLDM"] = False
    st = run()                                                                                          # the next round of the window
    assert calls == ["GOLDM"]                                                                           # SILVERM is not fetched again
    assert st["GOLDM"]["ok"] and st["last_mcx_run_date"] == dt.datetime.now(collector.IST).date().isoformat()


def test_the_after_close_index_run_still_closes_its_day_even_when_a_symbol_failed(tmp_path, monkeypatch):
    """Only the MCX morning window retries (it ends by itself at 07:30); the after-close index run keeps its old behaviour (no retry all night)."""
    import asyncio
    from types import SimpleNamespace
    from app.options import collector
    monkeypatch.setattr(collector, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(collector, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(collector.master, "load_underlying", lambda sym: SimpleNamespace(name=sym))

    async def ingest(*a, **k):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(collector.intraday, "ingest", ingest)

    async def get_session():
        return None
    st = asyncio.run(collector.run_once(SimpleNamespace(get_session=get_session), symbols=("BANKNIFTY",)))
    assert st["BANKNIFTY"]["ok"] is False and st["last_run_date"] == dt.datetime.now(collector.IST).date().isoformat()
