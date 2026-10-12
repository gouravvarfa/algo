"""Synthetic BankNifty-INDEX candles for the replay tests. Clearly synthetic:
used only to test logic, never presented as market data."""
import datetime as dt

START = dt.date(2026, 9, 1)


def day_ts(i):
    return dt.datetime.combine(START + dt.timedelta(days=i), dt.time())


def daily_rows(closes, opens=None, highs=None, lows=None):
    """1D candles; open defaults to the previous close, high/low to the
    body extremes. opens/highs/lows: {index: value} overrides."""
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = (opens or {}).get(i, prev)
        h = (highs or {}).get(i, max(o, c))
        lo = (lows or {}).get(i, min(o, c))
        rows.append((day_ts(i), float(o), float(h), float(lo), float(c), 0.0))
        prev = c
    return rows


def rows15(day_index, first, later=()):
    """A full 25-candle 15m session for one day. first=(o,h,l,c) is the
    09:15 candle; later = [(o,h,l,c), ...] are the 09:30, 09:45 ... candles;
    remaining candles repeat the last supplied close flat."""
    base = day_ts(day_index).replace(hour=9, minute=15)
    specs = [first, *later]
    last_close = specs[-1][3]
    out = []
    for k in range(25):
        o, h, lo, c = specs[k] if k < len(specs) else (last_close,) * 4
        out.append((base + dt.timedelta(minutes=15 * k), float(o), float(h), float(lo), float(c), 0.0))
    return out


def intraday_rows(day_index, interval_min=1, price=100.0):
    """A full flat session of `interval_min` candles for one day."""
    base = day_ts(day_index).replace(hour=9, minute=15)
    n = len(range(9 * 60 + 15, 15 * 60 + 30, interval_min))
    return [(base + dt.timedelta(minutes=interval_min * k), price, price, price, price, 0.0) for k in range(n)]


def csv_text(rows, interval="1D", instrument="BANKNIFTY"):
    lines = ["timestamp,instrument,interval,open,high,low,close,volume"]
    for ts, o, h, lo, c, v in rows:
        stamp = ts.date().isoformat() if interval == "1D" else ts.isoformat()
        lines.append(f"{stamp},{instrument},{interval},{o},{h},{lo},{c},{v}")
    return "\n".join(lines)
