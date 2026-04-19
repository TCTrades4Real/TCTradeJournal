# TCTradeJournal — Project Guide

## Overview
Day-trading journal and backtesting system. Pulls real trades from Schwab, renders a browser-based dashboard, and runs a bar-by-bar backtester to compare simulated strategy results against live trades.

---

## Architecture

```
export.py           → fetch Schwab executions → write trade_data/ CSVs
                      → auto-runs calendar_data.py then backtester.py

calendar_data.py    → parse trade_data/ CSVs
                      → write dashboard/calendar_data.json

fetch_ohlcv.py      → pre-fetch 1-min OHLCV from Massive API
                      → write dashboard/ohlcv_data.json  (used by candlestick.html)

backtester.py       → fetch 2-min + 30-min bars (Massive API, cached)
                      → run strategy bar-by-bar
                      → write dashboard/backtest_results.json

dashboard/
  index.html        → monthly calendar view
  month.html        → monthly PnL grid
  day.html          → daily symbol table + intraday chart (real + sim)
  candlestick.html  → per-symbol candlestick chart + trade markers
```

---

## Data Flow

1. **export.py** → Schwab API → `trade_data/YYYY-MM-DD-AccountStatement.csv`
2. **calendar_data.py** → reads all CSVs → `dashboard/calendar_data.json`
3. **backtester.py** → reads `calendar_data.json` + fetches bars → `dashboard/backtest_results.json`
4. **Dashboard HTML files** → load JSON files locally (no server needed, open as `file://`)

---

## Key Files

| File | Purpose |
|------|---------|
| `utilities/config.py` | Schwab API keys, account hash, Tradervue creds |
| `dashboard/calendar_data.json` | All trade dates, symbols, executions, daily PnL |
| `dashboard/ohlcv_data.json` | 1-min OHLCV cache (for candlestick.html) |
| `dashboard/ohlcv_backtest_cache.json` | 2-min + 30-min OHLCV cache (for backtester) |
| `dashboard/backtest_results.json` | Sim trades, daily sim PnL, chart points per day |
| `optimize.py` | Grid-search optimizer — scores param combos by daily Sharpe |

---

## Backtester Strategy

Single strategy per symbol/date. 2-min fast / 30-min slow.

| Strategy | Fast TF | Slow TF | Constant |
|----------|---------|---------|----------|
| `[A] 2-min fast / 30-min slow` | 2-min | 30-min | `STRATEGY_A_NAME` |

**Direction:** Long only

---

### Strategy [A] — 2-min fast / 30-min slow

### Constants
```python
BREAKOUT_PCT   = 0.0075   # prev 2-min bar high * 1.0075
SLIPPAGE       = 0.02
RISK_PER_TRADE = 25.0     # dollars risked per trade
ENTRY_START    = 09:30 ET
ENTRY_END      = 16:00 ET (no new entries at/after)
AH_END         = 20:00 ET (exit window)
```

### Entry Conditions (all 7 must be true)
- c0: bar volume >= 25,000
- c1: bar high >= prev_2min_high * 1.0075 (fast) OR (bar high >= prev_30min_high * 1.0075 AND breakout_level > bar VWAP) (slow); entry at lower triggered level
- c2: current 2-min bar MACD >= current 2-min bar Signal  (MACD spans: fast=12, slow=26, signal=9 — optimizable)
- c3: prev_2min_high within 7.5% of EMA9, OR EMA9 within 2.5% of EMA20
- c4: prev 2-min bar high > prev 2-min bar EMA9
- c5: prev 2-min bar EMA9 > prev 2-min bar VWAP
- c6: breakout_level <= min(bar VWAP * 1.10, bar VWAP + 0.50)

### Position Sizing
```
shares       = floor(RISK_PER_TRADE / (entry_price - initial_stop))
entry_price  = lower triggered breakout_level + SLIPPAGE
initial_stop = prev_30min_bar_low * 0.99   (hard stop, at time of entry)
r_unit       = entry_price - initial_stop                  (30-min based)
```

