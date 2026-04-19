"""
backtester.py — Bar-by-bar intraday backtester
Strategy: Long-only breakout with 2-min fast / 30-min slow TF stops

Usage:
    python backtester.py                           # all dates/symbols
    python backtester.py --date 2026-03-16         # single date
    python backtester.py --symbol ACXP             # single symbol
    python backtester.py --date 2026-03-16 --symbol ACXP
    python backtester.py --refresh                 # rebuild bar cache
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from utilities import config

# ── Constants ─────────────────────────────────────────────────────────────────
BREAKOUT_PCT    = 0.0075
SLIPPAGE        = 0.02
RISK_PER_TRADE  = 25.0
SESSION_START   = time(7, 0)    # bar fetch window
ENTRY_START     = time(9, 30)   # no entries before market open
ENTRY_END       = time(16, 0)   # no new entries at/after close
AH_END          = time(20, 0)   # exit window closes
EASTERN         = ZoneInfo('America/New_York')
STRATEGY_A_NAME = '[A] 2-min fast / 30-min slow'

MASSIVE_API_KEY = 'REDACTED_API_KEY'
MASSIVE_BASE    = 'https://api.massive.com'

ROOT          = os.path.dirname(__file__)
CALENDAR_PATH = os.path.join(ROOT, 'dashboard', 'calendar_data.json')
CACHE_PATH    = os.path.join(ROOT, 'dashboard', 'ohlcv_backtest_cache.json')

# ── Cache I/O ─────────────────────────────────────────────────────────────────
def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print(f'[warn] Cache corrupted, starting fresh: {CACHE_PATH}')
    return {}


def save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, separators=(',', ':'))

# ── Data Fetching ─────────────────────────────────────────────────────────────
def _fetch_massive(symbol: str, date_str: str, interval_min: int) -> list[dict]:
    url = (f'{MASSIVE_BASE}/v2/aggs/ticker/{symbol}'
           f'/range/{interval_min}/minute/{date_str}/{date_str}')
    params = {'adjusted': 'false', 'sort': 'asc', 'limit': 50000,
              'apiKey': MASSIVE_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        results = resp.json().get('results') or []
        return [
            {
                'time':   r['t'] // 1000,
                'open':   round(r['o'], 4),
                'high':   round(r['h'], 4),
                'low':    round(r['l'], 4),
                'close':  round(r['c'], 4),
                'volume': int(r.get('v') or 0),
            }
            for r in results
        ]
    except Exception as e:
        print(f'  [massive error] {symbol} {date_str} {interval_min}m: {e}')
        return []


def _agg_candles(schwab_candles: list[dict], interval_min: int) -> list[dict]:
    """Aggregate 1-min Schwab candles (datetime field = epoch ms) to interval_min bars."""
    buckets: dict[int, dict] = {}
    for c in schwab_candles:
        ts = c['datetime'] // 1000
        dt = datetime.fromtimestamp(ts, tz=EASTERN)
        snapped = (dt.minute // interval_min) * interval_min
        bucket_dt = dt.replace(minute=snapped, second=0, microsecond=0)
        bts = int(bucket_dt.timestamp())
        if bts not in buckets:
            buckets[bts] = {
                'time':   bts,
                'open':   round(c['open'],  4),
                'high':   round(c['high'],  4),
                'low':    round(c['low'],   4),
                'close':  round(c['close'], 4),
                'volume': int(c.get('volume', 0)),
            }
        else:
            b = buckets[bts]
            b['high']   = round(max(b['high'], c['high']), 4)
            b['low']    = round(min(b['low'],  c['low']),  4)
            b['close']  = round(c['close'], 4)
            b['volume'] += int(c.get('volume', 0))
    return sorted(buckets.values(), key=lambda x: x['time'])


def _fetch_schwab(client, symbol: str, date_str: str, interval_min: int) -> list[dict]:
    try:
        start = datetime.strptime(date_str, '%Y-%m-%d').replace(
            hour=0, minute=0, second=0, tzinfo=EASTERN)
        end = start.replace(hour=23, minute=59, second=59)
        # Schwab supports minute frequencies: 1, 5, 10, 15, 30
        schwab_freq = 30 if interval_min == 30 else 1
        resp = client.price_history(
            symbol,
            periodType='day',
            frequencyType='minute',
            frequency=schwab_freq,
            startDate=start,
            endDate=end,
            needExtendedHoursData=True,
        )
        candles = resp.json().get('candles', [])
        if not candles:
            return []
        if interval_min <= schwab_freq:
            return [
                {
                    'time':   c['datetime'] // 1000,
                    'open':   round(c['open'],  4),
                    'high':   round(c['high'],  4),
                    'low':    round(c['low'],   4),
                    'close':  round(c['close'], 4),
                    'volume': int(c.get('volume', 0)),
                }
                for c in candles
            ]
        return _agg_candles(candles, interval_min)
    except Exception as e:
        print(f'  [schwab error] {symbol} {date_str} {interval_min}m: {e}')
        return []


def fetch_bars(symbol: str, date_str: str, interval_min: int,
               client=None, cache: dict | None = None) -> list[dict]:
    """Return cached bars or fetch from Schwab (primary) / Massive (fallback)."""
    key = str(interval_min)
    if cache is not None:
        cached = cache.get(key, {}).get(date_str, {}).get(symbol)
        if cached is not None:
            return cached

    bars: list[dict] = []
    if client is not None:
        bars = _fetch_schwab(client, symbol, date_str, interval_min)
    if not bars:
        bars = _fetch_massive(symbol, date_str, interval_min)

    if cache is not None and bars:
        cache.setdefault(key, {}).setdefault(date_str, {})[symbol] = bars

    return bars

# ── DataFrame Construction ────────────────────────────────────────────────────
def bars_to_df(bars: list[dict]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(
            columns=['time', 'open', 'high', 'low', 'close', 'volume', 'datetime'])
    df = pd.DataFrame(bars)
    df['datetime'] = (pd.to_datetime(df['time'], unit='s', utc=True)
                        .dt.tz_convert(EASTERN))
    return df.sort_values('datetime').reset_index(drop=True)

# ── Indicators ────────────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame,
                       macd_fast: int = 12,
                       macd_slow: int = 26,
                       macd_signal: int = 9) -> pd.DataFrame:
    """
    Add vwap, ema9, ema20, macd, signal columns.
    VWAP resets per calendar day at 09:30 ET.
    EMA/MACD computed over the full frame so warmup rows seed the filter.
    """
    df = df.copy()
    df['vwap'] = np.nan

    for d, _ in df.groupby(df['datetime'].dt.date):
        mask = ((df['datetime'].dt.date == d) &
                (df['datetime'].dt.time >= ENTRY_START))
        sess = df.loc[mask]
        if sess.empty:
            continue
        tp      = (sess['high'] + sess['low'] + sess['close']) / 3
        cum_tpv = (tp * sess['volume']).cumsum().to_numpy()
        cum_vol = sess['volume'].cumsum().to_numpy()
        vwap    = np.where(cum_vol > 0, cum_tpv / cum_vol, np.nan)
        df.loc[mask, 'vwap'] = vwap

    df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    ema_f = df['close'].ewm(span=macd_fast,   adjust=False).mean()
    ema_s = df['close'].ewm(span=macd_slow,   adjust=False).mean()
    df['macd']   = ema_f - ema_s
    df['signal'] = df['macd'].ewm(span=macd_signal, adjust=False).mean()

    return df

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_prev_slow_bar(df_slow: pd.DataFrame, bar_time: pd.Timestamp) -> pd.Series | None:
    """Return the most recent 30-min bar that has fully closed by bar_time.
    Bar datetime is open time; close time = open + 30 min."""
    close_times = df_slow['datetime'] + pd.Timedelta(minutes=30)
    candidates  = df_slow[close_times <= bar_time]
    if candidates.empty:
        return None
    return candidates.iloc[-1]


def _prior_business_days(date_str: str, n: int = 2) -> list[str]:
    end   = pd.Timestamp(date_str) - pd.Timedelta(days=1)
    dates = pd.bdate_range(end=end, periods=n)
    return [d.strftime('%Y-%m-%d') for d in dates]


def _get_first_entry(cal: dict, date_str: str, symbol: str
                     ) -> tuple[float | None, pd.Timestamp | None]:
    """Return (price, tz-aware Timestamp) of the first buy execution for symbol on date_str."""
    year, month, day = date_str.split('-')
    execs = (cal.get(year, {})
                .get(str(int(month)), {})
                .get('days', {})
                .get(str(int(day)), {})
                .get('executions', []))
    for ex in execs:
        if ex.get('symbol') == symbol and ex.get('side') == 'buy':
            dt = datetime.strptime(f'{date_str} {ex["time"]}', '%Y-%m-%d %H:%M:%S')
            ts = pd.Timestamp(dt).tz_localize(EASTERN)
            return float(ex['price']), ts
    return None, None


def _iter_pairs(cal: dict, args) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for year, ydata in cal.items():
        for month, mdata in ydata.items():
            for day, ddata in (mdata.get('days') or {}).items():
                date_str = f'{year}-{month.zfill(2)}-{day.zfill(2)}'
                if args.date and date_str != args.date:
                    continue
                if args.start and date_str < args.start:
                    continue
                if args.end and date_str > args.end:
                    continue
                for sym in (ddata.get('symbols') or {}):
                    if args.symbol and sym != args.symbol:
                        continue
                    pairs.append((date_str, sym))
    return sorted(pairs)

# ── Strategy ──────────────────────────────────────────────────────────────────
def run_strategy(
    df_fast: pd.DataFrame,
    df_slow: pd.DataFrame,
    symbol: str,
    date_str: str,
    start_idx: int = 2,
    df_fast_warmup: pd.DataFrame | None = None,
    inject_entry_time=None,
    strategy_name: str = STRATEGY_A_NAME,
    params: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Bar-by-bar simulation. Returns (trades, near_misses).

    df_fast / df_fast_warmup contain raw OHLCV; indicators are computed here.
    df_slow is used as-is (OHLCV only) for stop calculation.
    """
    p             = params or {}
    p_breakout    = p.get('breakout_pct',  BREAKOUT_PCT)
    p_vol_min     = p.get('vol_min',       25_000)
    p_ema_fast    = p.get('ema_prox_fast', 0.075)
    p_ema_slow    = p.get('ema_prox_slow', 0.025)
    p_stop_mult   = p.get('stop_mult',     0.99)
    p_macd_fast   = p.get('macd_fast',    12)
    p_macd_slow   = p.get('macd_slow',    26)
    p_macd_signal = p.get('macd_signal',  9)

    target_date = pd.Timestamp(date_str).date()

    # ── Build indicator frame (warmup + target day) ───────────────────────────
    if df_fast_warmup is not None and not df_fast_warmup.empty:
        combined = (pd.concat([df_fast_warmup, df_fast], ignore_index=True)
                      .sort_values('datetime').reset_index(drop=True))
        combined = compute_indicators(combined, p_macd_fast, p_macd_slow, p_macd_signal)
        df = (combined[combined['datetime'].dt.date == target_date]
                .copy().reset_index(drop=True))
    else:
        df = compute_indicators(df_fast.copy(), p_macd_fast, p_macd_slow, p_macd_signal)
        df = (df[df['datetime'].dt.date == target_date]
                .copy().reset_index(drop=True))

    # ── Session filter ────────────────────────────────────────────────────────
    df = df[(df['datetime'].dt.time >= SESSION_START) &
            (df['datetime'].dt.time <  AH_END)].copy().reset_index(drop=True)

    df_slow = (df_slow[(df_slow['datetime'].dt.time >= SESSION_START) &
                       (df_slow['datetime'].dt.time <  AH_END)]
                 .copy().reset_index(drop=True))

    if df.empty or len(df) < start_idx + 1:
        return [], []

    # ── Advance loop start to the first real trade bar ───────────────────────
    if inject_entry_time is not None:
        inject_ts = inject_entry_time if isinstance(inject_entry_time, pd.Timestamp) \
                    else pd.Timestamp(inject_entry_time)
        if inject_ts.tzinfo is None:
            inject_ts = inject_ts.tz_localize(EASTERN)
        diffs = (df['datetime'] - inject_ts).abs()
        start_idx = max(1, int(diffs.idxmin()))

    trades:      list[dict] = []
    near_misses: list[dict] = []

    # ── Position state ────────────────────────────────────────────────────────
    in_position      = False
    entry_price      = 0.0
    initial_stop     = 0.0
    r_unit           = 0.0
    entry_bar_time: pd.Timestamp | None = None
    entry_shares      = 0
    highest_slow_low  = 0.0

    for i in range(start_idx, len(df)):
        bar      = df.iloc[i]
        prev_bar = df.iloc[i - 1]
        bar_time: pd.Timestamp = bar['datetime']

        # ── NO POSITION ───────────────────────────────────────────────────────
        if not in_position:
            if bar_time.time() < ENTRY_START or bar_time.time() >= ENTRY_END:
                continue

            pbh1           = float(prev_bar['high'])
            breakout_level = pbh1 * (1 + p_breakout)
            prev_slow_c1   = _get_prev_slow_bar(df_slow, bar_time)
            c1             = float(bar['high']) >= breakout_level

            c0 = int(bar['volume']) >= p_vol_min
            c2 = float(bar['macd']) >= float(bar['signal'])
            c3 = (
                abs(pbh1 - float(prev_bar['ema9'])) /
                    max(abs(pbh1), abs(float(prev_bar['ema9'])), 1e-10) <= p_ema_fast
                or
                abs(float(prev_bar['ema9']) - float(prev_bar['ema20'])) /
                    max(abs(float(prev_bar['ema9'])), abs(float(prev_bar['ema20'])), 1e-10) <= p_ema_slow
            )
            c4 = float(prev_bar['high']) > float(prev_bar['ema9'])
            pv = prev_bar['vwap']
            c5 = (not pd.isna(pv)) and float(prev_bar['ema9']) > float(pv)
            conditions = [c0, c1, c2, c3, c4, c5]
            entry_ok   = all(conditions)

            if entry_ok:
                prev_slow = prev_slow_c1
                if prev_slow is None:
                    continue
                ep   = breakout_level + SLIPPAGE
                stp  = float(prev_slow['low']) * p_stop_mult    # hard stop: 30-min based
                ru   = ep - stp                                 # 1R: 30-min based
                shrs = math.floor(RISK_PER_TRADE / max(abs(ru), 1e-9))
                if shrs <= 0:
                    continue
                in_position      = True
                entry_price      = ep
                initial_stop     = stp
                r_unit           = ru
                entry_shares     = shrs
                entry_bar_time    = bar_time
                highest_slow_low  = float(prev_slow_c1['low']) if prev_slow_c1 is not None else 0.0

            elif c1:
                # Near miss: bar reached breakout but ≥1 condition failed
                pv_val = float(pv) if not pd.isna(pv) else 0.0
                near_misses.append({
                    'date':           date_str,
                    'symbol':         symbol,
                    'time':           bar_time.strftime('%H:%M'),
                    'bar_high':       round(float(bar['high']),      4),
                    'breakout_level': round(breakout_level,           4),
                    'prev_bar_high':  round(pbh1,                     4),
                    'volume':         int(bar['volume']),
                    'macd':           round(float(bar['macd']),       6),
                    'signal':         round(float(bar['signal']),     6),
                    'ema9':           round(float(prev_bar['ema9']),  4),
                    'ema20':          round(float(prev_bar['ema20']), 4),
                    'prev_bar_ema9':  round(float(prev_bar['ema9']), 4),
                    'prev_bar_vwap':  round(pv_val,                   4),
                    'conditions':     conditions,
                })

        # ── IN POSITION ───────────────────────────────────────────────────────
        else:
            exit_price: float | None = None
            reason:     str   | None = None
            bar_low  = float(bar['low'])

            prev_slow_pos = _get_prev_slow_bar(df_slow, bar_time)
            if prev_slow_pos is not None:
                highest_slow_low = max(highest_slow_low, float(prev_slow_pos['low']))
            slow_trail = highest_slow_low * 0.99

            # [0] Hard stop
            if bar_low <= initial_stop:
                exit_price = initial_stop - SLIPPAGE
                reason     = 'neg_r'
            # [1] Slow trailing stop — only when > entry + 0.25
            elif bar_low <= slow_trail and slow_trail > entry_price + 0.25:
                exit_price = slow_trail - SLIPPAGE
                reason     = 'trail_slow'
            if exit_price is not None:
                pnl = (exit_price - entry_price) * entry_shares
                rr  = (exit_price - entry_price) / max(abs(r_unit), 1e-9)
                trades.append({
                    'date':        date_str,
                    'symbol':      symbol,
                    'strategy':    strategy_name,
                    'side':        'LONG',
                    'time':        entry_bar_time,
                    'exit_time':   bar_time,
                    'entry_price': round(entry_price, 4),
                    'stop_price':  round(initial_stop, 4),
                    'exit_price':  round(exit_price,  4),
                    'shares':      entry_shares,
                    'pnl':         round(pnl, 2),
                    'rr':          round(rr,  4),
                    'reason':      reason,
                })
                in_position       = False
                entry_price       = 0.0
                initial_stop      = 0.0
                r_unit            = 0.0
                entry_bar_time    = None
                entry_shares      = 0
                highest_slow_low  = 0.0
            
    # ── EOD forced exit ───────────────────────────────────────────────────────
    if in_position:
        last_bar   = df.iloc[-1]
        ep         = float(last_bar['close']) - SLIPPAGE
        pnl        = (ep - entry_price) * entry_shares
        rr         = (ep - entry_price) / max(abs(r_unit), 1e-9)
        trades.append({
            'date':        date_str,
            'symbol':      symbol,
            'strategy':    strategy_name,
            'side':        'LONG',
            'time':        entry_bar_time,
            'exit_time':   last_bar['datetime'],
            'entry_price': round(entry_price, 4),
            'stop_price':  round(initial_stop, 4),
            'exit_price':  round(ep, 4),
            'shares':      entry_shares,
            'pnl':         round(pnl, 2),
            'rr':          round(rr, 4),
            'reason':      'eod',
        })

    return trades, near_misses

