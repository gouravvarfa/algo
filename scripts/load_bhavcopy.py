r"""Download + load NSE F&O Bhavcopy (index options) into data/bhavcopy/fo_eod.db and print a verification summary.

    .venv\Scripts\python scripts\load_bhavcopy.py --from 2026-09-17 --to 2026-10-16
"""
import argparse, datetime as dt, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.options import bhavcopy as bc  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="start", required=True)
ap.add_argument("--to", dest="end", required=True)
ap.add_argument("--symbols", default="BANKNIFTY,NIFTY")
a = ap.parse_args()
s = bc.load_range(dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end), a.symbols.split(","))
print(f"\nloaded days {s['loaded_days']} | rows {s['rows']} | rejected {s['rejected']} | no-file weekdays {s['no_file_days']} | errors {len(s['errors'])}")
c = sqlite3.connect(bc.DB)
for sym in a.symbols.split(","):
    r = c.execute("SELECT COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date), COUNT(*), COUNT(DISTINCT expiry), GROUP_CONCAT(DISTINCT lot_size) FROM options_eod WHERE symbol=? AND trade_date BETWEEN ? AND ?", (sym, a.start, a.end)).fetchone()
    print(f"{sym}: {r[0]} trading days {r[1]}..{r[2]} | {r[3]} contract-days | {r[4]} expiries | lot size(s) {r[5]}")
