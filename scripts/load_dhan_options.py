"""Download expired index-option minute candles from Dhan (POST /v2/charts/rollingoption) into data/intraday/dhan_options_1m.db.

Research data only: nothing here places an order. Credentials come from .env (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN), never from the command line.

What Dhan really does (measured, not what its page says): one call returns at most TWO calendar days (toDate - fromDate <= 1; the page says 30),
both dates are inclusive, the series is one strike (ATM+k, k = -10..10) of one side (CE or PE) of one expiry (expiryCode 1 = nearest, 2 = the next),
and every minute carries the real strike, so a fixed strike can be rebuilt. Prices match our own stored data (2 Nov 2026 09:25 ATM CE: Dhan 102.45, ours 102.5).

Newest days first and all strikes of a window together, so an interrupted run still leaves complete recent days. Re-running resumes: finished
windows are skipped. Rate: RPS requests per second in total (Dhan's data limit is 5/s).

  python scripts/load_dhan_options.py --symbol NIFTY --flag WEEK --code 1 --start 2021-11-06 --end 2026-11-05
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "intraday" / "dhan_options_1m.db"
URL = "https://api.dhan.co/v2/charts/rollingoption"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SECURITY_ID = {"NIFTY": 13, "BANKNIFTY": 25}
FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
SCHEMA = """
create table if not exists candles(symbol text, flag text, code int, opt text, rel int, ts text, strike real, open real, high real, low real,
  close real, volume integer, iv real, oi real, spot real, primary key(symbol, flag, code, opt, rel, ts));
