"""
optimize.py — Grid-search parameter optimization for backtester strategies.

Scores each parameter combination by daily Sharpe ratio:
  score = mean(daily_R) / std(daily_R, ddof=1)
  daily_R = sum of R-multiples for all trades on that day (0 on no-trade days)
  All calendar trading days included so zero-trade days penalise the denominator.

Usage:
    python optimize.py                  # optimize strategy
    python optimize.py --top 20         # show top 20 results (default: 10)

Prerequisites:
    Run `python backtester.py` first to populate the bar cache.
    Pairs with missing bars are silently skipped.
"""

import argparse
import itertools
import json
import os
import sys
import time

import pandas as pd

from backtester import (
    CALENDAR_PATH,
    CACHE_PATH,
    EASTERN,
    STRATEGY_A_NAME,
    _get_first_entry,
    _iter_pairs,
    _prior_business_days,
    bars_to_df,
    load_cache,
    run_strategy,
)

# ── Parameter grid ────────────────────────────────────────────────────────────
GRID = {
    'breakout_pct': [0.005, 0.0075, 0.010, 0.015],
    'vol_min':      [10_000, 25_000, 50_000,75_000],
    'stop_mult':    [0.96, 0.97, 0.98, 0.99],
    'macd_fast':    [8, 9, 12],
    'macd_slow':    [21, 20, 26],
}

# Current strategy defaults — always present in results, marked [baseline]
BASELINE = {
    'breakout_pct': 0.0075,
    'vol_min':      25_000,
    'stop_mult':    0.99,
    'macd_fast':    12,
    'macd_slow':    26,
}

MIN_TRADES = 10   # discard combos with fewer trades


# ── Scoring ───────────────────────────────────────────────────────────────────
def sharpe_score(trades: list[dict], all_dates: set[str]) -> float:
    """
    Daily Sharpe using R-multiples, sample std (ddof=1), zero-filled for no-trade days.
    all_dates: every unique trading date in the backtest universe.
    """
    if len(trades) < MIN_TRADES:
        return -999.0
    day_r: dict[str, float] = {d: 0.0 for d in all_dates}
    for t in trades:
        day_r[t['date']] = day_r.get(t['date'], 0.0) + t['rr']
    vals = list(day_r.values())
    n = len(vals)
    if n < 2:
        return -999.0
    mu  = sum(vals) / n
    std = (sum((v - mu) ** 2 for v in vals) / (n - 1)) ** 0.5
    return mu / (std + 1e-9)


# ── Bar pre-loader ────────────────────────────────────────────────────────────
def preload_bars(pairs: list[tuple[str, str]],
                 cache: dict,
                 fast_intervals: list[int]) -> dict:
    """
    Build a dict keyed (interval_min, date_str, symbol) → pd.DataFrame.
    Includes 2 prior business days per pair for warmup.
    Only loads from cache — no live fetching.
    """
    print('Pre-loading bars from cache…')
    bar_dfs: dict[tuple, pd.DataFrame] = {}
    dates_needed: set[tuple[int, str, str]] = set()

    for date_str, symbol in pairs:
        for iv in fast_intervals + [30]:
            dates_needed.add((iv, date_str, symbol))
        for wd in _prior_business_days(date_str, 2):
            for iv in fast_intervals:
                dates_needed.add((iv, wd, symbol))

    loaded = skipped = 0
    for iv, date_str, symbol in dates_needed:
        key = (iv, date_str, symbol)
        raw = cache.get(str(iv), {}).get(date_str, {}).get(symbol)
        if raw:
            bar_dfs[key] = bars_to_df(raw)
            loaded += 1
        else:
            skipped += 1

    print(f'  Loaded {loaded} bar sets, skipped {skipped} (not in cache)\n')
    return bar_dfs


# ── Single combo evaluation ───────────────────────────────────────────────────
def eval_combo(params: dict,
               pairs: list[tuple[str, str]],
               bar_dfs: dict,
               fast_interval: int,
               strategy_name: str,
               cal: dict) -> list[dict]:
    trades: list[dict] = []
    for date_str, symbol in pairs:
        fast_key  = (fast_interval, date_str, symbol)
        slow_key  = (30,            date_str, symbol)
        df_fast   = bar_dfs.get(fast_key)
        df_slow   = bar_dfs.get(slow_key)
        if df_fast is None or df_slow is None or df_fast.empty or df_slow.empty:
            continue

        warmup_frames = []
        for wd in _prior_business_days(date_str, 2):
            wf = bar_dfs.get((fast_interval, wd, symbol))
            if wf is not None and not wf.empty:
                warmup_frames.append(wf)
        df_wu = pd.concat(warmup_frames).sort_values('datetime').reset_index(drop=True) \
                if warmup_frames else None

        _, inject_time = _get_first_entry(cal, date_str, symbol)

        t, _ = run_strategy(
            df_fast, df_slow, symbol, date_str,
            df_fast_warmup=df_wu,
            inject_entry_time=inject_time,
            strategy_name=strategy_name,
            params=params,
        )
        trades.extend(t)
    return trades


