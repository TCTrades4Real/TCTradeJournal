# TCTradeJournal — Project Guide

## Overview
Day-trading journal. Pulls real trades from TradeZero (live account) and OHLCV price
history from Alpaca, and renders a browser-based dashboard. Historical trades from a
retired Schwab integration remain in `trade_data/*.csv` as frozen history — still parsed
into the calendar on every run, just no longer added to going forward.

---

## Architecture

```
import_tradezero.py  → fetch today's filled TradeZero live orders
                       → merge with historical Schwab CSVs in trade_data/
                       → calendar_data.py's build_and_write() → dashboard/calendar/calendar_data_YYYY.json
                       → write dashboard/account_balance.json (TZ live equity + today's PnL)
                       → auto-runs the rest of the pipeline (see Pipeline section)

calendar_data.py     → parse trade_data/ CSVs (+ executions handed in by import_tradezero.py)
                       → write dashboard/calendar/calendar_data_YYYY.json (per-year)
                       → write dashboard/calendar/calendar_index.json

fetch_ohlcv.py       → pre-fetch 1-min OHLCV from Alpaca (SIP feed, years of history)
                       → write dashboard/ohlcv/ohlcv_YYYY-MM-DD.json (per-day)

compute_mfe_mae.py   → reads per-year calendar JSONs + per-day OHLCV
                       → adds mfe/mae fields to each roundtrip in calendar JSONs

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
2. Merge with historical Schwab executions read from `trade_data/` CSVs, rebuild the
   calendar via `calendar_data.build_and_write()` → per-year calendar JSONs
3. Write `dashboard/account_balance.json` from the TradeZero live account (equity +
   today's realized PnL) — non-fatal if it fails
4. `fetch_ohlcv.py` → fetch/cache missing 1-min OHLCV bars (Alpaca)
5. `compute_mfe_mae.py` → annotate roundtrips with MFE/MAE
6. FTP upload the changed calendar JSONs + balance box (pass `--no-upload` to skip)

**Known limitation:** TradeZero's `/orders` endpoint only returns *today's* orders — no
working historical-range endpoint exists. `import_tradezero.py` must run same-day. It's
safe to re-run later the same day: `calendar_data.py` always rebuilds the full calendar
JSON from scratch, so re-running never double-counts.

`fetch_ohlcv.py` uploads its own changed files to `tctrades.com` incrementally as it runs
(independent of step 6 above).

---

## Data Flow

1. **import_tradezero.py** → TradeZero API → merged with `trade_data/YYYY-MM-DD-{Cash|Roth}-AccountStatement.csv` (historical Schwab, frozen)
2. **calendar_data.py** (`build_and_write`, called by import_tradezero.py) → `dashboard/calendar/calendar_data_YYYY.json`
3. **fetch_ohlcv.py** → Alpaca API → `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json`
4. **compute_mfe_mae.py** → reads calendar + OHLCV → writes `mfe`/`mae` into calendar JSONs
5. **Dashboard HTML files** → fetch calendar JSONs via relative paths (works on file:// and tctrades.com)

---

## Key Files

| File | Purpose |
|------|---------|
| `utilities/config.py` | TradeZero API keys (paper + live), Alpaca API keys, Tradervue creds, legacy Schwab fields (unused, kept harmlessly) |
| `utilities/tradezero_client.py` | Thin TradeZero REST client — `get_orders()` (today only), `get_account()` |
| `utilities/alpaca_client.py` | Thin wrapper around alpaca-py's `StockHistoricalDataClient` — `get_minute_bars()`, `get_daily_bars()` |
| `dashboard/calendar/calendar_index.json` | List of available years |
| `dashboard/calendar/calendar_data_YYYY.json` | Per-year roundtrips with mfe/mae, daily PnL, `account` field (`Cash`/`Roth` = historical Schwab, `TZLive` = TradeZero) |
| `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json` | Per-day 1-min OHLCV cache `{SYMBOL: [bars...]}` |
| `dashboard/account_balance.json` | `{balance, pnl_today}` — TradeZero live account equity + today's realized PnL, read by `nav.js`'s sidebar figure and `monte_carlo.html`'s starting-balance seed |

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

## Dashboard Notes

- All HTML files are standalone — open directly as `file://`, no web server needed
- Calendar JSONs are split per-year; `reports.html` loads all years via `calendar_index.json`
- OHLCV data in per-day files under `dashboard/ohlcv/` (used by `candlestick-chart.html` and `compute_mfe_mae.py`)
- `candlestick-chart.html`: LightweightCharts SVG overlay for trade markers (z-index layering); always
  rendered inside an iframe popup (`window.self !== window.top` gates its own nav/margin/auth-adjacent
  logic) — day.html and trades.html each own an identical `.cdl-overlay` popup that
  points the iframe at it with `?year=&month=&day=&symbol=` (optionally `&entry=&exit=`)
- `candlestick-chart.html` symbol pills show one combined "MARGIN" value per symbol (no
  Cash/Roth/TZ breakdown). Buy/sell triangle markers: green/red for Cash and TZLive trades
  (same color, no distinction), purple for Roth trades (still visually distinct)
- `day.html`: Chart.js intraday PnL chart; symbol table shows one combined "Margin" column
  (no Cash/Roth/TZ breakdown) — the account-level split still exists in the underlying JSON
  (`pnl_cash`/`pnl_roth`/`pnl_tz`) and drives the All/Cash/Roth/TZ Live stats-bar tabs
- `reports.html` Monte Carlo: loss cap is `N × R` (clamps losses, does not exclude them); default 1.25R
- Symbol pills show REAL PnL with green `rgb(11,98,71)` / red `rgb(140,31,31)`

---

```bash
# Full pipeline: fetch TradeZero trades → rebuild calendar → OHLCV → MFE/MAE → FTP deploy
python import_tradezero.py

# Calendar only (after manually dropping CSVs into trade_data/, no TradeZero fetch)
python calendar_data.py

# Pre-fetch 1-min OHLCV for candlestick charts
python fetch_ohlcv.py

# Compute MFE/MAE from cached OHLCV (run after fetch_ohlcv.py)
python compute_mfe_mae.py
```

---

## Dashboard FTP Deploy

After modifying any file in `dashboard/` (HTML, JS) — including `nav.js`, `auth.js`, and all `.html` pages — run:

```bash
python ftp_dashboard.py
```

This uploads the changed files to `tctrades.com` using the credentials in `.vscode/sftp.json`. The script accepts optional file paths to upload only specific files:

```bash
python ftp_dashboard.py dashboard/candlestick-chart.html dashboard/nav.js
```

Each upload is size-verified against the local file (with one retry) — a failed
verification raises instead of silently reporting success.

**Always run this after saving dashboard edits.** Do not skip it.

`import_tradezero.py` uploads calendar JSONs and the account balance box itself (not via
`ftp_dashboard.py`'s default file list) as the final pipeline step.

---

## External Integrations

| Service | Purpose |
|---------|---------|
| TradeZero API | Live trade fetch (today's filled orders + account balance) |
| Alpaca API (`alpaca-py`, SIP feed) | OHLCV price history (1-min + daily bars) |
| HostGator FTP | Hosts tctrades.com dashboard |

Schwab (`schwabdev`) and Tradervue export were retired — `trade_data/*.csv` from Schwab
remains as frozen historical data, still parsed by `calendar_data.py` on every run.