# ── Stats ─────────────────────────────────────────────────────────────────────
def _max_streak(is_win: list[bool]) -> tuple[int, int]:
    max_w = max_l = cur_w = cur_l = 0
    for w in is_win:
        if w:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_w = max(max_w, cur_w)
        max_l = max(max_l, cur_l)
    return max_w, max_l


def print_stats(trades: list[dict], strategy_name: str = STRATEGY_A_NAME):
    if not trades:
        print('\nNo trades generated.\n')
        return

    pnls    = [t['pnl'] for t in trades]
    rrs     = [t['rr']  for t in trades]
    is_win  = [p > 0    for p in pnls]
    win_cnt = sum(is_win)
    los_cnt = len(trades) - win_cnt

    day_pnl: dict[str, float] = {}
    for t in trades:
        day_pnl[t['date']] = day_pnl.get(t['date'], 0.0) + t['pnl']
    win_days  = sum(1 for v in day_pnl.values() if v > 0)
    loss_days = sum(1 for v in day_pnl.values() if v < 0)

    max_cw, max_cl = _max_streak(is_win)

    win_pnls  = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    avg_win   = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0.0
    avg_loss  = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    rr_ratio  = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    gross_pnl = sum(pnls)
    gross_r   = sum(rrs)
    win_pct   = win_cnt / len(trades) * 100

    sep = '─' * 116
    hdr = (f'{"STRATEGY":<26} {"TRADES":>6} | {"WIN DAYS":>8}  {"LOSS DAYS":>9} | '
           f'{"WINS":>5}  {"LOSSES":>6} | {"WIN %":>6} | {"MAX CW":>6}  {"MAX CL":>6} | '
           f'{"AVG WIN":>8} | {"AVG LOSS":>9} | {"R/R":>5} | {"GROSS P&L":>10} | {"GROSS R":>8}')
    row = (f'{strategy_name:<26} {len(trades):>6} | {win_days:>8}  {loss_days:>9} | '
           f'{win_cnt:>5}  {los_cnt:>6} | {win_pct:>5.1f}% | {max_cw:>6}  {max_cl:>6} | '
           f'{avg_win:>+8.2f} | {avg_loss:>+9.2f} | {rr_ratio:>5.2f} | '
           f'{gross_pnl:>+10.2f} | {gross_r:>+8.2f}')
    print(f'\n{sep}')
    print(hdr)
    print(row)
    print(f'{sep}\n')


