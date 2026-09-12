"""
backtesting.py — Backtest a breakout strategy on historically-traded symbols.

ENTRY CONDITIONS  (2-min bars, long only, session open -> 16:00 ET)
  1. Bar high >= prev 2-min bar high x 1.01              (breakout trigger, fill at trigger)
  2. Bar volume >= 25,000
  3. 2-min MACD > signal line  OR  9 EMA > 20 EMA
  4. EMA proximity: |prev_high - EMA9| / max <= 7.5%
                 OR |EMA9 - EMA20| / max <= 2.5%
  5. Prev bar high > prev bar EMA9
  6. Prev bar EMA9 > prev bar VWAP
  Fill: first 5-second sub-bar that crosses the trigger (max(trigger, that sub-bar's open))
  No new entry on the same 1-min bar as an exit — the entry fill-scan can't tell which
  sub-bars came after the exit within that bar, so it could otherwise fill a "new" position
  on a tick at or before the one that closed the old one (verified against raw tick data:
  every same-bar case sampled had entry_ts <= exit_ts, which can't happen in real trading).

EXIT CONDITIONS  (first hit wins except partial TPs)
  1. Bar low <= most recently completed 2-min bar low (as of entry) x 0.99    hard stop (fixed)
  2. Bar low < highest 2-min bar low since entry x 0.99                       trail stop
     (activates only when bar open >= entry + $0.15)
#  3. Partial TPs: 1/2 at 1R, then 1/4 of remainder at each R
  4. EOD: close at last bar

SIZING  shares = floor($5 / risk-per-share)
        risk-per-share = entry - (prior_2min_bar_low x 0.99)

Reads 5-second OHLCV bars from tick_data/ (built from raw Alpaca trade prints by
fetch_ticks.py — run that first), not the 1-min dashboard/ohlcv/ cache. Sub-bar granularity
lets stop-vs-target ordering within a strategy bar resolve by real time order instead of
guessing off the bar's aggregate high/low.

Usage:
    python fetch_ticks.py                   # pre-fetch tick_data/ (run once, or after new trades)
    python backtesting.py                   # runs, writes dashboard/backtest/backtest_trades_YYYY.json + backtest_index.json, FTP uploads
    python backtesting.py --from-date 2025-09-01
    python backtesting.py --symbol SIDU
    python backtesting.py --no-export       # skip writing the JSON export
    python backtesting.py --no-upload       # export but skip the FTP upload
"""

import bisect
import json
import os
import math
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict, OrderedDict

_ET       = ZoneInfo('America/New_York')
_HERE     = os.path.dirname(__file__)
CAL_DIR   = os.path.join(_HERE, 'dashboard', 'calendar')
TICK_DIR  = os.path.join(_HERE, 'tick_data')

# Re-pull today's tick data from Alpaca on every run (today's session is still filling in
# intraday) instead of trusting whatever's already cached in tick_data/. Set False once
# today's session has closed, or to skip the extra fetch and just use the cache as-is.
REFETCH_TODAY_TICKS = False

# ╔══════════════════════════════════════════════════════════════╗
# ║                  STRATEGY PARAMETERS                        ║
# ╠══════════════════════════════════════════════════════════════╣
# ║  Edit these values to tune the strategy.                    ║
# ╚══════════════════════════════════════════════════════════════╝

# ── Backtest date range ───────────────────────────────────────
DEFAULT_FROM_DATE  = '2025-09-01'   # YYYY-MM-DD; override with --from-date

# ── Sizing ────────────────────────────────────────────────────
RISK_PER_TRADE     = 10              # dollars risked per trade

# ── Entry ─────────────────────────────────────────────────────
ENTRY_MULT         = 1.01           # breakout trigger: bar high > prev high * this
FILL_SLIP          = 0.00           # slippage added to fill price
MIN_VOLUME         = 25_000         # bar volume must be >= this
EMA_FAST           = 9              # fast EMA period (2-min bars)
EMA_SLOW           = 20             # slow EMA period (2-min bars)
MACD_FAST          = 12             # MACD fast EMA
MACD_SLOW          = 26             # MACD slow EMA
MACD_SIG           = 9              # MACD signal EMA
EMA_PROX_HIGH      = 0.10          # |prev_high - EMA9| / max <= this  (proximity A) .10
EMA_PROX_CROSS     = 0.03         # |EMA9 - EMA20| / max    <= this   (proximity B) .03
# ── Exit ──────────────────────────────────────────────────────
STOP_MULT          = 0.99           # stop = prev 2-min bar low * this
                                     # trail activates once (highest low since entry x STOP_MULT) > entry


# ── indicator helpers ─────────────────────────────────────────────────────────

def calc_ema(values, period):
    """EMA aligned to input list; None until enough non-None values."""
    result = [None] * len(values)
    k = 2 / (period + 1)
    prev_ema = None
    count = 0
    sma_acc = 0.0
    for i, v in enumerate(values):
        if v is None:
            continue
        count += 1
        sma_acc += v
        if count < period:
            continue
        if count == period:
            prev_ema = sma_acc / period
            result[i] = prev_ema
            continue
        prev_ema = v * k + prev_ema * (1 - k)
        result[i] = prev_ema
    return result


def calc_macd(closes):
    ema_f  = calc_ema(closes, MACD_FAST)
    ema_s  = calc_ema(closes, MACD_SLOW)
    macd   = [f - s if f is not None and s is not None else None
              for f, s in zip(ema_f, ema_s)]
    signal = calc_ema(macd, MACD_SIG)
    return macd, signal


# ── bar aggregation ───────────────────────────────────────────────────────────

def _slot(ts, minutes):
    dt = datetime.fromtimestamp(ts, tz=_ET)
    return (dt.hour * 60 + dt.minute) // minutes


def _aggregate(bars_1min, minutes, with_vwap=False):
    bars = sorted(bars_1min, key=lambda b: b['time'])
    cum_tv = cum_v = 0.0
    slots  = OrderedDict()
    for b in bars:
        s   = _slot(b['time'], minutes)
        vol = b.get('volume', 1) or 1
        if with_vwap:
            cum_tv += (b['high'] + b['low'] + b['close']) / 3 * vol
            cum_v  += vol
            vwap    = cum_tv / cum_v
        if s not in slots:
            entry = dict(slot=s, time=b['time'],
                         open=b['open'], high=b['high'],
                         low=b['low'],   close=b['close'],
                         volume=vol)
            if with_vwap:
                entry['vwap'] = vwap
            slots[s] = entry
        else:
            e = slots[s]
            e['high']   = max(e['high'], b['high'])
            e['low']    = min(e['low'],  b['low'])
            e['close']  = b['close']
            e['volume'] += vol
            if with_vwap:
                e['vwap'] = vwap
    return list(slots.values())