# ── Results table ─────────────────────────────────────────────────────────────
def print_results(results: list[dict], strategy_name: str, top_n: int):
    sep = '─' * 112
    print(f'\n{sep}')
    print(f'TOP {top_n}  {strategy_name}')
    print(f'{"RANK":>5}  {"bk%":>6}  {"vol":>6}  {"stp":>5}  {"mf":>4}  {"ms":>4}  '
          f'{"TRADES":>7}  {"WIN%":>6}  {"GROSS P&L":>11}  {"GROSS R":>9}  {"SHARPE":>8}  NOTE')
    print(sep)
    for r in results[:top_n]:
        is_baseline = all(abs(r['params'].get(k, 0) - BASELINE[k]) < 1e-9
                          for k in BASELINE)
        note = '[baseline]' if is_baseline else ''
        win_pct = r['wins'] / r['trades'] * 100 if r['trades'] else 0
        print(f'{r["rank"]:>5}  {r["params"]["breakout_pct"]:>6.4f}  '
              f'{r["params"]["vol_min"]:>6.0f}  {r["params"]["stop_mult"]:>5.2f}  '
              f'{r["params"]["macd_fast"]:>4}  {r["params"]["macd_slow"]:>4}  '
              f'{r["trades"]:>7}  {win_pct:>5.1f}%  '
              f'{r["gross_pnl"]:>+11.2f}  {r["gross_r"]:>+9.2f}  {r["sharpe"]:>8.4f}  {note}')
    print(sep)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f'{h}h {m:02d}m' if h else f'{m}m {s:02d}s'


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Backtest parameter optimizer')
    parser.add_argument('--top', type=int, default=10)
    args = parser.parse_args()

    if not os.path.exists(CALENDAR_PATH):
        sys.exit(f'ERROR: {CALENDAR_PATH} not found.  Run python calendar_data.py first.')
    if not os.path.exists(CACHE_PATH):
        sys.exit(f'ERROR: {CACHE_PATH} not found.  Run python backtester.py first to build cache.')

    with open(CALENDAR_PATH) as f:
        cal = json.load(f)
    cache = load_cache()

    class _Args:
        date = start = end = symbol = None
    pairs = _iter_pairs(cal, _Args())
    all_dates = {date for date, _ in pairs}
    print(f'Pairs: {len(pairs)}  |  Trading days: {len(all_dates)}  |  Grid combos: {len(list(itertools.product(*GRID.values())))}\n')

    strategies = [(STRATEGY_A_NAME, 2)]

    fast_intervals = list({iv for _, iv in strategies})
    bar_dfs = preload_bars(pairs, cache, fast_intervals)

    keys   = list(GRID.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*GRID.values())]

    for strategy_name, fast_iv in strategies:
        n_combos = len(combos)
        print(f'Optimizing {strategy_name}  ({n_combos} combos × {len(pairs)} pairs)…\n')
        t0 = time.time()
        results = []
        for i, params in enumerate(combos, 1):
            trades = eval_combo(params, pairs, bar_dfs, fast_iv, strategy_name, cal)
            score  = sharpe_score(trades, all_dates)
            wins    = sum(1 for t in trades if t['pnl'] > 0)
            gross   = sum(t['pnl'] for t in trades)
            gross_r = sum(t['rr']  for t in trades)
            results.append({
                'params':    params,
                'trades':    len(trades),
                'wins':      wins,
                'gross_pnl': round(gross,   2),
                'gross_r':   round(gross_r, 4),
                'sharpe':    round(score,   4),
            })
            elapsed = time.time() - t0
            eta = (elapsed / i) * (n_combos - i)
            print(
                f'  [{i:>3}/{n_combos}]  '
                f'bk={params["breakout_pct"]:.4f}  vol={params["vol_min"]:>6.0f}  '
                f'stp={params["stop_mult"]:.2f}  mf={params["macd_fast"]:>2}  ms={params["macd_slow"]:>2}'
                f'  →  trades={len(trades):>4}  sharpe={score:>7.4f}'
                f'  ({_fmt_duration(elapsed)} elapsed, ~{_fmt_duration(eta)} left)'
            )

        print(f'\nAll {n_combos} combos done in {_fmt_duration(time.time() - t0)}. Sorting…')
        results.sort(key=lambda r: r['sharpe'], reverse=True)
        for rank, r in enumerate(results, 1):
            r['rank'] = rank

        print_results(results, strategy_name, args.top)


if __name__ == '__main__':
    main()
