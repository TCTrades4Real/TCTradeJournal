# TCTradeJournal — Project Guide

## Overview
Day-trading journal. Pulls real trades from TradeZero (live account) and OHLCV price
history from Alpaca, and renders a browser-based dashboard. Runs entirely locally — no
hosting involved. Historical trades from a retired Schwab integration remain in
`trade_data/*.csv` as frozen history — still parsed into the calendar on every run, just
no longer added to going forward.

Dashboard pages fetch calendar/OHLCV JSON via relative paths, which Chrome/Edge block
under `file://` (CORS). Serve locally instead:

```bash
python dashboard_server.py        # or: python launch_dashboard.py (also opens the browser)
# then open http://localhost:8000/index.html
```

`dashboard_server.py` is a thin wrapper around `http.server` that adds one write
endpoint (`POST /api/exclude-paper-trade`) so `trades.html`'s paper-trade delete
button works — plain `python -m http.server` still serves the dashboard fine, but clicking
delete on a paper trade will fail against it (no write endpoint).

---

## Architecture

```
import_tradezero.py  → fetch today's filled TradeZero live + paper orders
                       → merge with historical Schwab CSVs in trade_data/ (paper
                         exclude-listed first — see Paper Trades)
                       → calendar_data.py's build_and_write() → dashboard/calendar/calendar_data_YYYY.json
                         (Cash/Roth/TZLive/TZPaper all blended into every total/stat)
                       → write dashboard/account_balance.json (TZ LIVE equity + today's PnL only)
                       → auto-runs the rest of the pipeline (see Pipeline section)

calendar_data.py     → parse trade_data/ CSVs (+ executions handed in by import_tradezero.py)
                       → write dashboard/calendar/calendar_data_YYYY.json (per-year)
                       → write dashboard/calendar/calendar_index.json

fetch_ohlcv.py       → pre-fetch 1-min OHLCV from Alpaca (SIP feed, years of history)
                       → write dashboard/ohlcv/ohlcv_YYYY-MM-DD.json (per-day)

compute_mfe_mae.py   → reads per-year calendar JSONs + per-day OHLCV
                       → adds mfe/mae fields to each roundtrip in calendar JSONs

dashboard_server.py  → local static file server for dashboard/ + one write endpoint
                       (POST /api/exclude-paper-trade) for the paper-trade delete button —
                       triggers import_tradezero.rebuild_calendar() (no API calls)
launch_dashboard.py  → starts dashboard_server.py + opens a tabless Chrome window

dashboard/
  index.html         → monthly calendar view
  month.html         → monthly PnL grid
  day.html           → daily symbol table + intraday chart
  candlestick-chart.html → per-symbol candlestick chart + trade markers; not a page of its own —
                       loaded in an iframe popup by day.html and trades.html
  reports.html       → trading stats, equity curves, MFE/MAE analysis, Monte Carlo
  trades.html        → trade log
```

---

## Full Pipeline (import_tradezero.py)

Running `python import_tradezero.py` executes the entire pipeline in order:

1. Fetch today's filled TradeZero live orders, tag them `account="TZLive"`
2. Fetch today's filled TradeZero **paper** orders, tag them `account="TZPaper"` —
   non-fatal if it fails (see Paper Trades below for the exclude-list applied before
   they reach the calendar build)
3. Merge both with historical Schwab executions read from `trade_data/` CSVs, rebuild
   the calendar via `calendar_data.build_and_write()` → per-year calendar JSONs (paper
   blended into every total/stat alongside Cash/Roth/TZLive)