def _group_by_slot(fine_bars, minutes):
    """Map each `minutes`-bucket slot -> time-ordered list of the finer-grained input
    bars (e.g. 5-second bars) that make it up. Used to replay a strategy bar's own
    sub-bars in real time order when resolving stop-vs-target ordering."""
    groups = defaultdict(list)
    for b in sorted(fine_bars, key=lambda b: b['time']):
        groups[_slot(b['time'], minutes)].append(b)
    return groups


def ts_to_hhmm(ts):
    return datetime.fromtimestamp(ts, tz=_ET).strftime('%H:%M')


def premarket_high(bars_1min):
    """Max high across 1-min bars strictly before 09:30 ET, or None if no premarket bars."""
    high = None
    for b in bars_1min:
        dt = datetime.fromtimestamp(b['time'], tz=_ET)
        if dt.hour * 60 + dt.minute < 570:
            if high is None or b['high'] > high:
                high = b['high']
    return high


def build_prior_bar_field_lookup(ref_bars, window_seconds, field='low'):
    """Returns fn(ts) -> field value of the most recently *completed* ref-timeframe
    bar strictly before ts, or None if none have completed yet."""
    times = [b['time'] for b in ref_bars]

    def lookup(ts):
        idx = bisect.bisect_right(times, ts - window_seconds) - 1
        return ref_bars[idx][field] if idx >= 0 else None

    return lookup


def build_prior_series_lookup(ref_bars, window_seconds, series):
    """Same as build_prior_bar_field_lookup but for a value series (e.g. a MACD array)
    aligned index-for-index with ref_bars, rather than a field on the bar dict itself."""
    times = [b['time'] for b in ref_bars]

    def lookup(ts):
        idx = bisect.bisect_right(times, ts - window_seconds) - 1
        return series[idx] if idx >= 0 else None

    return lookup


def find_pullback_trigger(bars, ema9, ema20, i, peak_lag, impulse_lookback, impulse_pct,
                           pullback_min, pullback_max, depth_tol, struct_tol):
    """
    Look backward from bar i for the nearest confirmed local peak that ends a
    qualifying impulse leg, followed by a valid shallow pullback. Returns the
    breakout trigger price (pullback's own high x ENTRY_MULT), or None.

    A peak at bar j is "confirmed" once peak_lag bars have closed after it without
    exceeding its high — that lag is what keeps this look-ahead safe (bar i must be
    strictly after j + peak_lag).
    """
    lo = max(impulse_lookback, i - 1 - peak_lag - pullback_max)
    hi = i - 1 - peak_lag
    for j in range(hi, lo - 1, -1):
        if j < impulse_lookback or j + peak_lag >= i:
            continue
        window = bars[j:j + peak_lag + 1]
        if bars[j]['high'] != max(b['high'] for b in window):
            continue  # not a confirmed local peak

        lookback_low = min(b['low'] for b in bars[j - impulse_lookback:j + 1])
        if lookback_low <= 0 or (bars[j]['high'] - lookback_low) / lookback_low < impulse_pct:
            continue  # no qualifying impulse leg into this peak

        pullback = bars[j + 1:i]
        if not (pullback_min <= len(pullback) <= pullback_max):
            continue
        if any(b['high'] > bars[j]['high'] for b in pullback):
            continue  # price already broke back above the peak — not a pullback, still trending

        idxs = range(j + 1, i)
        if any(ema9[k] is None or ema20[k] is None for k in idxs):
            continue

        low_idx = min(idxs, key=lambda k: bars[k]['low'])
        if ema9[low_idx] < ema20[low_idx] * struct_tol:
            continue  # trend structure broke at the pullback low
        if bars[low_idx]['low'] < ema20[low_idx] * depth_tol:
            continue  # pullback undercut the 20EMA by more than the allowed tolerance

        pullback_high = max(b['high'] for b in pullback)
        return pullback_high * ENTRY_MULT
    return None


# ── core backtest ─────────────────────────────────────────────────────────────