RESULTS_PATH = os.path.join(ROOT, 'dashboard', 'backtest_results.json')


def write_backtest_json(trades: list[dict], near_misses: list[dict] | None = None):
    """Write per-day + per-symbol summary to dashboard/backtest_results.json."""
    day_data: dict[str, dict] = {}
    for t in trades:
        d, sym = t['date'], t['symbol']
        if d not in day_data:
            day_data[d] = {'trades': 0, 'pnl': 0.0, 'symbols': {}, '_trades': []}
        day_data[d]['trades'] += 1
        day_data[d]['pnl'] = round(day_data[d]['pnl'] + t['pnl'], 2)
        syms = day_data[d]['symbols']
        if sym not in syms:
            syms[sym] = {'trades': 0, 'pnl': 0.0}
        syms[sym]['trades'] += 1
        syms[sym]['pnl'] = round(syms[sym]['pnl'] + t['pnl'], 2)
        day_data[d]['_trades'].append({'t': t['exit_time'].strftime('%H:%M'), 'pnl': t['pnl']})
        day_data[d].setdefault('sim_trades', []).append({
            'symbol':      sym,
            'entry_time':  t['time'].strftime('%H:%M'),
            'entry_price': t['entry_price'],
            'exit_time':   t['exit_time'].strftime('%H:%M'),
            'exit_price':  t['exit_price'],
            'pnl':         t['pnl'],
        })

    for nm in (near_misses or []):
        d, sym = nm['date'], nm['symbol']
        if d not in day_data:
            day_data[d] = {'trades': 0, 'pnl': 0.0, 'symbols': {}, '_trades': []}
        day_data[d].setdefault('near_misses', []).append({
            'symbol':         sym,
            'time':           nm['time'],
            'breakout_level': nm['breakout_level'],
            'volume':         nm['volume'],
            'conditions':     nm['conditions'],
        })

    for d, dd in day_data.items():
        raw = sorted(dd.pop('_trades'), key=lambda x: x['t'])
        cum = 0.0
        chart = []
        for pt in raw:
            cum = round(cum + pt['pnl'], 2)
            chart.append({'t': pt['t'], 'pnl': cum})
        dd['chart'] = chart
    with open(RESULTS_PATH, 'w') as f:
        json.dump(day_data, f, separators=(',', ':'))
    print(f'Backtest results  → {RESULTS_PATH}')


