r"""Fetch real 1-minute NIFTY index + option candles from Angel One into data/intraday/nifty_1m.db, then cross-check
the last minute of each contract-day against the NSE Bhavcopy close.

    .venv\Scripts\python scripts\load_intraday.py --days 30 --horizon 45
Only contracts still listed in Angel One's master can be fetched (expired ones are not available from Angel One).
"""
import argparse, asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.options import intraday, master  # noqa: E402
from app.providers.angelone_provider import AngelOneProvider  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--symbol", default="NIFTY")
ap.add_argument("--days", type=int, default=30)
ap.add_argument("--horizon", type=int, default=45, help="fetch contracts expiring within this many days")
ap.add_argument("--pad", type=float, default=300.0, help="strikes within index low/high +/- pad")
a = ap.parse_args()


async def main():
    info = master.load_underlying(a.symbol)
    session = await AngelOneProvider().get_session()
    s = await intraday.ingest(session, info, a.days, a.horizon, a.pad, log=lambda m: print(m, flush=True))
    print(f"\nindex minutes {s['index_rows']} | contracts {s['contracts']} ({s['contracts_with_data']} with data) | option minutes {s['rows']} "
          f"| rejected {s['rejected']} | errors {len(s['errors'])}")
    for e in s["errors"][:10]:
        print("  error:", e)
    c = intraday.connect()
    print("days with intraday data:", [d["date"] for d in intraday.days(c, a.symbol)])
    x = intraday.crosscheck(c, a.symbol)
    print(f"cross-check vs Bhavcopy close: {x['within_1pct']}/{x['compared']} contract-days within 1% (or 5 paise)")
    for w in x["worst"]:
        print("  largest gap:", w)

asyncio.run(main())