create table if not exists windows(symbol text, flag text, code int, opt text, rel int, d0 text, d1 text, rows int, primary key(symbol, flag, code, opt, rel, d0));
create table if not exists failures(symbol text, flag text, code int, opt text, rel int, d0 text, d1 text, status int, note text, at text);
"""


def env() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class Limiter:
    """One shared clock for every thread. When Dhan answers 429 (too many requests) ALL threads stop for `rest` seconds and the pace drops by a quarter
    (never below MIN_RPS), so a throttle is waited out once instead of every thread hammering it. The pace is never raised again during a run."""
    MIN_RPS = 1.0

    def __init__(self, rps: float, rest: float = 90.0):
        self.gap, self.rest, self.lock, self.next, self.hits = 1.0 / rps, rest, threading.Lock(), 0.0, 0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            time.sleep(at - now)

    def throttled(self):
        with self.lock:
            now = time.monotonic()
            if self.next < now + self.rest - 5:                  # a throttle already being waited out is not counted again by the other threads
                self.hits += 1
                self.gap = min(self.gap * 1.25, 1.0 / self.MIN_RPS)
                self.next = now + self.rest
                print(f"{time.strftime('%H:%M:%S')} Dhan said too many requests: all threads rest {self.rest:.0f}s, pace now {1.0 / self.gap:.2f} calls/s (hit {self.hits})", flush=True)


class AuthError(Exception):
    pass


def windows(start: dt.date, end: dt.date):
    """Pairs of consecutive weekdays (Mon-Tue, Wed-Thu) and single days (Fri): the most days one call can carry. Newest first."""
    out, d = [], start
    while d <= end:
        if d.weekday() >= 5:
            d += dt.timedelta(days=1)
            continue
        nxt = d + dt.timedelta(days=1)
        if d.weekday() in (0, 2) and nxt <= end:
            out.append((d, nxt))
            d += dt.timedelta(days=2)
        else:
            out.append((d, d))
            d += dt.timedelta(days=1)
    return out[::-1]


def fetch(h: dict, lim: Limiter, body: dict):
    """(status, parsed JSON or text). Retries network errors and rate limits; raises AuthError when the token is refused."""
    for attempt in range(8):
        lim.wait()
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")[:300]
            if e.code in (401, 403) or "DH-901" in text or "DH-902" in text:
                raise AuthError(text)
            if e.code == 429 or "DH-904" in text:
                lim.throttled()
                continue
            if e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, text
        except Exception:
            time.sleep(3 * (attempt + 1))
    return 0, "gave up after retries"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--flag", default="WEEK", choices=["WEEK", "MONTH"])
    ap.add_argument("--code", type=int, default=1)
    ap.add_argument("--start", default="2021-11-06")
    ap.add_argument("--end", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    ap.add_argument("--strikes", type=int, default=10, help="ATM-n .. ATM+n (Dhan allows 10 near expiry on an index, 3 otherwise)")
    ap.add_argument("--rps", type=float, default=4.0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--max-windows", type=int, default=0)
    a = ap.parse_args()
    e = env()
    h = {"access-token": e["DHAN_ACCESS_TOKEN"], "client-id": e["DHAN_CLIENT_ID"], "Content-Type": "application/json", "Accept": "application/json"}
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, check_same_thread=False, timeout=60)
    db.executescript(SCHEMA)
    lock, lim = threading.Lock(), Limiter(a.rps)
    done = {(r[0], r[1], r[2], r[3]) for r in db.execute("select opt, rel, d0, d1 from windows where symbol=? and flag=? and code=?", (a.symbol, a.flag, a.code))}
    wins = windows(dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end))
    if a.max_windows:
        wins = wins[:a.max_windows]
    series = [(o, k) for k in range(-a.strikes, a.strikes + 1) for o in ("CALL", "PUT")]
    jobs = [(w, o, k) for w in wins for o, k in series if (("CE" if o == "CALL" else "PE"), k, w[0].isoformat(), w[1].isoformat()) not in done]
    print(f"{len(wins)} windows x {len(series)} series; {len(jobs)} calls to do", flush=True)
    stats = {"done": 0, "rows": 0, "fail": 0}
    stop = threading.Event()

    def work(job):
        if stop.is_set():
            return
        (d0, d1), o, k = job
        side = "CE" if o == "CALL" else "PE"
        body = {"exchangeSegment": "NSE_FNO", "interval": 1, "securityId": SECURITY_ID[a.symbol], "instrument": "OPTIDX", "expiryFlag": a.flag,
                "expiryCode": a.code, "strike": "ATM" if k == 0 else f"ATM{k:+d}", "drvOptionType": o, "requiredData": FIELDS,
                "fromDate": d0.isoformat(), "toDate": d1.isoformat()}
        try:
            st, d = fetch(h, lim, body)
        except AuthError as exc:
            print("TOKEN REFUSED, stopping:", exc, flush=True)
            stop.set()
            return
        with lock:
            if st != 200 or not isinstance(d, dict):
                stats["fail"] += 1
                db.execute("insert into failures values(?,?,?,?,?,?,?,?,?,?)", (a.symbol, a.flag, a.code, side, k, d0.isoformat(), d1.isoformat(), st, str(d)[:200], dt.datetime.now().isoformat(timespec="seconds")))
                db.commit()
                return
            s = (d.get("data") or {}).get("ce" if o == "CALL" else "pe") or {}
            ts = s.get("timestamp") or []
            rows = []
            for i, t in enumerate(ts):
                stamp = dt.datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M")
                g = lambda f: (s.get(f) or [None] * len(ts))[i] if s.get(f) else None
                rows.append((a.symbol, a.flag, a.code, side, k, stamp, g("strike"), g("open"), g("high"), g("low"), g("close"), g("volume"), g("iv"), g("oi"), g("spot")))
            db.executemany("insert or replace into candles values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.execute("insert or replace into windows values(?,?,?,?,?,?,?,?)", (a.symbol, a.flag, a.code, side, k, d0.isoformat(), d1.isoformat(), len(rows)))
            db.commit()
            stats["done"] += 1
            stats["rows"] += len(rows)
            if stats["done"] % 200 == 0:
                print(f"{time.strftime('%H:%M:%S')} {stats['done']}/{len(jobs)} calls, {stats['rows']} rows, {stats['fail']} failed, now at {d0}", flush=True)

    with ThreadPoolExecutor(a.threads) as ex:
        list(ex.map(work, jobs))
    print("finished:", stats, "(re-run the same command to retry failures and resume)", flush=True)


if __name__ == "__main__":
    main()
