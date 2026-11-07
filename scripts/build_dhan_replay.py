"""Convert the Dhan option download into the Simulator's replay database (data/intraday/nifty_1m_dhan.db). Research data only; nothing here touches a broker.

  python scripts/build_dhan_replay.py                 # converts every day (about 15-30 minutes), safe to re-run
  python scripts/build_dhan_replay.py --after 2026-10-01   # only days after this date (a later top-up)
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.options import dhan_replay, intraday  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "data" / "intraday" / "dhan_options_1m.db"))
    ap.add_argument("--dst", default=str(intraday.REPLAY_DB))
    ap.add_argument("--after", default=None)
    a = ap.parse_args()
    out = dhan_replay.build(Path(a.src), Path(a.dst), only_after=a.after)
    print("done:", out)


if __name__ == "__main__":
    main()
