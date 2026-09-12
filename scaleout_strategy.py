"""
scaleout_strategy.py — Strategy A's entry criteria (unchanged), with a tick-driven scale-out
exit in place of Strategy A's stop/trail/partial-TP rules.

Entry: identical 6-condition breakout checklist as backtesting.py's Strategy A (1-min bars,
OR-trigger vs prev 1-min/2-min high, MACD > signal, fill-realism at the crossing sub-bar).

Exit: a per-tick state machine (ScaleOutState) replayed over the 5-second sub-bars cached in
tick_data/ (our finest available resolution — no raw tick log is stored). Three partial-fill
triggers (r_target, structure, time_based; priority order, one fire per tick) and four
full-exit conditions (halt_down [skipped — no LULD data], hard_stop, trailing_stop, full_tp
[full exit at entry + 2.5R or entry + $1.00, whichever is closer] — giveback removed;
full-exit checked before partials each tick, priority order, first match wins).

Two variants run: exit state (entry_candle_id, prior_candle, support_level ratchet, hard-stop
reference) keyed on 1-min candles, and a duplicate keyed on 2-min candles. Entry stays on
1-min bars in both.

Usage:
    python scaleout_strategy.py
    python scaleout_strategy.py --from-date 2026-01-01
    python scaleout_strategy.py --symbol XPON
"""
import argparse
import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from backtesting import (
    _aggregate, _group_by_slot, _slot, calc_ema, calc_macd, ts_to_hhmm,
    build_prior_bar_field_lookup, load_tick_day, CAL_DIR,
    ENTRY_MULT, FILL_SLIP, MIN_VOLUME, EMA_FAST, EMA_SLOW,
    MACD_SLOW, MACD_SIG, EMA_PROX_HIGH, EMA_PROX_CROSS, STOP_MULT, RISK_PER_TRADE,
)

# ── scale-out constants ─────────────────────────────────────────────────────────
TIME_PARTIAL_BUCKET_SECONDS               = 60
STRUCTURE_PARTIAL_MIN_BID_ABOVE_ENTRY_PCT = 0.03
PARTIAL_MIN_PRICE_ADVANCE_PCT             = 0.03   # min advance vs last_partial_price, all 3 triggers
GIVEBACK_MIN_RUN_ABOVE_ENTRY_PCT          = 0.03
GIVEBACK_MIN_RUN_ABOVE_ENTRY_CAP          = 0.20   # dollars; arm dist = min(entry*pct, cap)
FULL_TP_R      = 2.5   # full exit at entry + 2.5R or entry + $1.00, whichever is closer
FULL_TP_DOLLAR = 1.00
HALT_DOWN_EXIT_BUFFER_PCT                 = 0.01   # unused — no limit_down_price data available
PARTIAL_PCT_R_TARGET  = 1 / 5
PARTIAL_PCT_STRUCTURE = 1 / 6
PARTIAL_PCT_TIME      = 1 / 7


def round_half_up(x):
    return math.floor(x + 0.5)


@dataclass
class ScaleOutState:
    symbol: str
    entry_price: float
    position_qty: float
    one_r: float
    hard_stop_price: float
    entry_candle_id: Optional[int]

    r_target_count: int = 0
    last_partial_price: float = 0.0
    last_structure_partial_level: float = 0.0
    r_gate_candle_id: Optional[int] = None
    entry_time_bucket: Optional[int] = None
    last_time_partial_bucket: Optional[int] = None
    support_level: float = 0.0
    any_partial_taken: bool = False
    highest_price_since_entry: float = 0.0
    partials_enabled: bool = True


# ── entry (Strategy A's exact 6-condition checklist) ────────────────────────────

