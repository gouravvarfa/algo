r"""India VIX daily close (Angel One historical candles, token 99926017) into fo_eod.db table vix_close.
Used by the backtester's optional VIX entry filter. Re-runnable (upsert by date).

    .venv\Scripts\python scripts\load_vix.py
"""
import asyncio, datetime as dt, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.options import bhavcopy  # noqa: E402
from app.options.master import IST  # noqa: E402
from app.providers.angelone_client import get_candle_data  # noqa: E402
from app.providers.angelone_provider import AngelOneProvider  # noqa: E402

SCHEMA = "CREATE TABLE IF NOT EXISTS vix_close (trade_date TEXT PRIMARY KEY, close REAL NOT NULL, source TEXT NOT NULL)"


async def main():
    s = await AngelOneProvider().get_session()
    got, a = {}, dt.date(2008, 1, 1)
    while a <= dt.date.today():
        b = min(a + dt.timedelta(days=1500), dt.date.today())
        for r in await get_candle_data(s, "NSE", "99926017", "ONE_DAY", f"{a} 09:15", f"{b} 15:30"):
            if float(r[4]) > 0:
                got[dt.datetime.fromisoformat(r[0]).astimezone(IST).date().isoformat()] = float(r[4])
        await asyncio.sleep(0.5)
        a = b + dt.timedelta(days=1)
    c = sqlite3.connect(bhavcopy.DB, timeout=600)
    c.execute(SCHEMA)
    with c:
        c.executemany("INSERT OR REPLACE INTO vix_close VALUES (?,?, 'angelone')", sorted(got.items()))
    print(f"India VIX: {len(got)} days, {min(got)} -> {max(got)}; last 3: {sorted(got.items())[-3:]}")

asyncio.run(main())
