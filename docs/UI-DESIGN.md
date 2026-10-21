# UI design system and references

## References reviewed (accessible ones only)
| Reference | Access | What was useful |
|---|---|---|
| TradingView, public chart page (screenshot taken) | opened | Chart takes ~70% of the width; a fixed ~340 px side panel holds the instrument details; a thin top toolbar; OHLC legend inside the chart; ~28-30 px table rows; green/red only for P&L; muted grid lines |
| Interactive Brokers, Trader Workstation page (screenshot taken, product images) | page opened behind a region dialog | Dense multi-panel workspace, tabular numerals right-aligned, almost no decoration, dark theme for long sessions |
| Schwab thinkorswim | blocked: server returned an authorisation error page | not reviewed |
| StockMock / AlgoTest (earlier session) | partly blocked, docs readable | payoff + positions + one timeline layout |
No branding, assets or exact layouts were copied.

## Principles applied
1. The chart is the hero but never taller than the viewport: `--chart-h: clamp(340px, 100vh - 330px, 640px)`.
2. Summary beside the chart (320 px) on wide screens, stacked below 1280 px.
3. Numbers: IBM Plex Mono / tabular numerals so values do not jump; labels in Inter 10.5-12.5 px.
4. One accent (blue), semantic green/red for P&L only, amber for PAPER/warnings, purple for calculated estimates.
5. Always-visible context: market open/closed and "PAPER no orders" chips in the top bar.

## Tokens (web/theme.css, "Design system v2")
Spacing 4/8/12/16/24 · control height 32 · table row 30 · radius 6/8/12 · nav 52 · fonts self-hosted (web/fonts) with system fallbacks · light/dark/system theme · `prefers-reduced-motion` respected.

## Navigation
Dashboard · Top Bottom · Options ▾ (Simulator, Strategy Builder, Backtesting, History, Option Chain) · Signals ▾ (Index Replay, 15-Min Breakout) · Settings. Every old route still works (`/journal` -> Top Bottom).