def find_next_entry(entry_bars, ema9, ema20, macd, sig, prior_extra_high, entry_sub_bars,
                     start_i, start_slot):
    """First qualifying 1-min breakout at/after list index start_i AND clock-slot start_slot.
    Returns (i, entry_px, entry_ts) or None."""
    for i in range(max(start_i, 1), len(entry_bars)):
        cur, prev = entry_bars[i], entry_bars[i - 1]

        if cur['slot'] < start_slot:
            continue

        e9_prev, e20_prev = ema9[i - 1], ema20[i - 1]
        if e9_prev is None or e20_prev is None:
            continue

        trigger = prev['high'] * ENTRY_MULT
        extra_high = prior_extra_high(cur['time'])
        if extra_high is not None:
            trigger = min(trigger, extra_high * ENTRY_MULT)

        if cur['high'] < trigger:
            continue

        # breakout + volume, evaluated tick-by-tick (per 5-second sub-bar), not once per bar
        # close: first sub-bar where price has crossed the trigger AND cumulative volume from
        # this bar's open through that sub-bar already clears MIN_VOLUME.
        subs = entry_sub_bars.get(cur['slot']) or []
        fill_sub = None
        cum_vol = 0
        for sb in subs:
            cum_vol += sb['volume']
            if sb['high'] >= trigger and cum_vol >= MIN_VOLUME:
                fill_sub = sb
                break

        if subs and fill_sub is None:
            continue
        if not subs and cur['volume'] < MIN_VOLUME:
            continue

        entry_px = (max(trigger, fill_sub['open']) if fill_sub is not None else trigger) + FILL_SLIP

        macd_p1, sig_p1 = macd[i - 1], sig[i - 1]
        if macd_p1 is None or sig_p1 is None or macd_p1 <= sig_p1:
            continue

        prox_high  = abs(prev['high'] - e9_prev) / max(prev['high'], e9_prev)
        prox_cross = abs(e9_prev - e20_prev) / max(e9_prev, e20_prev)
        if prox_high > EMA_PROX_HIGH and prox_cross > EMA_PROX_CROSS:
            continue

        if prev['high'] <= e9_prev:
            continue
        if e9_prev <= prev['vwap']:
            continue

        return i, entry_px, cur['time']
    return None


# ── scale-out exit engine ────────────────────────────────────────────────────────