def print_rules():
    sep = '─' * 78
    print(sep)
    print('ENTRY CONDITIONS  (2-min fast bars, long only, session open → 16:00 ET)')
    print(sep)
    print(f'1. Bar high ≥ prev 2-min bar high × {1 + BREAKOUT_PCT:.4f}  (breakout trigger)')
    print( '   Entry price = breakout level + slippage')
    print( '2. Bar volume ≥ 25,000')
    print( '3. Current 2-min bar MACD ≥ signal line')
    print( '4. EMA proximity: |prev bar high – EMA9| / max ≤ 7.5%')
    print( '                  OR   |EMA9 – EMA20| / max ≤ 2.5%')
    print( '5. Prev bar high > prev bar EMA9')
    print( '6. Prev bar EMA9 > prev bar VWAP')
    print(f'Fill: breakout level + ${SLIPPAGE:.2f} slippage')
    print()
    print(sep)
    print('EXIT CONDITIONS  (first met wins)')
    print(sep)
    print('1. Bar low ≤ initial_stop  →  hard stop')
    print('2. Bar low ≤ highest_slow_low × 0.99  AND  highest_slow_low × 0.99 > entry_price + 0.25  →  trail_slow')
    print('   highest_slow_low = running max of all fully-closed 30-min bar lows since entry')
    print('   initial_stop = prev 30-min bar low × 0.99')
    print(f'Sizing: shares = floor(${RISK_PER_TRADE:.0f} / risk-per-share)')
    print(sep)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Bar-by-bar intraday backtester')
    parser.add_argument('--symbol',  help='Filter to one symbol')
    parser.add_argument('--date',    help='Filter to one date (YYYY-MM-DD)')
    parser.add_argument('--start',   help='Start of date range, inclusive (YYYY-MM-DD)')
    parser.add_argument('--end',     help='End of date range, inclusive (YYYY-MM-DD)')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch all bars, overwriting cache')
    args = parser.parse_args()

    if not os.path.exists(CALENDAR_PATH):
        sys.exit(f'ERROR: {CALENDAR_PATH} not found.  Run python calendar_data.py first.')

    with open(CALENDAR_PATH) as f:
        cal = json.load(f)

    cache = {} if args.refresh else load_cache()

    client = None
    try:
        import schwabdev
        client = schwabdev.Client(config.SCHWAB_API_KEY, config.SCHWAB_CLIENT_ID)
        print('Schwab client initialised (primary data source).')
    except Exception:
        print('Schwab unavailable — using Massive API.')

    pairs = _iter_pairs(cal, args)
    if not pairs:
        sys.exit('No matching date/symbol pairs found in calendar_data.json.')

    print(f'Running backtest over {len(pairs)} symbol/date pair(s)…\n')

    all_trades: list[dict] = []
    all_misses: list[dict] = []

    for date_str, symbol in pairs:
        print(f'  {date_str}  {symbol:8s}', end='  ', flush=True)

        bars_2  = fetch_bars(symbol, date_str,  2, client, cache)
        bars_30 = fetch_bars(symbol, date_str, 30, client, cache)

        if not bars_30:
            print('(no 30-min data)')
            continue

        warmup_2: list[dict] = []
        for wd in _prior_business_days(date_str, 2):
            warmup_2 += fetch_bars(symbol, wd, 2, client, cache)

        df_30 = bars_to_df(bars_30)
        _, inject_time = _get_first_entry(cal, date_str, symbol)

        ta: list[dict] = []
        misses: list[dict] = []

        if bars_2:
            df_2  = bars_to_df(bars_2)
            df_wu2 = bars_to_df(warmup_2) if warmup_2 else None
            ta, misses = run_strategy(
                df_2, df_30, symbol, date_str,
                df_fast_warmup=df_wu2,
                inject_entry_time=inject_time,
                strategy_name=STRATEGY_A_NAME,
            )
            all_trades.extend(ta)
            all_misses.extend(misses)

        print(f'{len(ta)} trades,  {len(misses)} near-miss(es)')

    save_cache(cache)
    print(f'\nCache saved        → {CACHE_PATH}')

    write_backtest_json(all_trades, all_misses)
    print_stats(all_trades, STRATEGY_A_NAME)
    print_rules()


if __name__ == '__main__':
    main()
