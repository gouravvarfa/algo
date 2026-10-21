r"""Daily NIFTY / BANKNIFTY index closes from Angel One (2001 onward) into fo_eod.db table index_close (source='angelone').
Used ONLY for legacy Bhavcopy days that have no NSE index file (before mid-2013). Before using them, this script
cross-checks Angel One against NSE's official ind_close_all files on sample days and prints the agreement.

    .venv\Scripts\python scripts\load_index_closes.py
"""
import asyncio, datetime as dt, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402
from app.options import bhavcopy, bhavcopy_legacy as bl  # noqa: E402
from app.options.master import IST  # noqa: E402
from app.providers.angelone_client import get_candle_data  # noqa: E402
from app.providers.angelone_provider import AngelOneProvider  # noqa: E402

TOKENS = {"NIFTY": "99926000", "BANKNIFTY": "99926009"}


async def main():
    s = await AngelOneProvider().get_session()
    got = {}
    for sym, tok in TOKENS.items():
        a = dt.date(2001, 1, 1)
        while a <= dt.date.today():
            b = min(a + dt.timedelta(days=1500), dt.date.today())
            rows = await get_candle_data(s, "NSE", tok, "ONE_DAY", f"{a} 09:15", f"{b} 15:30")
            for r in rows:
                d = dt.datetime.fromisoformat(r[0]).astimezone(IST).date().isoformat()
                if float(r[4]) > 0:
                    got[(d, sym)] = float(r[4])
            await asyncio.sleep(0.5)
            a = b + dt.timedelta(days=1)
        print(sym, sum(1 for k in got if k[1] == sym), "days, first", min(k[0] for k in got if k[1] == sym), flush=True)
    c = sqlite3.connect(bhavcopy.DB, timeout=3600)
    c.execute(bl.INDEX_CLOSE_SCHEMA)
    with c:
        c.executemany("INSERT OR REPLACE INTO index_close VALUES (?,?,?, 'angelone')", [(d, sym, v) for (d, sym), v in got.items()])
    # cross-check with NSE's own files on sample days (first trading day of each quarter, 2013-07 .. 2024-06)
    n = ok = 0
    worst = []
    with httpx.Client(headers=bhavcopy.HEADERS, timeout=60, follow_redirects=True) as h:
        for y in range(2013, 2025):
            for m in (1, 4, 7, 10):
                for day in range(1, 8):
                    d = dt.date(y, m, day)
                    if d < dt.date(2013, 7, 1) or d > dt.date(2024, 7, 5) or (d.isoformat(), "NIFTY") not in got:
                        continue
                    p = bl.RAW / f"ix{d:%Y%m%d}.csv"
                    bl.RAW.mkdir(parents=True, exist_ok=True)
                    try:
                        t = bl._get(h, bl.IX_URL.format(d=d), p, False)
                    except RuntimeError:
                        t = None
                    if not t:
                        continue
                    for sym, v in bl.index_closes(t.decode("utf-8", "replace")).items():
                        a = got.get((d.isoformat(), sym))
                        if a is None:
                            continue
                        n += 1
                        ok += abs(a - v) <= 0.05 + 1e-9
                        worst.append((round(abs(a - v), 2), d.isoformat(), sym, a, v))
                    break
    worst.sort(reverse=True)
    print(f"cross-check Angel One vs NSE index file: {ok}/{n} sample closes identical (within 5 paise)")
    for w in worst[:5]:
        print("  largest difference:", w)

asyncio.run(main())