def backtest_symbol_day(symbol, date_str, bars_1min, first_trade_hhmm, bar_minutes=2, stop_minutes=2,
                         partial_tp=False, first_tp_r=1, partial_tp_step_dollar=None,
                         full_tp_r=None, full_tp_dollar=None,
                         require_above_vwap=False, new_high_bars=None, extra_trigger_minutes=None,
                         trail_minutes=None, pullback_mode=False, peak_lag=2, impulse_lookback=15,
                         impulse_pct=0.03, pullback_min=1, pullback_max=25, depth_tol=0.995,
                         struct_tol=0.98, momentum_filter=None, require_lower_high_low=False,
                         momentum_extra_minutes=2, exit_macd_filter=None, exit_extra_minutes=2,
                         min_risk_per_share=None, entry_cutoff_hhmm=None,
                         partial_tp_bar_close=False, half_exit_breakdown=False,
                         require_above_pm_high=False, partial_tp_break_pct=None,
                         block_reentry_same_bar=False,
                         min_volume=None, entry_mult=None,
                         require_ema_proximity=True, ema_prox_high=None, ema_prox_cross=None,
                         require_high_above_ema9=True, require_ema9_above_vwap=True):
    min_volume     = MIN_VOLUME     if min_volume     is None else min_volume
    entry_mult     = ENTRY_MULT     if entry_mult     is None else entry_mult
    ema_prox_high  = EMA_PROX_HIGH  if ema_prox_high  is None else ema_prox_high
    ema_prox_cross = EMA_PROX_CROSS if ema_prox_cross is None else ema_prox_cross

    bars     = _aggregate(bars_1min, bar_minutes, with_vwap=True)
    sub_bars = _group_by_slot(bars_1min, bar_minutes)

    pm_high = premarket_high(bars_1min) if require_above_pm_high else None

    if momentum_filter == 'macd_rising':
        min_bars = MACD_SLOW + MACD_SIG + 4
    elif momentum_filter in ('macd', 'macd_dual') or exit_macd_filter in ('1min', 'either'):
        min_bars = MACD_SLOW + MACD_SIG + 3
    else:
        min_bars = EMA_SLOW + 2
    if len(bars) < min_bars:
        return []

    stop_bars      = bars if stop_minutes == bar_minutes else _aggregate(bars_1min, stop_minutes, with_vwap=False)
    prior_stop_low = build_prior_bar_field_lookup(stop_bars, stop_minutes * 60, field='low')

    # (optional) OR-trigger: also accept a breakout off the previous extra_trigger_minutes-bar
    # high — whichever threshold (native bar vs. extra timeframe) is lower effectively fires first.
    prior_extra_high = None
    if extra_trigger_minutes is not None:
        extra_bars = bars if extra_trigger_minutes == bar_minutes \
                     else _aggregate(bars_1min, extra_trigger_minutes, with_vwap=False)
        prior_extra_high = build_prior_bar_field_lookup(extra_bars, extra_trigger_minutes * 60, field='high')

    # (optional) trail stop reference on a different timeframe than bar_minutes — running max of
    # completed trail_minutes-bar lows since entry, instead of the native bar's low each iteration.
    prior_trail_low = None
    if trail_minutes is not None and trail_minutes != bar_minutes:
        trail_bars = stop_bars if trail_minutes == stop_minutes else _aggregate(bars_1min, trail_minutes, with_vwap=False)
        prior_trail_low = build_prior_bar_field_lookup(trail_bars, trail_minutes * 60, field='low')

    # --- indicator arrays on the strategy bars ---
    closes = [b['close'] for b in bars]
    ema9   = calc_ema(closes, EMA_FAST)
    ema20  = calc_ema(closes, EMA_SLOW)
    if momentum_filter in ('macd', 'macd_rising', 'macd_dual') or exit_macd_filter in ('1min', 'either'):
        macd, sig = calc_macd(closes)

    # (optional) second-timeframe MACD/signal — for the dual-timeframe entry OR filter, and/or
    # the 2-min exit filter — most recently completed *_extra_minutes-bar's MACD/signal/low
    # strictly before the current bar's time.
    prior_macd2 = prior_sig2 = None
    if momentum_filter == 'macd_dual':
        macd2_bars = bars if momentum_extra_minutes == bar_minutes \
                     else _aggregate(bars_1min, momentum_extra_minutes, with_vwap=False)
        closes2      = [b['close'] for b in macd2_bars]
        macd2, sig2  = calc_macd(closes2)
        prior_macd2  = build_prior_series_lookup(macd2_bars, momentum_extra_minutes * 60, macd2)
        prior_sig2   = build_prior_series_lookup(macd2_bars, momentum_extra_minutes * 60, sig2)

    prior_exit_macd2 = prior_exit_sig2 = prior_exit_low2 = None
    if exit_macd_filter in ('extra', 'either'):
        exit2_bars = bars if exit_extra_minutes == bar_minutes \
                     else _aggregate(bars_1min, exit_extra_minutes, with_vwap=False)
        exit_closes2     = [b['close'] for b in exit2_bars]
        exit_macd2, exit_sig2 = calc_macd(exit_closes2)
        prior_exit_macd2 = build_prior_series_lookup(exit2_bars, exit_extra_minutes * 60, exit_macd2)
        prior_exit_sig2  = build_prior_series_lookup(exit2_bars, exit_extra_minutes * 60, exit_sig2)
        prior_exit_low2  = build_prior_bar_field_lookup(exit2_bars, exit_extra_minutes * 60, field='low')

    start_slot = (lambda h, m: (h * 60 + m) // bar_minutes)(
        *map(int, first_trade_hhmm.split(':')))

    cutoff_slot = None
    if entry_cutoff_hhmm is not None:
        cutoff_slot = (lambda h, m: (h * 60 + m) // bar_minutes)(
            *map(int, entry_cutoff_hhmm.split(':')))

    open_positions = []
    trades         = []
    last_exit_i    = None

    for i in range(1, len(bars)):
        cur  = bars[i]
        prev = bars[i - 1]
        had_position = bool(open_positions)

        # ── update open positions ─────────────────────────────────────────
        still_open = []
        for pos in open_positions:
            # update highest bar low seen since entry
            if prior_trail_low is None:
                if prev['low'] > pos['best_bar_low']:
                    pos['best_bar_low'] = prev['low']
            else:
                ref_low = prior_trail_low(cur['time'])
                if ref_low is not None and ref_low > pos['best_bar_low']:
                    pos['best_bar_low'] = ref_low

            # track peak favorable excursion (analytics only — not used in exit decisions)
            if cur['high'] > pos['max_high']:
                pos['max_high'] = cur['high']

            hard_stop     = pos['stop']
            trail_trigger = pos['best_bar_low'] * STOP_MULT
            trail_stop    = trail_trigger if trail_trigger > pos['entry'] else None

            effective_stop = hard_stop
            stop_reason    = 'stop'
            if trail_stop is not None and trail_stop > hard_stop:
                effective_stop = trail_stop
                stop_reason    = 'trail'

            # Replay this strategy bar's own finer sub-bars (e.g. 5-second bars) in real
            # time order, so a stop and a target that both fall inside the same 1-2 minute
            # bar resolve by whichever actually happened first, instead of guessing off the
            # bar's aggregate high/low. Falls back to the bar itself if no finer data is
            # available for this slot.
            closed = False
            for sb in sub_bars.get(cur['slot']) or [cur]:

                # stop hits remaining shares
                if sb['low'] <= effective_stop:
                    exit_px = effective_stop
                    pnl     = pos['realized_pnl'] + (exit_px - pos['entry']) * pos['shares_rem']
                    trades.append({**pos,
                                    'exit': round(exit_px, 4),
                                    'exit_reason': stop_reason,
                                    'exit_time': ts_to_hhmm(sb['time']),
                                    'exit_ts': sb['time'],
                                    'pnl': round(pnl, 2),
                                    'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                                    'exit_r': round((exit_px - pos['entry']) / pos['R'], 4)})
                    closed = True
                    break

                # (optional) half-size derisk: fires once per trade, the first time a
                # sub-bar's low breaks below BOTH avg cost (entry) and the prior bar's low.
                # "Prior bar" excludes the entry bar itself (i - 1 == entry_i) since
                # comparing against the candle the trade was triggered off of isn't a real
                # breakdown signal. Sells half of whatever shares remain at the breached
                # level; the other half stays subject to the normal stop/trail/TP/EOD exits.
                if half_exit_breakdown and not pos['half_exit_done'] and i - 1 != pos['entry_i']:
                    breakdown_px = min(pos['entry'], prev['low'])
                    if sb['low'] < breakdown_px:
                        pos['half_exit_done'] = True
                        sell = min(pos['shares_rem'], max(1, math.floor(pos['shares_rem'] * 0.5)))
                        pos['realized_pnl'] += (breakdown_px - pos['entry']) * sell
                        pos['shares_rem']   -= sell
                        pos['partials'].append({'time': ts_to_hhmm(sb['time']), 'ts': sb['time'],
                                                 'price': round(breakdown_px, 4), 'shares': sell})
                        if pos['shares_rem'] == 0:
                            trades.append({**pos,
                                            'exit': round(breakdown_px, 4),
                                            'exit_reason': 'half_exit',
                                            'exit_time': ts_to_hhmm(sb['time']),
                                            'exit_ts': sb['time'],
                                            'pnl': round(pos['realized_pnl'], 2),
                                            'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                                            'exit_r': round((breakdown_px - pos['entry']) / pos['R'], 4)})
                            closed = True
                            break

                # (optional) momentum exit: MACD < signal on the fully-closed prior bar AND
                # price breaks below that same-timeframe prior bar's low. Exits at the
                # breached low level.
                if exit_macd_filter is not None:
                    exit_px = None
                    if exit_macd_filter in ('1min', 'either'):
                        macd_p1, sig_p1 = macd[i - 1], sig[i - 1]
                        if macd_p1 is not None and sig_p1 is not None and macd_p1 < sig_p1 \
                                and sb['low'] < prev['low']:
                            exit_px = prev['low']
                    if exit_px is None and exit_macd_filter in ('extra', 'either'):
                        m2  = prior_exit_macd2(cur['time'])
                        s2  = prior_exit_sig2(cur['time'])
                        lo2 = prior_exit_low2(cur['time'])
                        if m2 is not None and s2 is not None and lo2 is not None and m2 < s2 \
                                and sb['low'] < lo2:
                            exit_px = lo2
                    if exit_px is not None:
                        pnl = pos['realized_pnl'] + (exit_px - pos['entry']) * pos['shares_rem']
                        trades.append({**pos,
                                        'exit': round(exit_px, 4),
                                        'exit_reason': 'macd_exit',
                                        'exit_time': ts_to_hhmm(sb['time']),
                                        'exit_ts': sb['time'],
                                        'pnl': round(pnl, 2),
                                        'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                                        'exit_r': round((exit_px - pos['entry']) / pos['R'], 4)})
                        closed = True
                        break

                # full-size exit once price hits the target (whichever threshold is closer to entry)
                if pos['full_tp'] is not None and sb['high'] >= pos['full_tp']:
                    exit_px = pos['full_tp']
                    pnl     = pos['realized_pnl'] + (exit_px - pos['entry']) * pos['shares_rem']
                    trades.append({**pos,
                                    'exit': round(exit_px, 4),
                                    'exit_reason': 'tp',
                                    'exit_time': ts_to_hhmm(sb['time']),
                                    'exit_ts': sb['time'],
                                    'pnl': round(pnl, 2),
                                    'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                                    'exit_r': round((exit_px - pos['entry']) / pos['R'], 4)})
                    closed = True
                    break

                # (optional) partial exit on a break of the previous bar's high by
                # partial_tp_break_pct or more — sells 25% of remaining shares, at most once per
                # bar, once the break_partial_active_i gate (set at entry) has been reached.
                if partial_tp_break_pct is not None and i >= pos['break_partial_active_i'] \
                        and pos['break_partial_last_i'] != i:
                    break_px = prev['high'] * (1 + partial_tp_break_pct)
                    if sb['high'] >= break_px:
                        pos['break_partial_last_i'] = i
                        sell = min(pos['shares_rem'], max(1, math.floor(pos['shares_rem'] * 0.25)))
                        pos['realized_pnl'] += (break_px - pos['entry']) * sell
                        pos['shares_rem']   -= sell
                        pos['tp_count']     += 1
                        pos['partials'].append({'time': ts_to_hhmm(sb['time']), 'ts': sb['time'],
                                                 'price': round(break_px, 4), 'shares': sell})

                        if pos['shares_rem'] == 0:
                            trades.append({**pos,
                                           'exit': round(break_px, 4),
                                           'exit_reason': 'tp',
                                           'exit_time': ts_to_hhmm(sb['time']),
                                           'exit_ts': sb['time'],
                                           'pnl': round(pos['realized_pnl'], 2),
                                           'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                                           'exit_r': round((break_px - pos['entry']) / pos['R'], 4)})
                            closed = True
                            break

                # partial TP exits: sell 25% of remaining shares at each step (R-multiple, or a
                # fixed $ step when partial_tp_step_dollar is set)
                if partial_tp:
                    step = partial_tp_step_dollar if partial_tp_step_dollar is not None else pos['R']
                    last_tp_px = pos['entry']   # track last exit price for final record
                    while sb['high'] >= pos['next_tp'] and pos['shares_rem'] > 0:
                        tp_px      = pos['next_tp']
                        sell       = min(pos['shares_rem'], max(1, math.floor(pos['shares_rem'] * 0.25)))
                        pos['realized_pnl'] += (tp_px - pos['entry']) * sell
                        pos['shares_rem']   -= sell
                        pos['next_tp']       = round(pos['next_tp'] + step, 4)
                        pos['tp_count']     += 1
                        last_tp_px           = tp_px
                        pos['partials'].append({'time': ts_to_hhmm(sb['time']), 'ts': sb['time'],
                                                 'price': round(tp_px, 4), 'shares': sell})

                        if pos['shares_rem'] == 0:
                            trades.append({**pos,
                                           'exit': round(last_tp_px, 4),
                                           'exit_reason': 'tp',
                                           'exit_time': ts_to_hhmm(sb['time']),
                                           'exit_ts': sb['time'],
                                           'pnl': round(pos['realized_pnl'], 2),
                                           'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                                           'exit_r': round((last_tp_px - pos['entry']) / pos['R'], 4)})
                            break
                    if pos['shares_rem'] == 0:
                        closed = True
                        break

            if closed:
                continue

            # partial TP: sell 25% of remaining shares whenever this bar closes above avg cost.
            # The decision isn't knowable until the bar's last tick prints, so the fill is dated
            # to that last sub-bar, not cur['time'] (which _aggregate sets to the bar's *open*
            # tick — using it here would date this fill before ticks earlier in the same bar).
            if partial_tp_bar_close and cur['close'] > pos['entry'] and pos['shares_rem'] > 0:
                tp_px = cur['close']
                bar_close_ts = (sub_bars.get(cur['slot']) or [cur])[-1]['time']
                sell  = min(pos['shares_rem'], max(1, math.floor(pos['shares_rem'] * 0.25)))
                pos['realized_pnl'] += (tp_px - pos['entry']) * sell
                pos['shares_rem']   -= sell
                pos['tp_count']     += 1
                pos['partials'].append({'time': ts_to_hhmm(bar_close_ts), 'ts': bar_close_ts,
                                         'price': round(tp_px, 4), 'shares': sell})

                if pos['shares_rem'] == 0:
                    trades.append({**pos,
                                   'exit': round(tp_px, 4),
                                   'exit_reason': 'tp',
                                   'exit_time': ts_to_hhmm(bar_close_ts),
                                   'exit_ts': bar_close_ts,
                                   'pnl': round(pos['realized_pnl'], 2),
                                   'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                                   'exit_r': round((tp_px - pos['entry']) / pos['R'], 4)})

            if pos['shares_rem'] > 0:
                still_open.append(pos)
        open_positions = still_open

        if block_reentry_same_bar and had_position and not open_positions:
            last_exit_i = i

        # ── look for new entry ────────────────────────────────────────────
        if cur['slot'] < start_slot:
            continue

        if cutoff_slot is not None and cur['slot'] >= cutoff_slot:
            continue

        if open_positions:          # only 1 position at a time
            continue

        if block_reentry_same_bar and last_exit_i == i:
            continue

        e9_prev  = ema9[i - 1]
        e20_prev = ema20[i - 1]
        if any(v is None for v in [e9_prev, e20_prev]):
            continue

        if pullback_mode:
            trigger = find_pullback_trigger(bars, ema9, ema20, i, peak_lag, impulse_lookback,
                                             impulse_pct, pullback_min, pullback_max,
                                             depth_tol, struct_tol)
            if trigger is None:
                continue
        else:
            trigger = prev['high'] * entry_mult
            if prior_extra_high is not None:
                extra_high = prior_extra_high(cur['time'])
                if extra_high is not None:
                    trigger = min(trigger, extra_high * entry_mult)
        # 1. breakout
        if cur['high'] < trigger:
            continue

        # 1+2. breakout + volume, evaluated tick-by-tick (per 5-second sub-bar), not once per
        # bar close: find the first sub-bar where price has actually crossed the trigger AND
        # cumulative volume from this bar's open through that sub-bar already clears
        # MIN_VOLUME — same no-look-ahead discipline as the rest of this engine. A bar whose
        # total volume only clears 25k *after* price already dropped back below the trigger,
        # or whose crossing tick doesn't yet have 25k behind it, must not fire. Fill price:
        # the first tick that satisfies both, catching gap-through slippage on fast breakouts.
        subs = sub_bars.get(cur['slot']) or []
        fill_sub = None
        cum_vol = 0
        for sb in subs:
            cum_vol += sb['volume']
            if sb['high'] >= trigger and cum_vol >= min_volume:
                fill_sub = sb
                break

        if subs and fill_sub is None:
            continue   # no tick this bar had trigger + volume threshold together
        if not subs and cur['volume'] < min_volume:
            continue   # no sub-bar data available — fall back to the bar-level volume check

        entry_px = (max(trigger, fill_sub['open']) if fill_sub is not None else trigger) + FILL_SLIP

        # 1b. (optional) fill price itself must clear the fresh new_high_bars-bar high — filters
        # backside/chop entries. Compares entry_px (known at the trigger moment), not cur['high']
        # (not fully known until the bar closes, which would be look-ahead).
        if new_high_bars is not None:
            lookback = bars[max(0, i - new_high_bars):i]
            if not lookback or entry_px < max(b['high'] for b in lookback):
                continue

        # 2b. (optional) momentum filter — reads only the fully-closed previous bar (i-1),
        # never the current bar's own close-derived indicators (that would be look-ahead).
        if momentum_filter == 'macd':
            macd_p1, sig_p1 = macd[i - 1], sig[i - 1]
            if macd_p1 is None or sig_p1 is None or macd_p1 <= sig_p1:
                continue
        elif momentum_filter == 'macd_rising':
            # both readings must come from fully-closed bars (i-1, i-2) — using the current,
            # still-forming bar's own close-derived MACD would be look-ahead.
            if i < 2:
                continue
            macd_p1, sig_p1 = macd[i - 1], sig[i - 1]
            macd_p2, sig_p2 = macd[i - 2], sig[i - 2]
            if None in (macd_p1, sig_p1, macd_p2, sig_p2):
                continue
            if (macd_p1 - sig_p1) <= (macd_p2 - sig_p2):
                continue  # histogram must be rising, even if MACD is still below signal
        elif momentum_filter == 'ema':
            if e9_prev <= e20_prev:
                continue
        elif momentum_filter == 'macd_dual':
            # native (1-min) prior bar — already look-ahead safe
            macd_p1, sig_p1 = macd[i - 1], sig[i - 1]
            ok_native = macd_p1 is not None and sig_p1 is not None and macd_p1 > sig_p1
            # most recently completed momentum_extra_minutes-bar strictly before cur's time
            m2, s2 = prior_macd2(cur['time']), prior_sig2(cur['time'])
            ok_extra = m2 is not None and s2 is not None and m2 > s2
            if not (ok_native or ok_extra):
                continue

        # 3. (optional) EMA proximity (both conditions)
        if require_ema_proximity:
            prox_high  = abs(prev['high'] - e9_prev) / max(prev['high'], e9_prev)
            prox_cross = abs(e9_prev - e20_prev)     / max(e9_prev, e20_prev)
            if prox_high > ema_prox_high or prox_cross > ema_prox_cross:
                continue

        # 4. (optional) prev bar high > prev bar EMA9
        if require_high_above_ema9 and prev['high'] <= e9_prev:
            continue

        # 5. (optional) prev bar EMA9 > prev bar VWAP
        if require_ema9_above_vwap and e9_prev <= prev['vwap']:
            continue

        # 6. (optional) entry price must not be below VWAP
        if require_above_vwap and entry_px < cur['vwap']:
            continue

        # 7. (optional) prev bar must be a down bar vs the bar before it — both its high and
        # low lower — i.e. a one-bar pullback/pause right before the breakout. Uses only i-1
        # and i-2, both fully closed.
        if require_lower_high_low:
            if i < 2:
                continue
            prev2 = bars[i - 2]
            if not (prev['high'] < prev2['high'] and prev['low'] < prev2['low']):
                continue

        # 8. (optional) entry price must clear the day's premarket high (bars before 09:30 ET).
        # No premarket bars at all (pm_high is None) disqualifies the entry rather than passing
        # it through unfiltered.
        if require_above_pm_high and (pm_high is None or entry_px <= pm_high):
            continue

        # stop is based on the most recently completed stop-timeframe bar's low
        stop_ref_low = prior_stop_low(cur['time'])
        if stop_ref_low is None:
            continue
        stop_px = stop_ref_low * STOP_MULT

        # no fill if stop fires same bar
        if cur['low'] <= stop_px:
            continue

        risk_per_share = entry_px - stop_px
        if risk_per_share <= 0:
            continue

        # (optional) skip tight-stop setups — a small risk-per-share forces a huge share count
        # to hit the fixed $ risk budget, making the trade dominated by tick noise and slippage.
        if min_risk_per_share is not None and risk_per_share < min_risk_per_share:
            continue

        shares = math.floor(RISK_PER_TRADE / risk_per_share)
        if shares < 1:
            continue

        R  = entry_px - stop_px
        tp = entry_px + partial_tp_step_dollar if partial_tp_step_dollar is not None \
             else entry_px + R * first_tp_r

        full_tp_candidates = []
        if full_tp_r is not None:
            full_tp_candidates.append(entry_px + R * full_tp_r)
        if full_tp_dollar is not None:
            full_tp_candidates.append(entry_px + full_tp_dollar)
        full_tp = min(full_tp_candidates) if full_tp_candidates else None

        # (optional) break-of-prior-high partial exit: active from the entry bar itself when
        # the entry price came in below that bar's prior-candle high (room left before the
        # threshold), otherwise deferred to the next bar — an entry that already fired at/above
        # the prior high (the normal breakout case) would trivially re-trigger this off the same
        # move that produced the entry.
        break_partial_active_i = i if entry_px < prev['high'] else i + 1

        open_positions.append(dict(
            date=date_str, symbol=symbol,
            entry=round(entry_px, 4), stop=round(stop_px, 4),
            tp=round(tp, 4), R=round(R, 4), shares=shares,
            entry_time=ts_to_hhmm(cur['time']),
            entry_ts=cur['time'],
            entry_i=i,
            best_bar_low=cur['low'],
            max_high=cur['high'],
            shares_rem=shares,
            next_tp=round(tp, 4),
            tp_count=0,
            realized_pnl=0.0,
            full_tp=round(full_tp, 4) if full_tp is not None else None,
            half_exit_done=False,
            break_partial_active_i=break_partial_active_i,
            break_partial_last_i=-1,
            partials=[],
        ))

    # EOD
    if bars and open_positions:
        last = bars[-1]
        for pos in open_positions:
            pnl = pos['realized_pnl'] + (last['close'] - pos['entry']) * pos['shares_rem']
            trades.append({**pos,
                           'exit': round(last['close'], 4),
                           'exit_reason': 'eod',
                           'exit_time': ts_to_hhmm(last['time']),
                           'exit_ts': last['time'],
                           'pnl': round(pnl, 2),
                           'max_r': round((pos['max_high'] - pos['entry']) / pos['R'], 4),
                           'exit_r': round((last['close'] - pos['entry']) / pos['R'], 4)})

    return trades


# ── data loading ──────────────────────────────────────────────────────────────

def load_tick_day(date_str):
    """5-second OHLCV bars (built from raw trade prints by fetch_ticks.py)."""
    path = os.path.join(TICK_DIR, f'ticks_{date_str}.json')
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


_alpaca_client = None
_alpaca_client_failed = False


def _get_alpaca_client():
    """Lazy, memoized Alpaca client — only ever touched when a fetch is actually needed,
    so a fully-cached run never requires credentials to be configured."""
    global _alpaca_client, _alpaca_client_failed
    if _alpaca_client is not None or _alpaca_client_failed:
        return _alpaca_client
    try:
        from utilities import config
        from utilities.alpaca_client import AlpacaClient
        _alpaca_client = AlpacaClient(config.ALPACA_API_KEY_ID, config.ALPACA_API_SECRET_KEY,
                                       feed=config.ALPACA_DATA_FEED)
    except Exception as e:
        print(f'  [alpaca unavailable] {e} — skipping missing tick data fetch')
        _alpaca_client_failed = True
    return _alpaca_client


INVALID_SYMBOLS_PATH = os.path.join(TICK_DIR, 'invalid_symbols.json')

_invalid_symbols = None


def _load_invalid_symbols():
    """Symbols Alpaca has told us don't exist — loaded once per process, persisted to
    tick_data/invalid_symbols.json so future runs never waste a fetch on them again."""
    global _invalid_symbols
    if _invalid_symbols is None:
        if os.path.exists(INVALID_SYMBOLS_PATH):
            with open(INVALID_SYMBOLS_PATH) as f:
                _invalid_symbols = set(json.load(f))
        else:
            _invalid_symbols = set()
    return _invalid_symbols


def _mark_symbol_invalid(symbol):
    invalid = _load_invalid_symbols()
    invalid.add(symbol)
    os.makedirs(TICK_DIR, exist_ok=True)
    with open(INVALID_SYMBOLS_PATH, 'w') as f:
        json.dump(sorted(invalid), f, indent=2)


def fetch_missing_tick_symbol(date_str, symbol, day_cache):
    """Fetch + bucket one symbol's tick data via Alpaca, merge it into day_cache (the same
    dict object held in tick_cache[date_str], so other symbols already cached for this date
    aren't lost), and persist the whole day back to tick_data/. Returns the bucketed bars,
    or [] if the fetch wasn't possible (including symbols already known-invalid)."""
    from fetch_ticks import bucket_trades, BUCKET_SECONDS, day_path

    if symbol in _load_invalid_symbols():
        return []

    client = _get_alpaca_client()
    if client is None:
        return []
    try:
        trades = client.get_trades(symbol, date_str)
    except Exception as e:
        if 'invalid symbol' in str(e).lower() or 'symbol not found' in str(e).lower():
            _mark_symbol_invalid(symbol)
            print(f'  [invalid symbol] {symbol} — added to {INVALID_SYMBOLS_PATH}, will skip from now on')
        else:
            print(f'  [fetch error] {symbol} {date_str}: {e}')
        return []

    bars = bucket_trades(trades, BUCKET_SECONDS)
    day_cache[symbol] = bars
    os.makedirs(TICK_DIR, exist_ok=True)
    with open(day_path(date_str), 'w') as f:
        json.dump(day_cache, f, separators=(',', ':'))
    print(f'  fetched tick data: {symbol} {date_str} ({len(bars)} bars)')
    return bars


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-date', default=DEFAULT_FROM_DATE)
    parser.add_argument('--symbol',    default=None)
    parser.add_argument('--no-export', action='store_true',
                        help='Skip writing dashboard/backtest/backtest_trades_YYYY.json (exports by default)')
    parser.add_argument('--no-upload', action='store_true',
                        help='Skip the FTP upload step (uploads by default)')
    args = parser.parse_args()
    args.export = not args.no_export
    args.upload = args.export and not args.no_upload

    with open(os.path.join(CAL_DIR, 'calendar_index.json')) as f:
        years = [str(y) for y in json.load(f).get('years', [])]

    opportunities = []
    for yr in years:
        yr_path = os.path.join(CAL_DIR, f'calendar_data_{yr}.json')
        if not os.path.exists(yr_path):
            continue
        with open(yr_path) as f:
            data = json.load(f)
        for year, months in data.items():
            for month, mdata in months.items():
                for day, ddata in (mdata.get('days') or {}).items():
                    rts = ddata.get('roundtrips') or []
                    if not rts:
                        continue
                    date_str = f'{year}-{month.zfill(2)}-{day.zfill(2)}'
                    if date_str < args.from_date:
                        continue
                    sym_first = {}
                    for rt in rts:
                        sym = rt.get('symbol', '')
                        et  = rt.get('entry_time', '')
                        if sym and et:
                            if sym not in sym_first or et < sym_first[sym]:
                                sym_first[sym] = et
                    for sym, first_t in sym_first.items():
                        if args.symbol and sym != args.symbol:
                            continue
                        opportunities.append((date_str, sym, first_t))

    print(f'Symbol-days to scan: {len(opportunities)}')

    A_BAR_MIN         = 1        # entry timeframe (minutes)
    A_STOP_MIN        = 2        # hard-stop reference timeframe (minutes)
    A_EXTRA_MIN       = 2        # extra OR-trigger timeframe (minutes)
    A_TRAIL_MIN       = 2        # trail-stop reference timeframe (minutes)
    A_MOMENTUM_FILTER = 'macd'   # require MACD > signal (beat the 9EMA>20EMA alternative in the sweep)
    A_PARTIAL_TP_BAR_CLOSE = True  # sell 1/4 of remaining shares on any bar close > avg cost
    A_FULL_TP_DOLLAR = 1.00      # full exit once price reaches entry + $1.00 (whichever is closer)

    # Same entry/stop/trail/partial-exit rules throughout; only the full-TP R multiple varies.
    TP_LEVELS  = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    PRIMARY_TP = 2.5   # used for the monthly breakdown / annualized P&L / JSON export below

    # Each row is a labeled variant of Strategy A: an empty override dict runs the strategy
    # exactly as configured above; add a row with overrides to try a different entry criteria
    # combination (min_volume, entry_mult, require_ema_proximity, ema_prox_high/cross,
    # require_high_above_ema9, require_ema9_above_vwap, momentum_filter, ...) alongside it.
    STRATEGY_VARIANTS = [
        ('A', {}),
        ('15-min stop', {'stop_minutes': 15}),
    ]

    all_trades = {label: {r: [] for r in TP_LEVELS} for label, _ in STRATEGY_VARIANTS}
    tick_cache = {}

    today_str = datetime.now().strftime('%Y-%m-%d')

    for date_str, symbol, first_t in opportunities:
        if date_str not in tick_cache:
            if len(tick_cache) >= 10:
                del tick_cache[next(iter(tick_cache))]
            tick_cache[date_str] = load_tick_day(date_str)

        day_cache = tick_cache[date_str]
        bars = day_cache.get(symbol)
        # pull from Alpaca when this symbol/day has never been cached, or when it's today's
        # data and REFETCH_TODAY_TICKS says today's bars are still worth refreshing.
        if bars is None or (date_str == today_str and REFETCH_TODAY_TICKS):
            bars = fetch_missing_tick_symbol(date_str, symbol, day_cache)
        bars = bars or []
        if not bars:
            continue

        for label, variant_kwargs in STRATEGY_VARIANTS:
            for r in TP_LEVELS:
                call_kwargs = {
                    'bar_minutes':            A_BAR_MIN,
                    'stop_minutes':           A_STOP_MIN,
                    'extra_trigger_minutes':  A_EXTRA_MIN,
                    'trail_minutes':          A_TRAIL_MIN,
                    'momentum_filter':        A_MOMENTUM_FILTER,
                    'partial_tp_bar_close':   A_PARTIAL_TP_BAR_CLOSE,
                    'full_tp_r':              r,
                    'full_tp_dollar':         A_FULL_TP_DOLLAR,
                    'block_reentry_same_bar': True,
                    **variant_kwargs,   # per-variant overrides — any of the above, or any other
                                        # backtest_symbol_day entry-criteria kwarg (min_volume,
                                        # entry_mult, require_ema_proximity, ema_prox_high/cross,
                                        # require_high_above_ema9, require_ema9_above_vwap,
                                        # momentum_filter, ...)
                }
                trades = backtest_symbol_day(symbol, date_str, bars, first_t, **call_kwargs)
                all_trades[label][r].extend(trades)

    if not any(all_trades[label].values() for label, _ in STRATEGY_VARIANTS):
        print('No trades generated.')
        return

    # ── stats ─────────────────────────────────────────────────────────────────
    def compute_stats(trades):
        pnls   = [t['pnl'] for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        n      = len(pnls)
        net    = sum(pnls)
        wr     = len(wins) / n * 100 if n else 0
        avg_w  = sum(wins)   / len(wins)   if wins   else 0
        avg_l  = sum(losses) / len(losses) if losses else 0
        rr     = abs(avg_w / avg_l)        if avg_l  else 0

        max_cw = max_cl = cw = cl = 0
        for p in pnls:
            if p > 0:
                cw += 1; cl = 0; max_cw = max(max_cw, cw)
            else:
                cl += 1; cw = 0; max_cl = max(max_cl, cl)

        day_pnl = defaultdict(float)
        for t in trades:
            day_pnl[t['date']] += t['pnl']
        win_days  = sum(1 for v in day_pnl.values() if v > 0)
        loss_days = sum(1 for v in day_pnl.values() if v <= 0)

        # avg trades per calendar month
        dates      = sorted(t['date'] for t in trades)
        from_ym    = (int(dates[0][:4]),  int(dates[0][5:7]))
        to_ym      = (int(dates[-1][:4]), int(dates[-1][5:7]))
        num_months = (to_ym[0] - from_ym[0]) * 12 + (to_ym[1] - from_ym[1]) + 1
        avg_per_mo = n / num_months if num_months > 0 else n

        return dict(n=n, net=net, wr=wr, avg_w=avg_w, avg_l=avg_l, rr=rr,
                    max_cw=max_cw, max_cl=max_cl, win_days=win_days, loss_days=loss_days,
                    num_months=num_months, avg_per_mo=avg_per_mo, W=len(wins), L=len(losses))

    stats = {label: {r: compute_stats(all_trades[label][r]) for r in TP_LEVELS if all_trades[label][r]}
              for label, _ in STRATEGY_VARIANTS}

    if PRIMARY_TP not in stats.get('A', {}):
        print(f'No trades at PRIMARY_TP={PRIMARY_TP:g}R for strategy A '
              f'(entry criteria may be too strict) — other TP levels: {sorted(stats.get("A", {}))}')
        return

    # used further below for the monthly breakdown and annualized P&L (variant 'A', the one
    # that also drives the dashboard JSON export)
    net        = stats['A'][PRIMARY_TP]['net']
    num_months = stats['A'][PRIMARY_TP]['num_months']

    def strategy_name_for(r):
        return (f'{r:g}R  {A_BAR_MIN}min entry / {A_TRAIL_MIN}min trail / {A_STOP_MIN}min stop, '
                f'OR-trigger vs prev {A_BAR_MIN}min or {A_EXTRA_MIN}min high, MACD > signal, '
                f'partial TP on bar close > cost, full TP {r:g}R  ({args.from_date}+)')

    row_labels = {r: f'{r:g}R' for r in TP_LEVELS}
    col = max(max(len(v) for v in row_labels.values()), len('STRATEGY'))

    def format_row(name, s):
        pnl_str = f'{s["net"]:+,.2f}'
        aw_str  = f'+{s["avg_w"]:.2f}'
        al_str  = f'{s["avg_l"]:.2f}'
        apm_str = f'{s["avg_per_mo"]:.1f}'
        return (
            f'  {name:<{col}}  {s["n"]:>6}  {apm_str:>6} | {s["win_days"]:>8}  {s["loss_days"]:>9} | '
            f'{s["W"]:>6}  {s["L"]:>6} | {s["wr"]:>5.1f}% | '
            f'{s["max_cw"]:>6}  {s["max_cl"]:>6} | {aw_str:>8} | {al_str:>8} | {s["rr"]:>4.2f} | {pnl_str:>10}'
        )

    header = (
        f'  {"STRATEGY":<{col}}  {"TRADES":>6}  {"T/MO":>6} | {"WIN DAYS":>8}  {"LOSS DAYS":>9} | '
        f'{"WINS":>6}  {"LOSSES":>6} | {"WIN %":>6} | '
        f'{"MAX CW":>6}  {"MAX CL":>6} | {"AVG WIN":>8} | {"AVG LOSS":>8} | {"R/R":>4} | {"GROSS P&L":>10}'
    )
    divider = '-' * len(header)

    for label, _ in STRATEGY_VARIANTS:
        print()
        print(f'  STRATEGY {label}')
        print(divider)
        print(header)
        print(divider)
        for r in TP_LEVELS:
            if r in stats[label]:
                print(format_row(row_labels[r], stats[label][r]))
        print(divider)

    print()
    print(f'  {"PRIMARY TP (" + f"{PRIMARY_TP:g}R)":<30}  {"TRADES":>6}  {"GROSS P&L":>10}')
    for label, _ in STRATEGY_VARIANTS:
        s = stats[label].get(PRIMARY_TP)
        if s:
            print(f'  {label:<30}  {s["n"]:>6}  {s["net"]:>+10,.2f}')

    # monthly P&L breakdown by session
    def session(hhmm):
        h, m = map(int, hhmm.split(':'))
        mins = h * 60 + m
        if mins < 570:   return 'pre'    # before 09:30
        if mins < 960:   return 'rth'    # 09:30 - 15:59
        return 'ath'                      # 16:00+

    month_pnl = defaultdict(float)
    sess_pnl  = defaultdict(lambda: defaultdict(float))
    for t in all_trades['A'][PRIMARY_TP]:
        ym = t['date'][:7]
        s  = session(t['entry_time'])
        month_pnl[ym]      += t['pnl']
        sess_pnl[ym][s]    += t['pnl']

    col_w = 10
    print()
    print(f'  MONTHLY P&L BY SESSION')
    print(f'  {"MONTH":<7}   {"TOTAL":>{col_w}}   {"PRE":>{col_w}}   {"RTH":>{col_w}}   {"ATH":>{col_w}}')
    print('  ' + '-' * (7 + 4 * (col_w + 3)))
    for ym in sorted(month_pnl):
        v   = round(month_pnl[ym], 2)
        pre = round(sess_pnl[ym].get('pre', 0), 2)
        rth = round(sess_pnl[ym].get('rth', 0), 2)
        ath = round(sess_pnl[ym].get('ath', 0), 2)
        print(f'  {ym:<7}   {v:>+{col_w}.2f}   {pre:>+{col_w}.2f}   {rth:>+{col_w}.2f}   {ath:>+{col_w}.2f}')
    print('  ' + '-' * (7 + 4 * (col_w + 3)))
    tot_pre = round(sum(sess_pnl[ym].get('pre', 0) for ym in month_pnl), 2)
    tot_rth = round(sum(sess_pnl[ym].get('rth', 0) for ym in month_pnl), 2)
    tot_ath = round(sum(sess_pnl[ym].get('ath', 0) for ym in month_pnl), 2)
    print(f'  {"TOTAL":<7}   {net:>+{col_w}.2f}   {tot_pre:>+{col_w}.2f}   {tot_rth:>+{col_w}.2f}   {tot_ath:>+{col_w}.2f}')
    print('  ' + '-' * (7 + 4 * (col_w + 3)))

    ann_pnl = (net / num_months * 12) if num_months > 0 else 0
    print()
    print(f'  ANNUALIZED P&L  ({num_months:.1f} mo)   {ann_pnl:>+,.2f}')

    print()
    print(f'  ENTRY (Long only, {A_BAR_MIN}-min bars, session open -> 16:00 ET)')
    print(f'    1. Bar high >= min(prev {A_BAR_MIN}-min high, prev completed {A_EXTRA_MIN}-min high) x {ENTRY_MULT}  '
          f'(fill: trigger + ${FILL_SLIP:.2f})')
    print(f'    2. Bar volume >= {MIN_VOLUME:,}')
    print(f'    3. {A_BAR_MIN}-min MACD > signal line')
    print(f'    4. |prev {A_BAR_MIN}-min high - {A_BAR_MIN}-min EMA9| / max <= {EMA_PROX_HIGH*100:.1f}%  '
          f'AND  |{A_BAR_MIN}-min EMA9 - {A_BAR_MIN}-min EMA20| / max <= {EMA_PROX_CROSS*100:.1f}%')
    print(f'    5. Prev {A_BAR_MIN}-min bar high > prev {A_BAR_MIN}-min bar EMA9')
    print(f'    6. Prev {A_BAR_MIN}-min bar EMA9 > prev {A_BAR_MIN}-min bar VWAP')
    print()
    print('  EXIT (first hit wins; partial TP checked each bar before stop/EOD close the rest)')
    print(f'    Hard Stop   most recently completed {A_STOP_MIN}-min bar low x {STOP_MULT}  (set at entry, fixed)')
    print(f'    Trail Stop  highest completed {A_TRAIL_MIN}-min low since entry x {STOP_MULT}  (activates once this level > entry)')
    print(f'    Full TP     full exit at entry + <R>R (swept per strategy row above) or entry + ${A_FULL_TP_DOLLAR:.2f}, whichever is closer')
    print( '    Partial TP  sell 1/4 of remaining shares on any bar that closes above avg cost')
    print( '    EOD         close at last bar')
    print()
    print(f'  SIZING  risk=${RISK_PER_TRADE:.0f}  shares=floor(${RISK_PER_TRADE:.0f} / (entry - stop))')
    print()

    # ── optional JSON export ──────────────────────────────────────────────────
    if args.export:
        export_dir = os.path.join(_HERE, 'dashboard', 'backtest')
        os.makedirs(export_dir, exist_ok=True)

        by_year = {}
        for t in all_trades['A'][PRIMARY_TP]:
            rec = {
                'date':        t['date'],
                'symbol':      t['symbol'],
                'entry':       t['entry'],
                'entry_time':  t['entry_time'],
                'entry_ts':    t['entry_ts'],
                'exit':        t['exit'],
                'exit_time':   t.get('exit_time', ''),
                'exit_ts':     t.get('exit_ts', 0),
                'stop':        t['stop'],
                'tp':          t['tp'],
                'R':           t['R'],
                'shares':      t['shares'],
                'pnl':         t['pnl'],
                'exit_reason': t['exit_reason'],
                'partials':    t.get('partials', []),
            }
            by_year.setdefault(t['date'][:4], []).append(rec)

        generated = datetime.now(_ET).strftime('%Y-%m-%d %H:%M')
        total = 0
        for yr, yr_trades in sorted(by_year.items()):
            yr_path = os.path.join(export_dir, f'backtest_trades_{yr}.json')
            with open(yr_path, 'w') as f:
                json.dump({'strategy': strategy_name_for(PRIMARY_TP), 'generated': generated,
                           'trades': yr_trades}, f, separators=(',', ':'))
            print(f'  Exported {len(yr_trades)} trades -> {yr_path}')
            total += len(yr_trades)

        index_path = os.path.join(export_dir, 'backtest_index.json')
        with open(index_path, 'w') as f:
            json.dump({'years': sorted(by_year.keys())}, f, separators=(',', ':'))
        print(f'  Index -> {index_path}  ({total} trades total)')
        print()

        if args.upload:
            import subprocess, sys
            files = [index_path] + [
                os.path.join(export_dir, f'backtest_trades_{yr}.json')
                for yr in sorted(by_year.keys())
            ]
            print('Uploading backtest files...')
            subprocess.run([sys.executable, os.path.join(_HERE, 'ftp_dashboard.py')] + files, check=True)


if __name__ == '__main__':
    main()