### Exit Conditions (checked in order, first match wins)
- [0] `bar_low <= initial_stop` → exit at `initial_stop - SLIPPAGE` (reason: `neg_r`)
- [2] `bar_low <= highest_slow_low * 0.99` AND `highest_slow_low * 0.99 > entry_price + 0.25` → exit at `highest_slow_low * 0.99 - SLIPPAGE` (reason: `trail_slow`)
  - `highest_slow_low` = running max of all fully-closed 30-min bar lows since entry

### Warmup
2 prior business days of 2-min bars prepended before computing indicators (EMA, MACD, VWAP). Warmup rows dropped before the loop.

### Loop Start
The bar-by-bar loop starts at the same bar as the first real trade on the day (`inject_entry_time`). All 7 entry conditions apply normally from that bar — no forced entry.

---

## backtest_results.json Structure

```json
{
  "YYYY-MM-DD": {
    "trades": 2,
    "pnl": 47.50,
    "symbols": { "ACXP": { "trades": 2, "pnl": 47.50 } },
    "chart": [{ "t": "10:32", "pnl": 22.50 }, { "t": "11:15", "pnl": 47.50 }],
    "sim_trades": [
      {
        "symbol": "ACXP",
        "entry_time": "10:30",
        "entry_price": 3.8412,
        "exit_time": "10:32",
        "exit_price": 3.9200,
        "pnl": 22.50
      }
    ]
  }
}
```

---

## Dashboard Notes

- All HTML files are standalone — open directly as `file://`, no web server needed
- OHLCV data loaded from local JSON files (faster than live API)
- `candlestick.html`: LightweightCharts SVG overlay for trade markers (z-index layering)
- `day.html`: Chart.js dual dataset — real PnL (white) + sim PnL (purple dashed), unified x-axis
- Symbol pills show stacked REAL/SIM sub-pills with green `rgb(11,98,71)` / red `rgb(140,31,31)`
- Real/Sim toggle switches independently show/hide trade markers on the candlestick chart

---

## Backtester Maintenance Rules

> **Strategy sync rule:** Whenever entry conditions, exit conditions, constants (`BREAKOUT_PCT`, `SLIPPAGE`, `RISK_PER_TRADE`, session times), or position sizing logic change in `backtester.py`, you MUST also update:
> 1. `print_rules()` in `backtester.py` — the human-readable rules printed after every run
> 2. The **Backtester Strategy** section of this `CLAUDE.md` file

> **Optimizer sync rule:** Whenever a parameter is added to or removed from `run_strategy`'s `params` block in `backtester.py`, you MUST also update `optimize.py`:
> 1. `GRID` — add/remove the parameter and its candidate values
> 2. `BASELINE` — add/remove the parameter's default value
> 3. `print_results()` — add/remove the column from the header and row format

```bash
# Full pipeline: fetch new trades → rebuild calendar → run backtest
python export.py

# Calendar only (after manually dropping CSVs into trade_data/)
python calendar_data.py

# Backtest only
python backtester.py

# Single date/symbol smoke test
python backtester.py --date 2026-03-16 --symbol ACXP

# Date range
python backtester.py --start 2026-01-01 --end 2026-03-31

# Rebuild bar cache from scratch
python backtester.py --refresh

# Pre-fetch 1-min OHLCV for candlestick charts
python fetch_ohlcv.py

# Grid-search parameter optimization (run backtester.py first to build cache)
python optimize.py
python optimize.py --top 20
```

---

## External Integrations

| Service | Purpose |
|---------|---------|
| Schwab API (`schwabdev`) | Live trade fetch, price history |
| Massive API | OHLCV bar data (1-min, 5-min, 30-min) |
| Tradervue API | Optional trade journal import |
| MrProfit | CSV export format |
