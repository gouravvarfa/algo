"""Why a session ended: the 401 tells the page (the 'sign the other device out' takeover is gone: any number of devices may be signed in)."""
from tests.security.helpers import api, login, make_user


# ---- why a session ended ----
def test_the_401_says_why_the_session_ended(db, new_client):
    acct = make_user(db)
    c = new_client()
    login(c, acct)
    token = c.cookies.get("algo_sid")
    api(c, "POST", "/api/auth/logout")
    gone = new_client().get("/api/auth/me", headers={"Cookie": f"algo_sid={token}"})
    assert gone.status_code == 401 and gone.json()["reason"] == "logout"
    unknown = new_client().get("/api/auth/me", headers={"Cookie": "algo_sid=never-existed-" + "x" * 30})
    assert unknown.status_code == 401 and unknown.json()["reason"] is None
