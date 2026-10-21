r"""Load OLD-format NSE F&O Bhavcopy (2001 -> 5 Jul 2024) for NIFTY and BANKNIFTY index options, newest first.

    .venv\Scripts\python scripts\load_bhavcopy_legacy.py --from 2001-06-01 --to 2024-07-07
Re-runnable: days already loaded (UDiFF or legacy) are skipped; raw files are cached.
"""
import argparse, datetime as dt, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.options import bhavcopy_legacy as bl  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="start", default="2001-06-01")
ap.add_argument("--to", dest="end", default="2024-07-07")
ap.add_argument("--symbols", default="NIFTY,BANKNIFTY")
a = ap.parse_args()
s = bl.load_range(dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end), a.symbols.split(","), log=lambda m: print(m, flush=True))
print(f"\nloaded days {s['loaded_days']} | rows {s['rows']} | rejected {s['rejected']} | no-file weekdays {s['no_file_days']} | "
      f"days with a symbol not loaded {len(s['partial_days'])} | skipped existing {s['skipped_existing']} | errors {len(s['errors'])}")
for e in s["errors"][:20]:
    print("  error:", e)
