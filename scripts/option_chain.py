r"""
Standalone live option chain (real Angel One data). Prints the chain for one expiry and exits.

    .venv\Scripts\python scripts\option_chain.py                       # BANKNIFTY, nearest expiry, ATM +/- 10
    .venv\Scripts\python scripts\option_chain.py --symbol NIFTY --strikes 8 --expiry next

Uses the app's existing Angel One login (credentials store); nothing is ordered, nothing is stored except the daily
instruments-master cache in data/instruments/. Exit code: 0 = clean, 3 = HARD data-quality violation, 1 = error.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.options import chain as oc  # noqa: E402
from app.options import master  # noqa: E402
from app.providers.angelone_provider import AngelOneProvider  # noqa: E402


def f(v, nd=2, w=10):
    return f"{v:>{w},.{nd}f}" if v is not None else f"{'-':>{w}}"


def i(v, w=11):
    return f"{v:>{w},d}" if v is not None else f"{'-':>{w}}"


def render(ch: oc.Chain, info: master.UnderlyingInfo, args) -> str:
    atm = oc.atm_strike(ch)
    sample = next(iter(ch.rows.values()))["CE"].contract
    pe_oi = sum((s["PE"].oi or 0) for s in ch.rows.values() if "PE" in s)
    ce_oi = sum((s["CE"].oi or 0) for s in ch.rows.values() if "CE" in s)
    dte = (ch.expiry - ch.fetched_at.date()).days
    lines = [
        f"{ch.underlying} option chain  |  expiry {ch.expiry:%d-%b-%Y} ({dte} day(s), T={ch.t_years * 365:.2f}d)  |  lot size {sample.lot_size}  |  tick size Rs {sample.tick_size:g}",
        f"spot {ch.spot:,.2f}" + (f" (feed {ch.spot_feed_time:%H:%M:%S})" if ch.spot_feed_time else "") + f"  |  India VIX {ch.vix if ch.vix is not None else 'n/a'}  |  ATM strike {atm:g}  |  "
        f"PCR(OI) {pe_oi / ce_oi:.2f}  |  fetched {ch.fetched_at:%Y-%m-%d %H:%M:%S} IST  |  market {'OPEN' if ch.market_open else 'CLOSED'}",
        f"instruments master: {info.source}  |  strikes shown: ATM +/- {args.strikes} ({len(ch.rows)} listed strikes)  |  Greeks: broker unless marked *calc",
        "",
        f"{'':>3}{'--------------- CALLS (CE) ---------------':^{11 * 2 + 10 * 5}}{'':^14}{'--------------- PUTS (PE) ---------------':^{11 * 2 + 10 * 5}}",
        f"{'':>3}{'OI':>11}{'Volume':>11}{'IV%':>10}{'Delta':>10}{'Bid':>10}{'Ask':>10}{'LTP':>10}  {'STRIKE':^10}  {'LTP':>10}{'Bid':>10}{'Ask':>10}{'Delta':>10}{'IV%':>10}{'Volume':>11}{'OI':>11}",
    ]

    def side(leg, call):
        if leg is None:
            return " " * (11 * 2 + 10 * 5)
        g = "*" if leg.greeks_source == "calc" else " "
        oi, vol, iv, de = i(leg.oi), i(leg.volume), f(leg.iv, 2, 9) + g, f(leg.delta, 3)
        bid, ask, ltp = f(leg.bid), f(leg.ask), f(leg.ltp)
        # calls read  OI Volume IV Delta Bid Ask LTP | strike | puts mirror it  LTP Bid Ask Delta IV Volume OI
        return oi + vol + iv + de + bid + ask + ltp if call else ltp + bid + ask + de + iv + vol + oi

    for k in sorted(ch.rows):
        r = ch.rows[k]
        mark = ">>" if k == atm else "  "
        lines.append(f"{mark:>3}{side(r.get('CE'), True)}  {k:^10,.0f}  {side(r.get('PE'), False)}")
    lines += ["", "change in OI: n/a (the broker quote returns only the current open interest)"]
    flagged = [(k, t, l) for k, s in sorted(ch.rows.items()) for t, l in s.items() if l.flags]
    lines.append("")
    lines.append("DATA QUALITY")
    lines.append(f"  hard violations : {len(ch.violations)}" + ("" if ch.violations else "  (none)"))
    lines += [f"    - {v}" for v in ch.violations]
    lines.append(f"  stale ticks     : {len(ch.stale)}" + (f"  (older than {args.max_age:g}s while the market is open)" if ch.stale else "  (none" + ("" if ch.market_open else "; not judged: market closed") + ")"))
    lines += [f"    - {v}" for v in ch.stale[:10]]
    illiq = [x for x in flagged if any(fl.startswith("ILLIQ") for fl in x[2].flags)]
    lines.append(f"  illiquid legs   : {len(illiq)} of {sum(len(s) for s in ch.rows.values())} (OI<{args.min_oi}, volume<{args.min_volume}, spread>{args.max_spread_pct:g}% or one-sided) - excluded from strike selection, never forward-filled")
    for k, t, l in illiq[:8]:
        lines.append(f"    - {l.contract.symbol}: {'; '.join(fl for fl in l.flags if fl.startswith('ILLIQ'))}")
    if len(illiq) > 8:
        lines.append(f"    ... and {len(illiq) - 8} more")
    lines += [f"  note            : {w}" for w in ch.warnings]
    fmt = lambda v: 'none' if v is None else f'{v:g}'
    ce = oc.otm_by_points(ch, "CE", 200); pe = oc.otm_by_points(ch, "PE", 200)
    lines += ["", "STRIKE HELPERS (liquid strikes only): "
              f"ATM {atm:g} | OTM +200 pts CE {fmt(ce)} / PE {fmt(pe)} | "
              f"0.30-delta CE {fmt(oc.by_delta(ch, 'CE', 0.30))} / PE {fmt(oc.by_delta(ch, 'PE', 0.30))} | premium~Rs100 CE {fmt(oc.nearest_premium(ch, 'CE', 100))} / PE {fmt(oc.nearest_premium(ch, 'PE', 100))}"]
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BANKNIFTY", choices=["BANKNIFTY", "NIFTY"])
    ap.add_argument("--expiry", default="nearest", help="nearest | next | DDMMMYYYY (e.g. 27OCT2026)")
    ap.add_argument("--strikes", type=int, default=10, help="strikes on each side of ATM")
    ap.add_argument("--max-age", type=float, default=5.0, help="seconds; staleness is judged only while the market is open")
    ap.add_argument("--min-oi", type=int, default=1000)
    ap.add_argument("--min-volume", type=int, default=100)
    ap.add_argument("--max-spread-pct", type=float, default=10.0)
    ap.add_argument("--rate", type=float, default=0.065, help="risk-free rate used ONLY if Greeks must be calculated")
    ap.add_argument("--div", type=float, default=0.0, help="dividend yield used ONLY if Greeks must be calculated")
    ap.add_argument("--refresh-master", action="store_true", help="re-download today's instruments master")
    args = ap.parse_args()
    try:
        info = master.load_underlying(args.symbol, force_refresh=args.refresh_master)
        expiry = master.pick_expiry(info, args.expiry)
        session = await AngelOneProvider().get_session()
        ch = await oc.fetch_chain(session, info, expiry, args.strikes, args.max_age, args.min_oi, args.min_volume,
                                  args.max_spread_pct, args.rate, args.div)
    except Exception as exc:  # noqa: BLE001 - a CLI must say why it failed
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(render(ch, info, args))
    return 3 if ch.violations else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
