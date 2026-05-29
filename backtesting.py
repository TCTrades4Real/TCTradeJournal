"""
backtesting.py — Backtest a breakout strategy on historically-traded symbols.

ENTRY CONDITIONS  (2-min bars, long only, session open -> 16:00 ET)
  1. Bar high >= prev 2-min bar high x 1.0075            (breakout trigger, fill at trigger)
  2. Bar volume >= 25,000
  3. 2-min MACD > signal line
  4. EMA proximity: |prev_high - EMA9| / max <= 7.5%
                 OR |EMA9 - EMA20| / max <= 2.5%
  5. Prev bar high > prev bar EMA9
  6. Prev bar EMA9 > prev bar VWAP
  Fill: trigger price (no slippage)

EXIT CONDITIONS  (first hit wins except partial TPs)
  1. Bar low <= prev 2-min bar low x 0.99                hard stop  (set at entry, fixed)
  2. Bar low < highest 2-min bar low since entry x 0.99   trail stop
     (activates only when bar open >= entry + $0.15)
#  3. Partial TPs: 1/2 at 1R, then 1/4 of remainder at each R
  4. EOD: close at last bar

SIZING  shares = floor($25 / risk-per-share)
        risk-per-share = entry - (prev_2min_bar_low x 0.99)

Usage:
    python backtesting.py
    python backtesting.py --from-date 2025-09-01
    python backtesting.py --symbol SIDU
    python backtesting.py --export          # writes dashboard/backtest/backtest_trades_YYYY.json + backtest_index.json
"""

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
OHLCV_DIR = os.path.join(_HERE, 'dashboard', 'ohlcv')

# ╔══════════════════════════════════════════════════════════════╗
# ║                  STRATEGY PARAMETERS                        ║
# ╠══════════════════════════════════════════════════════════════╣
# ║  Edit these values to tune the strategy.                    ║
# ╚══════════════════════════════════════════════════════════════╝

# ── Backtest date range ───────────────────────────────────────
DEFAULT_FROM_DATE  = '2025-09-01'   # YYYY-MM-DD; override with --from-date

# ── Sizing ────────────────────────────────────────────────────
RISK_PER_TRADE     = 2.25           # dollars risked per trade

# ── Entry ─────────────────────────────────────────────────────
ENTRY_MULT         = 1.0075         # breakout trigger: bar high > prev high * this
FILL_SLIP          = 0.00           # slippage added to fill price
MIN_VOLUME         = 25_000         # bar volume must be >= this
EMA_FAST           = 9              # fast EMA period (2-min bars)
EMA_SLOW           = 20             # slow EMA period (2-min bars)
MACD_FAST          = 12             # MACD fast EMA
MACD_SLOW          = 26             # MACD slow EMA
MACD_SIG           = 9              # MACD signal EMA
EMA_PROX_HIGH      = 0.075          # |prev_high - EMA9| / max <= this  (proximity A)
EMA_PROX_CROSS     = 0.025          # |EMA9 - EMA20| / max    <= this   (proximity B)
# ── Exit ──────────────────────────────────────────────────────
STOP_MULT          = 0.99           # stop = prev 2-min bar low * this
TRAIL_ACTIVATE     = 0.15           # trail activates when bar open >= entry + this (cents)


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


def aggregate_2min(bars):
    return _aggregate(bars, 2, with_vwap=True)

def aggregate_30min(bars):
    return _aggregate(bars, 30, with_vwap=False)


def ts_to_hhmm(ts):
    return datetime.fromtimestamp(ts, tz=_ET).strftime('%H:%M')


# ── core backtest ─────────────────────────────────────────────────────────────

