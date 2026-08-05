# TCTradeJournal — Project Guide

## Overview
Day-trading journal. Pulls real trades from Schwab and renders a browser-based dashboard.

---

## Architecture

```
export.py            → fetch Schwab executions → write trade_data/ CSVs
                       → auto-runs full pipeline (see Pipeline section)

calendar_data.py     → parse trade_data/ CSVs
                       → write dashboard/calendar/calendar_data_YYYY.json (per-year)
                       → write dashboard/calendar/calendar_index.json

fetch_ohlcv.py       → pre-fetch 1-min OHLCV from Schwab (~10 days of history)
                       → write dashboard/ohlcv/ohlcv_YYYY-MM-DD.json (per-day)

compute_mfe_mae.py   → reads per-year calendar JSONs + per-day OHLCV
                       → adds mfe/mae fields to each roundtrip in calendar JSONs

fetch_ohlcv_daily.py → pre-fetch daily OHLCV for watchlist callout symbols (standalone,
                       not part of the export.py pipeline — run manually after adding callouts)
                       → write dashboard/ohlcv_daily/SYMBOL.json

dashboard/
  index.html         → monthly calendar view
  month.html         → monthly PnL grid
  day.html           → daily symbol table + intraday chart
  candlestick.html   → per-symbol candlestick chart + trade markers
  reports.html       → trading stats, equity curves, MFE/MAE analysis, Monte Carlo
  trades.html        → trade log
  watchlists.html    → catalog of a trader's watchlist callouts + setup playbook stats
  api/setups.php     → write endpoint for watchlists.html (cross-device sync)
```

---

## Full Pipeline (export.py)

Running `python export.py` executes the entire pipeline in order:

1. Fetch Schwab executions → write `trade_data/` CSVs
2. `calendar_data.py` → write per-year calendar JSONs
3. `fetch_ohlcv.py` → fetch/cache missing 1-min OHLCV bars
4. `compute_mfe_mae.py` → annotate roundtrips with MFE/MAE
5. FTP upload all calendar JSONs, changed OHLCV files, and all dashboard HTML/JS to tctrades.com

---

## Data Flow

1. **export.py** → Schwab API → `trade_data/YYYY-MM-DD-AccountStatement.csv`
2. **calendar_data.py** → reads all CSVs → `dashboard/calendar/calendar_data_YYYY.json`
3. **fetch_ohlcv.py** → Schwab API → `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json`
4. **compute_mfe_mae.py** → reads calendar + OHLCV → writes `mfe`/`mae` into calendar JSONs
5. **Dashboard HTML files** → fetch calendar JSONs via relative paths (works on file:// and tctrades.com)

---

## Key Files

| File | Purpose |
|------|---------|
| `utilities/config.py` | Schwab API keys, account hash, Tradervue creds |
| `dashboard/calendar/calendar_index.json` | List of available years |
| `dashboard/calendar/calendar_data_YYYY.json` | Per-year roundtrips with mfe/mae, daily PnL |
| `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json` | Per-day 1-min OHLCV cache `{SYMBOL: [bars...]}` |
| `dashboard/ohlcv/.uploaded` | Manifest of OHLCV files already FTP'd (skips re-upload) |
| `dashboard/ohlcv_daily/SYMBOL.json` | Per-symbol daily OHLCV cache for watchlist callout charts (array of bars, `time` as `YYYY-MM-DD`) |
| `dashboard/watchlist_setups/data.json` | Watchlist callouts + setup-type taxonomy — synced live via `dashboard/api/setups.php`, not pushed by routine `ftp_dashboard.py` runs |

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

## Watchlists (dashboard/watchlists.html)

- Catalogs the setups a trader you follow calls out in his nightly watchlists: logged as
  "callouts" (date, symbol(s), setup type, thesis, key levels, outcome), grouped into a
  "Setup Playbook" of named setup types with computed stats (PnL, accuracy, profit factor,
  avg winner/loser, cents/share, etc.) drawn from your own trades on the linked symbol/date.
- **Trade linking**: a callout's date is the night the watchlist was posted — the actual
  trade day is the *day after*. All trade-linking and chart-fetching windows use `date + 1`.
- **Sync**: no local-edit-then-publish for this data — `dashboard/api/setups.php` lets the
  page write directly to `dashboard/watchlist_setups/data.json` on the live server, gated by
  a per-device write key bootstrapped through the existing Google sign-in (see `auth.js`).
  This is why `ftp_dashboard.py` deliberately excludes that JSON from its default push.
- **Charts**: clicking a symbol pill on a callout opens daily (~2 week trailing) and 1-minute
  charts for the trade day, rendered from `dashboard/ohlcv_daily/SYMBOL.json` and the existing
  `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json`. Run `python fetch_ohlcv_daily.py` after adding new
  callouts to populate the daily cache — it isn't part of the `export.py` pipeline.

---

## Dashboard Notes

- All HTML files are standalone — open directly as `file://`, no web server needed
- Calendar JSONs are split per-year; `reports.html` loads all years via `calendar_index.json`
- OHLCV data in per-day files under `dashboard/ohlcv/` (used by `candlestick.html` and `compute_mfe_mae.py`)
- `candlestick.html`: LightweightCharts SVG overlay for trade markers (z-index layering)
- `day.html`: Chart.js intraday PnL chart
- `reports.html` Monte Carlo: loss cap is `N × R` (clamps losses, does not exclude them); default 1.25R
- Symbol pills show REAL PnL with green `rgb(11,98,71)` / red `rgb(140,31,31)`

---

```bash
# Full pipeline: fetch new trades → rebuild calendar → MFE/MAE → FTP deploy
python export.py

# Calendar only (after manually dropping CSVs into trade_data/)
python calendar_data.py

# Pre-fetch 1-min OHLCV for candlestick charts
python fetch_ohlcv.py

# Compute MFE/MAE from cached OHLCV (run after fetch_ohlcv.py)
python compute_mfe_mae.py

# Pre-fetch daily OHLCV for watchlist callout symbols (run after adding new callouts)
python fetch_ohlcv_daily.py
```

---

## Dashboard FTP Deploy

After modifying any file in `dashboard/` (HTML, JS) — including `nav.js`, `auth.js`, and all `.html` pages — run:

```bash
python ftp_dashboard.py
```

This uploads the changed files to `tctrades.com` using the credentials in `.vscode/sftp.json`. The script accepts optional file paths to upload only specific files:

```bash
python ftp_dashboard.py dashboard/candlestick.html dashboard/nav.js
```

**Always run this after saving dashboard edits.** Do not skip it.

`export.py` runs FTP deploy automatically as the final pipeline step.

---

## External Integrations

| Service | Purpose |
|---------|---------|
| Schwab API (`schwabdev`) | Live trade fetch, price history (OHLCV source, ~10 days) |
| Tradervue API | Optional trade journal import |
| MrProfit | CSV export format |
| HostGator FTP | Hosts tctrades.com dashboard |
