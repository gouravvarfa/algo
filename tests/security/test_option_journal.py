from tests.security.helpers import api, login, make_user


def _leg(i, **kw):
    base = dict(id=i, side="SELL", opt="CE", strike=24500, expiry="2026-10-27", lots=2, lot_size=75, entry=120.0, entry_date="2026-10-21", entry_time="09:20")
    base.update(kw)
    return base


def test_every_leg_is_kept_even_after_the_simulator_is_cleared(db, new_client):
    u = make_user(db, "optu")
    c = new_client()
    assert login(c, u).status_code == 200
    put = lambda legs, sym="NIFTY": api(c, "PUT", f"/api/me/sim-positions?symbol={sym}", json={"positions": legs})
    assert put([_leg(1), _leg(2, opt="PE", strike=24000)]).status_code == 200
    j = c.get("/api/me/option-journal").json()
    assert j["stats"]["trades"] == 2 and j["stats"]["open"] == 2
    # leg 1 is exited at 90: (120-90) * 2 * 75 = 4500 profit, as the simulator computed it
    put([_leg(1, closed=True, exit=90.0, exit_date="2026-10-22", exit_time="10:05", realized=4500.0, exit_reason="manual"), _leg(2, opt="PE", strike=24000)])
    s = c.get("/api/me/option-journal").json()["stats"]
    assert s["closed"] == 1 and s["net"] == 4500.0 and s["win_rate"] == 100.0 and s["curve"][-1]["cum"] == 4500.0
    put([])                                                                                  # clear the screen
    j = c.get("/api/me/option-journal").json()
    assert {t["status"] for t in j["trades"]} == {"CLOSED", "REMOVED"} and j["stats"]["net"] == 4500.0
    assert c.get("/api/me/sim-positions?symbol=NIFTY").json()["positions"] == []


def test_notes_tags_rating_csv_and_privacy(db, new_client):
    a, b = make_user(db, "optA"), make_user(db, "optB")
    ca, cb = new_client(), new_client()
    assert login(ca, a).status_code == 200 and login(cb, b).status_code == 200
    api(ca, "PUT", "/api/me/sim-positions?symbol=NIFTY", json={"positions": [_leg(1)]})
    tid = ca.get("/api/me/option-journal").json()["trades"][0]["id"]
    assert api(ca, "PUT", f"/api/me/option-journal/{tid}", json={"note": "sold into the spike", "tags": ["Breakout", "gap"], "rating": 4}).status_code == 200
    t = ca.get("/api/me/option-journal").json()["trades"][0]
    assert t["note"] == "sold into the spike" and t["tags"] == ["breakout", "gap"] and t["rating"] == 4
    assert api(cb, "PUT", f"/api/me/option-journal/{tid}", json={"note": "hijack"}).status_code == 404           # someone else's trade
    assert cb.get("/api/me/option-journal").json()["stats"]["trades"] == 0
    assert "sold into the spike" in ca.get("/api/me/option-journal.csv").text


def test_admin_sees_the_journal_read_only_and_only_for_people_below(db, new_client):
    admin, user = make_user(db, "adm2", "admin"), make_user(db, "optC")
    ca, cu = new_client(), new_client()
    assert login(ca, admin).status_code == 200 and login(cu, user).status_code == 200
    api(cu, "PUT", "/api/me/sim-positions?symbol=NIFTY", json={"positions": [_leg(7)]})
    assert ca.get(f"/api/admin/users/{user.id}/option-journal").json()["stats"]["trades"] == 1
    assert cu.get(f"/api/admin/users/{admin.id}/option-journal").status_code == 403
