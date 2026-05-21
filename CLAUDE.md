# TCTradeJournal — Project Guide

## Overview
Day-trading journal. Pulls real trades from Schwab and renders a browser-based dashboard.

---

## Architecture

```
export.py           → fetch Schwab executions → write trade_data/ CSVs
                      → auto-runs calendar_data.py

calendar_data.py    → parse trade_data/ CSVs
                      → write dashboard/calendar_data.json

fetch_ohlcv.py      → pre-fetch 1-min OHLCV from Massive API
                      → write dashboard/ohlcv_data.json  (used by candlestick.html)

dashboard/
  index.html        → monthly calendar view
  month.html        → monthly PnL grid
  day.html          → daily symbol table + intraday chart
  candlestick.html  → per-symbol candlestick chart + trade markers
```

---

## Data Flow

1. **export.py** → Schwab API → `trade_data/YYYY-MM-DD-AccountStatement.csv`
2. **calendar_data.py** → reads all CSVs → `dashboard/calendar_data.json`
3. **Dashboard HTML files** → load JSON files locally (no server needed, open as `file://`)

---

## Key Files

| File | Purpose |
|------|---------|
| `utilities/config.py` | Schwab API keys, account hash, Tradervue creds |
| `dashboard/calendar_data.json` | All trade dates, symbols, executions, daily PnL |
| `dashboard/ohlcv_data.json` | 1-min OHLCV cache (for candlestick.html) |

---

## Dashboard Notes

- All HTML files are standalone — open directly as `file://`, no web server needed
- OHLCV data loaded from local JSON files (faster than live API)
- `candlestick.html`: LightweightCharts SVG overlay for trade markers (z-index layering)
- `day.html`: Chart.js intraday PnL chart
- Symbol pills show REAL PnL with green `rgb(11,98,71)` / red `rgb(140,31,31)`

---

```bash
# Full pipeline: fetch new trades → rebuild calendar
python export.py

# Calendar only (after manually dropping CSVs into trade_data/)
python calendar_data.py

# Pre-fetch 1-min OHLCV for candlestick charts
python fetch_ohlcv.py
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

---

## External Integrations

| Service | Purpose |
|---------|---------|
| Schwab API (`schwabdev`) | Live trade fetch, price history |
| Massive API | OHLCV bar data (1-min) |
| Tradervue API | Optional trade journal import |
| MrProfit | CSV export format |