4. Write `dashboard/account_balance.json` from the TradeZero live account (equity +
   today's realized PnL, TZLive only) — non-fatal if it fails
5. `fetch_ohlcv.py` → fetch/cache missing 1-min OHLCV bars (Alpaca)
6. `compute_mfe_mae.py` → annotate roundtrips with MFE/MAE

**Known limitation:** TradeZero's `/orders` endpoint only returns *today's* orders — no
working historical-range endpoint exists. `import_tradezero.py` must run same-day. It's
safe to re-run later the same day: `calendar_data.py` always rebuilds the full calendar
JSON from scratch, so re-running never double-counts.

**Scheduled:** a Windows Task Scheduler task ("TCTradeJournal Import") runs
`run_import_tradezero.bat` hourly, Monday-Friday 7am-8pm, logging to
`logs/import_tradezero.log`.

---

## Data Flow

1. **import_tradezero.py** → TradeZero API → merged with `trade_data/YYYY-MM-DD-{Cash|Roth}-AccountStatement.csv` (historical Schwab, frozen)
2. **calendar_data.py** (`build_and_write`, called by import_tradezero.py) → `dashboard/calendar/calendar_data_YYYY.json`
3. **fetch_ohlcv.py** → Alpaca API → `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json`
4. **compute_mfe_mae.py** → reads calendar + OHLCV → writes `mfe`/`mae` into calendar JSONs
5. **Dashboard HTML files** → fetch calendar JSONs via relative paths (served locally over `http://`, see Dashboard Notes)

---

## Key Files

| File | Purpose |
|------|---------|
| `utilities/config.py` | TradeZero API keys (paper + live), Alpaca API keys, Tradervue creds, legacy Schwab fields (unused, kept harmlessly) |
| `utilities/tradezero_client.py` | Thin TradeZero REST client — `get_orders()` (today only), `get_account()` |
| `utilities/alpaca_client.py` | Thin wrapper around alpaca-py's `StockHistoricalDataClient` — `get_minute_bars()`, `get_daily_bars()` |
| `dashboard/calendar/calendar_index.json` | List of available years |
| `dashboard/calendar/calendar_data_YYYY.json` | Per-year roundtrips with mfe/mae, daily PnL, `account` field (`Cash`/`Roth` = historical Schwab, `TZLive`/`TZPaper` = TradeZero); each day/symbol record carries blended `pnl` plus `pnl_cash`/`pnl_roth`/`pnl_tz`/`pnl_paper` breakdowns |
| `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json` | Per-day 1-min OHLCV cache `{SYMBOL: [bars...]}` |
| `dashboard/account_balance.json` | `{balance, pnl_today}` — TradeZero **live** account equity + today's realized PnL only (never includes paper), read by `nav.js`'s sidebar figure and `monte_carlo.html`'s starting-balance seed |
| `dashboard/paper/paper_excluded.json` | List of `{date, symbol, entry_time}` keys for manually-deleted paper round-trips (written by `dashboard_server.py`'s delete endpoint) |
| `trade_data/tz_live_executions.json` / `tz_paper_executions.json` | Full raw execution history logs (today's `/orders` fetch gets merged in and persisted here every run — the only way around the TZ API's today-only limitation) |

---

## MFE / MAE

- **MFE** (Maximum Favorable Excursion): max interim profit during trade (position $)
- **MAE** (Maximum Adverse Excursion): max interim loss during trade (position $)
- Computed from 1-min OHLCV bars within `[entry_time, exit_time]` window
- Direction (long/short) inferred from sign relationship between pnl and price delta
- Stored as `mfe`/`mae` fields on each roundtrip in calendar JSONs
- Surfaced in `reports.html` → Trading Stats tab: Avg MFE, Avg MAE, Entry Efficiency, Trade Efficiency
- Scatter charts (MFE vs PnL, MAE vs PnL) in reports.html → Charts tab

```bash
python compute_mfe_mae.py             # update roundtrips missing mfe/mae
python compute_mfe_mae.py --refresh   # recompute all
python compute_mfe_mae.py --year 2026 # one year only
```

---

## Paper Trades

TradeZero paper-account fills (`utilities/config.py`'s `TZ_PAPER_*` creds, account
`TZP4CE26`) are pulled by `import_tradezero.py` alongside the live import and **blended
into the same calendar build as Cash/Roth/TZLive** — `account="TZPaper"` roundtrips flow
through `calendar_data.py` like any other account, so paper P&L is part of the headline
`pnl` figure (and the `pnl_paper` breakdown alongside `pnl_cash`/`pnl_roth`/`pnl_tz`) on
every page: index/month/day calendar cells, `reports.html` stats/equity-curves/MFE-MAE/
Monte Carlo, `trades.html`'s log, and `candlestick-chart.html`.

**The one exception:** `dashboard/account_balance.json` (today's realized PnL + account
equity) is computed from TZLive executions and the live TradeZero account only — paper
P&L can never be part of an actual brokerage equity figure, regardless of the blend.

- **Exclude-list:** the only paper-only filter, applied in
  `import_tradezero.build_all_executions()` before anything reaches
  `calendar_data.build_and_write()`. Specific round-trips can be permanently deleted (see
  below) — filtered by `filter_excluded_paper_trades()` against
  `dashboard/paper/paper_excluded.json`. There's no size-based filtering — every paper
  fill TradeZero reports is imported; fat-finger/test-size trades get dropped by hand via
  the same delete flow.
- **Visual distinction on the chart:** `candlestick-chart.html` still tells paper apart
  from real fills at a glance — same green-entry/blue-partial/orange-red-exit color
  scheme, but paper renders as **circles** instead of diamonds (decided per-marker from
  each exec's own `account` field in `renderExecMarkers()`, not a separate toggle —
  paper shows up automatically whenever "Real trades" is on).
- **Deleting a paper trade:** done from `trades.html` only (the candlestick chart's
  markers are read-only) — removes the *whole* round-trip (every fill). This POSTs to
  `dashboard_server.py`'s `/api/exclude-paper-trade` endpoint (via `paper-delete.js`),
  which appends to `dashboard/paper/paper_excluded.json` and immediately calls
  `import_tradezero.rebuild_calendar()` to regenerate every `calendar_data_YYYY.json`
  from the raw logs (Schwab + TZ live + TZ paper) — since paper is blended into real
  totals, a delete has to rebuild the whole calendar, not just a paper-only file.
  **Requires the dashboard be served via `dashboard_server.py` / `launch_dashboard.py`**
  — plain `python -m http.server` has no write endpoint, so delete fails with an
  on-screen error against it.
- **Raw history:** `trade_data/tz_paper_executions.json` is the unfiltered source of
  truth (every fill TradeZero ever reported, exclusions not yet applied) — same
  today-only-API/persist-forward pattern as `tz_live_executions.json`.
- **`trades.html` account filter:** substring-matches `account.toLowerCase()`, so the TZ
  Live button uses `data-acct="tzlive"` (not bare `"tz"`) to avoid also matching
  `"TZPaper"` — mirrored in `calendar_data.py`'s bucket logic (`'tzlive' in acct`, not
  `'tz' in acct`). Keep this in mind if you ever add another account whose name contains
  an existing account's substring.

---

## Dashboard Notes

- All HTML files are standalone, but must be served over `http://` (not `file://`) —
  Chrome/Edge block `fetch()` of local JSON under `file://`. See the serving instructions
  at the top of this file (`dashboard_server.py` / `launch_dashboard.py`)
- Calendar JSONs are split per-year; `reports.html` loads all years via `calendar_index.json`
- OHLCV data in per-day files under `dashboard/ohlcv/` (used by `candlestick-chart.html` and `compute_mfe_mae.py`)
- `candlestick-chart.html`: LightweightCharts SVG overlay for trade markers (z-index layering); always
  rendered inside an iframe popup (`window.self !== window.top` gates its own nav/margin
  logic) — day.html and trades.html each own an identical `.cdl-overlay` popup that
  points the iframe at it with `?year=&month=&day=&symbol=` (optionally `&entry=&exit=`)
- `candlestick-chart.html` trade markers: green entry, blue partial exit, orange/red
  final exit (win/loss), purple for Roth (entry/exit only, no win-loss split) — real
  trades are diamonds, paper trades (account `TZPaper`, see Paper Trades) are circles in
  the same colors (read-only, no click action). Both render under the one "Real trades" toggle, no
  separate paper toggle. A dotted line connects each round-trip's first entry to its
  final exit. Defaults to the 1m timeframe.
- `day.html`: Chart.js intraday PnL chart; symbol table shows one combined "Margin"
  column (no Cash/Roth/TZ/Paper breakdown) — no account-filter UI on this page at all;
  the account-level split (`pnl_cash`/`pnl_roth`/`pnl_tz`/`pnl_paper`) exists only in the
  underlying JSON
- `trades.html` has the dashboard's one real account-filter UI: All/Cash/Roth/TZ
  Live/Paper quick-filter buttons + a colored `.acct-badge` per row (green/purple/teal/amber)
- `reports.html` Monte Carlo: loss cap is `N × R` (clamps losses, does not exclude them); default 1.25R
- Symbol pills show REAL PnL with green `rgb(11,98,71)` / red `rgb(140,31,31)`

---

```bash
# Full pipeline: fetch TradeZero trades → rebuild calendar → OHLCV → MFE/MAE
python import_tradezero.py

# Calendar only (after manually dropping CSVs into trade_data/, no TradeZero fetch)
python calendar_data.py

# Pre-fetch 1-min OHLCV for candlestick charts
python fetch_ohlcv.py

# Compute MFE/MAE from cached OHLCV (run after fetch_ohlcv.py)
python compute_mfe_mae.py
```

---

## External Integrations

| Service | Purpose |
|---------|---------|
| TradeZero API | Live trade fetch (today's filled orders + account balance) |
| Alpaca API (`alpaca-py`, SIP feed) | OHLCV price history (1-min + daily bars) |

Schwab (`schwabdev`) and Tradervue export were retired — `trade_data/*.csv` from Schwab
remains as frozen historical data, still parsed by `calendar_data.py` on every run.
