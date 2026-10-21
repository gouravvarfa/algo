# P6 design (not implemented): portfolio backtesting and intraday replay

Status: design only. Nothing in this document exists in code yet. Implementation needs a separate approval.

## A. Portfolio backtesting

### Why "sum the strategies" is wrong
Adding the net P&L of independently backtested strategies ignores four things:
- Positions that are open at the same time share one margin pool.
- Drawdowns that overlap in time compound each other.
- A portfolio stop or target fires on the combined mark-to-market, not on each strategy separately.
- A strategy can be blocked from entering when capital is already used.

### Inputs
- `capital` (rupees).
- `strategies`: 2-10 saved strategies (server store), each with a `weight` / `lot multiplier`.
- Portfolio rules:
  - `max_margin_pct` of capital in use at any close.
  - `portfolio_sl` and `portfolio_target` on the combined closing MTM.
  - `max_daily_loss` on the combined closing MTM change.
  - `on_capital_shortfall`: `skip_entry` | `scale_down`.

### Margin (the missing piece)
- Option selling margin is SPAN + exposure. NSE publishes daily SPAN risk-parameter files for F&O.
- Plan: ingest the daily SPAN files the same way as the Bhavcopy, into a new table `span_params(trade_date, symbol, expiry, ...)`.
- Margin is then computed per position per day: short options use SPAN + exposure; long options use the premium paid.
- Until SPAN files are loaded, the honest fallback is a clearly labelled **user-supplied margin per lot** (e.g. ₹1.2L per short lot). It must never be shown as exchange margin.

### Engine shape
1. Build a single **portfolio calendar** of trading days in the range.
2. Each strategy generates its cycles exactly as today (same `run_cycle` rules). The change: cycles are stepped **day by day in parallel** instead of one at a time, so each day can see every open position. This means refactoring `run_cycle` into a resumable state machine (`start_cycle`, `step_day(d)`, `finish`) with no rule changes. It is verified by asserting that a 1-strategy portfolio reproduces `engine.run` bit-for-bit.
3. At each close:
   1. Sum the MTM of all open legs.
   2. Compute the margin in use.
   3. Apply the portfolio rules in a fixed order: portfolio SL, then target, then max daily loss.
   4. Exits happen at that close, as with the overall SL today.
4. Entries that day are allowed only if `margin_in_use + new_margin <= max_margin_pct * capital`. Otherwise `skip_entry` (recorded with the reason) or `scale_down` lots.

### Outputs
- Combined equity and drawdown curve.
- Margin-utilisation curve.
- Strategy-wise contribution (P&L and drawdown share).
- Overlap matrix: the number of days strategies were open together.
- Return on capital, which becomes meaningful because the denominator is defined.
- Skipped entries with reasons.

### Tests
- A 1-strategy portfolio equals `engine.run`.
- Two disjoint-time strategies equal the sum.
- An overlapping pair triggers the portfolio SL earlier than either alone.
- A capital-shortfall skip is recorded.
- Margin fallback labels are shown.

## B. Genuine intraday (1-minute) replay

### Data source options (each needs evaluation and licensing review before purchase)
| Source | Expired option contracts | Notes |
|---|---|---|
| Angel One SmartAPI historical candles | No (only live contracts) | Usable to **record going forward** only |
| Recording our own feed (Angel One WebSocket) | Builds up from the day recording starts | Free; no history before the start date |
| NSE / NSE-authorised vendors (e.g. TrueData, Global Datafeeds, others) | Yes, typically | Paid; check licence terms for local storage |

The recommended path is two parallel tracks:
1. Start recording 1-minute OHLC + OI for NIFTY weekly and monthly options (ATM ± 20 strikes) from the WebSocket now, so history accumulates.
2. Evaluate one vendor for back-history.

### Storage plan
- One Parquet file per `symbol/expiry/trade_date`, partitioned by symbol and month, with columns: `ts (IST minute)`, `strike`, `opt`, `open`, `high`, `low`, `close`, `volume`, `oi`.
- Rough size: about 2-5 MB per expiry-day for ATM ± 20 strikes.
- A DuckDB view over the Parquet files serves queries.
- The EOD SQLite store stays untouched and remains the source for the EOD tools.
- Quality checks at ingestion:
  - no duplicate minutes;
  - OHLC consistency;
  - no minute outside 09:15-15:30;
  - gaps are recorded, never filled.
- Each day has a coverage report. A day below a coverage threshold is marked unusable for replay.
- Cross-check: the last 1-minute close of each contract must agree with the Bhavcopy close for that day within the tick size. Any mismatch is flagged.

### What it unlocks (only after the data exists)
- Simulator time-of-day controls (SOD, −30m, +1m, …).
- Intraday entry/exit times.
- Exact SL/target ordering, with no more AMBIGUOUS flags.
- Minute-level re-entry and lock-profit.

The EOD tools keep working unchanged and stay labelled EOD.