def backtest_symbol_day(symbol, date_str, bars_1min, first_trade_hhmm, real_rts=None):
    two_min  = aggregate_2min(bars_1min)

    if len(two_min) < MACD_SLOW + MACD_SIG + 2:
        return []

    # slot → real entry price for same-candle override
    real_entry_by_slot = {}
    for rt in (real_rts or []):
        et = rt.get('entry_time', '')
        ep = rt.get('entry_price') or rt.get('entry')
        if et and ep:
            h, m = map(int, et.split(':'))
            real_entry_by_slot[(h * 60 + m) // 2] = ep

    # --- indicator arrays on 2-min bars ---
    closes    = [b['close'] for b in two_min]
    ema9      = calc_ema(closes, EMA_FAST)
    ema20     = calc_ema(closes, EMA_SLOW)
    macd, sig = calc_macd(closes)

    start_slot2 = (lambda h, m: (h * 60 + m) // 2)(
        *map(int, first_trade_hhmm.split(':')))

    open_positions = []
    trades         = []

    for i in range(1, len(two_min)):
        cur  = two_min[i]
        prev = two_min[i - 1]

        # ── update open positions ─────────────────────────────────────────
        still_open = []
        for pos in open_positions:
            # update highest 2-min bar low seen since entry
            if prev['low'] > pos['best_2min_low']:
                pos['best_2min_low'] = prev['low']

            hard_stop  = pos['stop']
            trail_stop = pos['best_2min_low'] * STOP_MULT \
                         if cur['open'] >= pos['entry'] + TRAIL_ACTIVATE else None

            effective_stop = hard_stop
            stop_reason    = 'stop'
            if trail_stop is not None and trail_stop > hard_stop:
                effective_stop = trail_stop
                stop_reason    = 'trail'

            # stop hits remaining shares
            if cur['low'] <= effective_stop:
                exit_px = effective_stop
                pnl     = pos['realized_pnl'] + (exit_px - pos['entry']) * pos['shares_rem']
                trades.append({**pos,
                                'exit': round(exit_px, 4),
                                'exit_reason': stop_reason,
                                'exit_time': ts_to_hhmm(cur['time']),
                                'exit_ts': cur['time'],
                                'pnl': round(pnl, 2)})
                continue

            # # partial TP exits: half at 1R, then 1/4 of remaining at each R after
            # last_tp_px = pos['entry']   # track last exit price for final record
            # while cur['high'] >= pos['next_tp'] and pos['shares_rem'] > 0:
            #     tp_px      = pos['next_tp']
            #     sell       = max(1, pos['shares_rem'] // 2) if pos['tp_count'] == 0 \
            #                  else max(1, pos['shares_rem'] // 4)
            #     sell       = min(sell, pos['shares_rem'])
            #     pos['realized_pnl'] += (tp_px - pos['entry']) * sell
            #     pos['shares_rem']   -= sell
            #     pos['next_tp']       = round(pos['next_tp'] + pos['R'], 4)
            #     pos['tp_count']     += 1
            #     last_tp_px           = tp_px
            #
            #     if pos['shares_rem'] == 0:
            #         trades.append({**pos,
            #                        'exit': round(last_tp_px, 4),
            #                        'exit_reason': 'tp',
            #                        'exit_time': ts_to_hhmm(cur['time']),
            #                        'exit_ts': cur['time'],
            #                        'pnl': round(pos['realized_pnl'], 2)})
            #         break

            if pos['shares_rem'] > 0:
                still_open.append(pos)
        open_positions = still_open

        # ── look for new entry ────────────────────────────────────────────
        if cur['slot'] < start_slot2:
            continue

        if open_positions:          # only 1 position at a time
            continue

        e9_prev  = ema9[i - 1]
        e20_prev = ema20[i - 1]
        e9_cur   = ema9[i]
        macd_cur = macd[i]
        sig_cur  = sig[i]

        if any(v is None for v in [e9_prev, e20_prev, e9_cur, macd_cur, sig_cur]):
            continue

        trigger  = prev['high'] * ENTRY_MULT
        entry_px = trigger + FILL_SLIP
        real_px  = real_entry_by_slot.get(cur['slot'])
        if real_px:
            entry_px = real_px

        # 1. breakout
        if cur['high'] < trigger:
            continue

        # 2. volume
        if cur['volume'] < MIN_VOLUME:
            continue

        # 3. MACD
        if macd_cur <= sig_cur:
            continue

        # 4. EMA proximity (either condition)
        prox_high  = abs(prev['high'] - e9_prev) / max(prev['high'], e9_prev)
        prox_cross = abs(e9_prev - e20_prev)     / max(e9_prev, e20_prev)
        if prox_high > EMA_PROX_HIGH and prox_cross > EMA_PROX_CROSS:
            continue

        # 5. prev bar high > prev bar EMA9
        if prev['high'] <= e9_prev:
            continue

        # 6. prev bar EMA9 > prev bar VWAP
        if e9_prev <= prev['vwap']:
            continue

        # no fill if stop fires same bar
        stop_px = prev['low'] * STOP_MULT
        if cur['low'] <= stop_px:
            continue

        risk_per_share = entry_px - stop_px
        if risk_per_share <= 0:
            continue

        shares = math.floor(RISK_PER_TRADE / risk_per_share)
        if shares < 1:
            continue

        R  = entry_px - stop_px
        tp = entry_px + R

        open_positions.append(dict(
            date=date_str, symbol=symbol,
            entry=round(entry_px, 4), stop=round(stop_px, 4),
            tp=round(tp, 4), R=round(R, 4), shares=shares,
            entry_time=ts_to_hhmm(cur['time']),
            entry_ts=cur['time'],
            best_2min_low=cur['low'],
            shares_rem=shares,
            next_tp=round(tp, 4),
            tp_count=0,
            realized_pnl=0.0,
        ))

    # EOD
    if two_min and open_positions:
        last = two_min[-1]
        for pos in open_positions:
            pnl = pos['realized_pnl'] + (last['close'] - pos['entry']) * pos['shares_rem']
            trades.append({**pos,
                           'exit': round(last['close'], 4),
                           'exit_reason': 'eod',
                           'exit_time': ts_to_hhmm(last['time']),
                           'exit_ts': last['time'],
                           'pnl': round(pnl, 2)})

    return trades


# ── data loading ──────────────────────────────────────────────────────────────

def load_ohlcv_day(date_str):
    path = os.path.join(OHLCV_DIR, f'ohlcv_{date_str}.json')
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--from-date', default=DEFAULT_FROM_DATE)
    parser.add_argument('--symbol',    default=None)
    parser.add_argument('--export',    action='store_true',
                        help='Write trades to dashboard/backtest/backtest_trades_YYYY.json')
    parser.add_argument('--upload',    action='store_true',
                        help='FTP upload backtest files after --export (implies --export)')
    args = parser.parse_args()
    if args.upload:
        args.export = True

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
                    sym_rts   = {}
                    for rt in rts:
                        sym = rt.get('symbol', '')
                        et  = rt.get('entry_time', '')
                        if sym and et:
                            if sym not in sym_first or et < sym_first[sym]:
                                sym_first[sym] = et
                            sym_rts.setdefault(sym, []).append(rt)
                    for sym, first_t in sym_first.items():
                        if args.symbol and sym != args.symbol:
                            continue
                        opportunities.append((date_str, sym, first_t, sym_rts.get(sym, [])))

    print(f'Symbol-days to scan: {len(opportunities)}')

    all_trades  = []
    ohlcv_cache = {}

    for date_str, symbol, first_t, real_rts in opportunities:
        if date_str not in ohlcv_cache:
            if len(ohlcv_cache) >= 10:
                del ohlcv_cache[next(iter(ohlcv_cache))]
            ohlcv_cache[date_str] = load_ohlcv_day(date_str)

        bars = ohlcv_cache[date_str].get(symbol) or []
        if not bars:
            continue

        result = backtest_symbol_day(symbol, date_str, bars, first_t, real_rts)
        all_trades.extend(result)

    if not all_trades:
        print('No trades generated.')
        return

    # ── stats ─────────────────────────────────────────────────────────────────
    pnls   = [t['pnl'] for t in all_trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(pnls)
    net    = sum(pnls)
    wr     = len(wins) / n * 100
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
    for t in all_trades:
        day_pnl[t['date']] += t['pnl']
    win_days  = sum(1 for v in day_pnl.values() if v > 0)
    loss_days = sum(1 for v in day_pnl.values() if v <= 0)

    # avg trades per calendar month
    dates       = sorted(t['date'] for t in all_trades)
    from_ym     = (int(dates[0][:4]),  int(dates[0][5:7]))
    to_ym       = (int(dates[-1][:4]), int(dates[-1][5:7]))
    num_months  = (to_ym[0] - from_ym[0]) * 12 + (to_ym[1] - from_ym[1]) + 1
    avg_per_mo  = n / num_months if num_months > 0 else n

    W = len(wins)
    L = len(losses)

    strategy_name = f'[A] 30-min stop+trail  ({args.from_date}+)'
    pnl_str = f'{net:+,.2f}'
    aw_str  = f'+{avg_w:.2f}'
    al_str  = f'{avg_l:.2f}'
    apm_str = f'{avg_per_mo:.1f}'

    col = max(len('[A] 30-min stop+trail  (2025-09-01+)'), len('STRATEGY'))
    header = (
        f'  {"STRATEGY":<{col}}  {"TRADES":>6}  {"T/MO":>6} | {"WIN DAYS":>8}  {"LOSS DAYS":>9} | '
        f'{"WINS":>6}  {"LOSSES":>6} | {"WIN %":>6} | '
        f'{"MAX CW":>6}  {"MAX CL":>6} | {"AVG WIN":>8} | {"AVG LOSS":>8} | {"R/R":>4} | {"GROSS P&L":>10}'
    )
    divider = '-' * len(header)
    data_row = (
        f'  {strategy_name:<{col}}  {n:>6}  {apm_str:>6} | {win_days:>8}  {loss_days:>9} | '
        f'{W:>6}  {L:>6} | {wr:>5.1f}% | '
        f'{max_cw:>6}  {max_cl:>6} | {aw_str:>8} | {al_str:>8} | {rr:>4.2f} | {pnl_str:>10}'
    )

    print()
    print(divider)
    print(header)
    print(divider)
    print(data_row)
    print(divider)

    # monthly P&L breakdown by session
    def session(hhmm):
        h, m = map(int, hhmm.split(':'))
        mins = h * 60 + m
        if mins < 570:   return 'pre'    # before 09:30
        if mins < 960:   return 'rth'    # 09:30 - 15:59
        return 'ath'                      # 16:00+

    month_pnl = defaultdict(float)
    sess_pnl  = defaultdict(lambda: defaultdict(float))
    for t in all_trades:
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
    print('  ENTRY (Long only, 2-min bars, session open -> 16:00 ET)')
    print(f'    1. Bar high >= prev 2-min high x {ENTRY_MULT}  (fill: trigger + ${FILL_SLIP:.2f})')
    print(f'    2. Bar volume >= {MIN_VOLUME:,}')
    print( '    3. 2-min MACD > signal line')
    print(f'    4. |prev_high - EMA9| / max <= {EMA_PROX_HIGH*100:.1f}%  OR  |EMA9 - EMA20| / max <= {EMA_PROX_CROSS*100:.1f}%')
    print( '    5. Prev bar high > prev bar EMA9')
    print( '    6. Prev bar EMA9 > prev bar VWAP')
    print()
    print('  EXIT (first hit wins except partial TPs)')
    print(f'    Hard Stop   prev 2-min bar low x {STOP_MULT}  (set at entry, fixed)')
    print(f'    Trail Stop  highest 2-min low since entry x {STOP_MULT}  (activates when open >= entry + ${TRAIL_ACTIVATE})')
    # print( '    Take Profit 1/2 at 1R, then 1/4 of remainder at each R  (R = entry - hard stop)')
    print( '    EOD         close at last bar')
    print()
    print(f'  SIZING  risk=${RISK_PER_TRADE:.0f}  shares=floor(${RISK_PER_TRADE:.0f} / (entry - stop))')
    print()

    # ── optional JSON export ──────────────────────────────────────────────────
    if args.export:
        export_dir = os.path.join(_HERE, 'dashboard', 'backtest')
        os.makedirs(export_dir, exist_ok=True)

        by_year = {}
        for t in all_trades:
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
            }
            by_year.setdefault(t['date'][:4], []).append(rec)

        generated = datetime.now(_ET).strftime('%Y-%m-%d %H:%M')
        total = 0
        for yr, yr_trades in sorted(by_year.items()):
            yr_path = os.path.join(export_dir, f'backtest_trades_{yr}.json')
            with open(yr_path, 'w') as f:
                json.dump({'strategy': strategy_name, 'generated': generated,
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
