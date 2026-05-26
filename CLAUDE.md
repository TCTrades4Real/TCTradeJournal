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

fetch_ohlcv.py       → pre-fetch 1-min OHLCV from Schwab (primary) / Massive API (fallback)
                       → write dashboard/ohlcv/ohlcv_YYYY-MM-DD.json (per-day)

compute_mfe_mae.py   → reads per-year calendar JSONs + per-day OHLCV
                       → adds mfe/mae fields to each roundtrip in calendar JSONs

dashboard/
  index.html         → monthly calendar view
  month.html         → monthly PnL grid
  day.html           → daily symbol table + intraday chart
  candlestick.html   → per-symbol candlestick chart + trade markers
  reports.html       → trading stats, equity curves, MFE/MAE analysis, Monte Carlo
  trades.html        → trade log
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
3. **fetch_ohlcv.py** → Schwab/Massive API → `dashboard/ohlcv/ohlcv_YYYY-MM-DD.json`
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
| Schwab API (`schwabdev`) | Live trade fetch, price history (primary OHLCV source, ~10 days) |
| Massive API | OHLCV bar data 1-min, all dates (fallback) |
| Tradervue API | Optional trade journal import |
| MrProfit | CSV export format |
| HostGator FTP | Hosts tctrades.com dashboard |