def run_scaleout_symbol_day(symbol, date_str, bars_fine, first_trade_hhmm, candle_minutes=1):
    entry_bars = _aggregate(bars_fine, 1, with_vwap=True)
    if len(entry_bars) < MACD_SLOW + MACD_SIG + 3:
        return []

    extra_bars = _aggregate(bars_fine, 2, with_vwap=False)
    prior_extra_high = build_prior_bar_field_lookup(extra_bars, 2 * 60, field='high')
    entry_sub_bars = _group_by_slot(bars_fine, 1)
    entry_times = [b['time'] for b in entry_bars]

    closes = [b['close'] for b in entry_bars]
    ema9  = calc_ema(closes, EMA_FAST)
    ema20 = calc_ema(closes, EMA_SLOW)
    macd, sig = calc_macd(closes)

    exit_bars = _aggregate(bars_fine, candle_minutes, with_vwap=False)
    if not exit_bars:
        return []
    exit_times = [b['time'] for b in exit_bars]

    def prior_exit_bar(ts):
        idx = bisect.bisect_right(exit_times, ts - candle_minutes * 60) - 1
        return exit_bars[idx] if idx >= 0 else None

    ticks = sorted(bars_fine, key=lambda b: b['time'])
    tick_times = [t['time'] for t in ticks]

    start_slot = (lambda h, m: (h * 60 + m) // 1)(*map(int, first_trade_hhmm.split(':')))

    trades = []
    scan_i = 1

    while True:
        found = find_next_entry(entry_bars, ema9, ema20, macd, sig, prior_extra_high, entry_sub_bars,
                                 scan_i, start_slot)
        if found is None:
            break
        entry_i, entry_px, entry_ts = found

        pc = prior_exit_bar(entry_ts)
        if pc is None:
            scan_i = entry_i + 1
            continue
        hard_stop_price = pc['low'] * STOP_MULT
        if entry_bars[entry_i]['low'] <= hard_stop_price:
            scan_i = entry_i + 1
            continue
        one_r = entry_px - hard_stop_price
        if one_r <= 0:
            scan_i = entry_i + 1
            continue
        shares = math.floor(RISK_PER_TRADE / one_r)
        if shares < 1:
            scan_i = entry_i + 1
            continue

        state = ScaleOutState(symbol=symbol, entry_price=entry_px, position_qty=shares,
                               one_r=one_r, hard_stop_price=hard_stop_price,
                               entry_candle_id=pc['slot'])
        state.highest_price_since_entry = entry_px
        state.entry_time_bucket = entry_ts // TIME_PARTIAL_BUCKET_SECONDS

        realized_pnl = 0.0
        exit_px = exit_ts = None
        exit_reason = 'eod'
        bar_ptr = -1

        j = bisect.bisect_left(tick_times, entry_ts)
        while j < len(ticks):
            tick = ticks[j]
            t_time, t_high, t_low, t_close = tick['time'], tick['high'], tick['low'], tick['close']

            while bar_ptr + 1 < len(exit_bars) and exit_bars[bar_ptr + 1]['time'] + candle_minutes * 60 <= t_time:
                bar_ptr += 1
            prior_candle = exit_bars[bar_ptr] if bar_ptr >= 0 else None
            current_forming_id = _slot(t_time, candle_minutes)

            # ratchets — every tick, before trigger checks
            if t_high > state.highest_price_since_entry:
                state.highest_price_since_entry = t_high
            if prior_candle is not None and prior_candle['slot'] != state.entry_candle_id \
                    and prior_candle['low'] > state.support_level:
                state.support_level = prior_candle['low']

            # ---- full-exit conditions (priority order; first match wins) ----
            fired_full = None

            # 1. halt_down_proximity — skipped: no LULD limit_down_price data available

            # 2. hard_stop
            if t_low < state.hard_stop_price:
                fired_full = ('hard_stop', t_low)

            # 3. trailing_stop
            if fired_full is None and t_low <= state.support_level * 0.99 and t_low > state.entry_price * 1.01:
                fired_full = ('trailing_stop', t_low)

            # 4. full_tp — full exit at entry + 2.5R or entry + $1.00, whichever is closer
            if fired_full is None:
                full_tp_price = state.entry_price + min(state.one_r * FULL_TP_R, FULL_TP_DOLLAR)
                if t_high >= full_tp_price:
                    fired_full = ('full_tp', full_tp_price)

            if fired_full is not None:
                reason, px = fired_full
                realized_pnl += (px - state.entry_price) * state.position_qty
                exit_px, exit_ts, exit_reason = px, t_time, reason
                state.position_qty = 0
                break

            # ---- partial triggers (priority order; first match wins; skip if qty <= 1) ----
            if state.position_qty > 1:
                fired_partial = False
                adv = 1 + PARTIAL_MIN_PRICE_ADVANCE_PCT

                # 1. r_target
                r_threshold = state.entry_price + state.one_r * state.r_target_count
                if (t_high >= r_threshold
                        and t_high >= state.last_partial_price * adv
                        and state.r_gate_candle_id is not None
                        and state.r_gate_candle_id == current_forming_id):
                    sell = min(state.position_qty, round_half_up(state.position_qty * PARTIAL_PCT_R_TARGET))
                    realized_pnl += (t_high - state.entry_price) * sell
                    state.position_qty -= sell
                    state.r_target_count += 1
                    state.last_partial_price = t_high
                    state.any_partial_taken = True
                    fired_partial = True

                # 2. structure
                if not fired_partial and prior_candle is not None \
                        and t_high > prior_candle['high'] \
                        and prior_candle['high'] != state.last_structure_partial_level \
                        and t_high >= state.entry_price * (1 + STRUCTURE_PARTIAL_MIN_BID_ABOVE_ENTRY_PCT) \
                        and t_high >= state.last_partial_price * adv:
                    sell = min(state.position_qty, round_half_up(state.position_qty * PARTIAL_PCT_STRUCTURE))
                    realized_pnl += (t_high - state.entry_price) * sell
                    state.position_qty -= sell
                    state.last_structure_partial_level = prior_candle['high']
                    state.last_partial_price = t_high
                    state.r_target_count = max(state.r_target_count,
                                                math.floor((t_high - state.entry_price) / state.one_r) + 1)
                    state.r_gate_candle_id = current_forming_id
                    state.any_partial_taken = True
                    fired_partial = True

                # 3. time_based
                if not fired_partial:
                    now_bucket = t_time // TIME_PARTIAL_BUCKET_SECONDS
                    if (t_close < state.entry_price + state.one_r
                            and now_bucket > state.entry_time_bucket
                            and t_close > state.entry_price * 1.01
                            and t_close >= state.last_partial_price * adv
                            and now_bucket != state.last_time_partial_bucket):
                        sell = min(state.position_qty, round_half_up(state.position_qty * PARTIAL_PCT_TIME))
                        realized_pnl += (t_close - state.entry_price) * sell
                        state.position_qty -= sell
                        state.last_time_partial_bucket = now_bucket
                        state.last_partial_price = t_close
                        state.any_partial_taken = True

            j += 1

        if exit_px is None:
            last_tick = ticks[-1]
            exit_px, exit_ts = last_tick['close'], last_tick['time']
            realized_pnl += (exit_px - state.entry_price) * state.position_qty
            state.position_qty = 0

        trades.append(dict(
            date=date_str, symbol=symbol,
            entry=round(entry_px, 4), entry_time=ts_to_hhmm(entry_ts), entry_ts=entry_ts,
            exit=round(exit_px, 4), exit_time=ts_to_hhmm(exit_ts), exit_ts=exit_ts,
            stop=round(hard_stop_price, 4), R=round(one_r, 4), shares=shares,
            pnl=round(realized_pnl, 2), exit_reason=exit_reason,
        ))

        scan_i = bisect.bisect_left(entry_times, exit_ts)
        if scan_i <= entry_i:
            scan_i = entry_i + 1

    return trades


# ── main: run both candle-size variants across all opportunities ────────────────

def load_opportunities(from_date):
    import json, os
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
                    if date_str < from_date:
                        continue
                    sym_first = {}
                    for rt in rts:
                        sym = rt.get('symbol', '')
                        et  = rt.get('entry_time', '')
                        if sym and et:
                            if sym not in sym_first or et < sym_first[sym]:
                                sym_first[sym] = et
                    for sym, first_t in sym_first.items():
                        opportunities.append((date_str, sym, first_t))
    return opportunities


def compute_stats(trades):
    pnls = [t['pnl'] for t in trades]
    n = len(pnls)
    net = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / n * 100 if n else 0
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    return dict(n=n, net=net, wr=wr, avg_w=avg_w, avg_l=avg_l)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-date', default='2025-09-01')
    parser.add_argument('--symbol', default=None)
    args = parser.parse_args()

    opportunities = load_opportunities(args.from_date)
    if args.symbol:
        opportunities = [o for o in opportunities if o[1] == args.symbol]
    print(f'Symbol-days to scan: {len(opportunities)}\n')

    tick_cache = {}
    results = {1: [], 2: []}

    for date_str, symbol, first_t in opportunities:
        if date_str not in tick_cache:
            if len(tick_cache) >= 10:
                del tick_cache[next(iter(tick_cache))]
            tick_cache[date_str] = load_tick_day(date_str)
        bars = tick_cache[date_str].get(symbol) or []
        if not bars:
            continue
        for cm in (1, 2):
            results[cm].extend(run_scaleout_symbol_day(symbol, date_str, bars, first_t, candle_minutes=cm))

    for cm in (1, 2):
        trades = results[cm]
        s = compute_stats(trades)
        label = f'[Scale-out] exit state on {cm}-min candles'
        print(f'{label:<45}  trades={s["n"]:>5}  win%={s["wr"]:>5.1f}  '
              f'avg_w={s["avg_w"]:>+7.2f}  avg_l={s["avg_l"]:>8.2f}  net={s["net"]:>+12.2f}')


if __name__ == '__main__':
    main()
