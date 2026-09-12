"""
tp_sweep.py — sweep take-profit configurations against Strategy A's entry/stop/trail rules
(unchanged), using cached tick_data/ for all of 2026, to find the config that maximizes net P&L.

Reuses backtest_symbol_day() from backtesting.py — only the exit-side TP/partial-scaling
kwargs vary between configs; entry, stop, and trail rules are identical across the whole sweep.

Usage:
    python tp_sweep.py
"""
import json
import os

from backtesting import backtest_symbol_day, load_tick_day, CAL_DIR

FROM_DATE = '2026-01-01'

A_BAR_MIN, A_STOP_MIN, A_EXTRA_MIN, A_TRAIL_MIN = 1, 2, 2, 2
A_MOMENTUM_FILTER = 'macd'


def load_opportunities(from_date):
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
                        et = rt.get('entry_time', '')
                        if sym and et:
                            if sym not in sym_first or et < sym_first[sym]:
                                sym_first[sym] = et
                    for sym, first_t in sym_first.items():
                        opportunities.append((date_str, sym, first_t))
    return opportunities


# ── candidate configurations: (label, kwargs passed to backtest_symbol_day) ────────────────
CONFIGS = [
    ('current: 2.5R/$1 full + partial on bar-close>cost',
     dict(partial_tp_bar_close=True, full_tp_r=2.5, full_tp_dollar=1.00)),

    ('no TP cap (trail/stop/EOD only)', dict()),

    # full exit only, no partials — isolates the effect of the TP level itself
    ('full exit @ 1.0R only', dict(full_tp_r=1.0)),
    ('full exit @ 1.5R only', dict(full_tp_r=1.5)),
    ('full exit @ 2.0R only', dict(full_tp_r=2.0)),
    ('full exit @ 2.5R only', dict(full_tp_r=2.5)),
    ('full exit @ 3.0R only', dict(full_tp_r=3.0)),
    ('full exit @ 4.0R only', dict(full_tp_r=4.0)),
    ('full exit @ 5.0R only', dict(full_tp_r=5.0)),

    # R-ladder partials: 25% off at each 1R step
    ('partials every 1R, no cap',        dict(partial_tp=True)),
    ('partials every 1R, capped @ 3R',   dict(partial_tp=True, full_tp_r=3.0)),

    # dollar-ladder partials: 25% off every $X move, with/without a full-exit cap
    ('partials every $0.25, no cap',         dict(partial_tp=True, partial_tp_step_dollar=0.25)),
    ('partials every $0.25, capped @ 2.5R',  dict(partial_tp=True, partial_tp_step_dollar=0.25, full_tp_r=2.5)),
    ('partials every $0.50, no cap',         dict(partial_tp=True, partial_tp_step_dollar=0.50)),
    ('partials every $0.50, capped @ 2.5R',  dict(partial_tp=True, partial_tp_step_dollar=0.50, full_tp_r=2.5)),
    ('partials every $1.00, no cap',         dict(partial_tp=True, partial_tp_step_dollar=1.00)),
    ('partials every $1.00, capped @ 2.5R',  dict(partial_tp=True, partial_tp_step_dollar=1.00, full_tp_r=2.5)),
]


def run():
    opportunities = load_opportunities(FROM_DATE)
    print(f'2026 symbol-days to scan: {len(opportunities)}\n')

    tick_cache = {}   # loaded once per date, reused across every config
    results = {name: [] for name, _ in CONFIGS}
    missing = 0

    for date_str, symbol, first_t in opportunities:
        if date_str not in tick_cache:
            tick_cache[date_str] = load_tick_day(date_str)
        bars = tick_cache[date_str].get(symbol) or []
        if not bars:
            missing += 1
            continue
        for name, kwargs in CONFIGS:
            trades = backtest_symbol_day(symbol, date_str, bars, first_t,
                                          bar_minutes=A_BAR_MIN, stop_minutes=A_STOP_MIN,
                                          extra_trigger_minutes=A_EXTRA_MIN,
                                          trail_minutes=A_TRAIL_MIN,
                                          momentum_filter=A_MOMENTUM_FILTER,
                                          **kwargs)
            results[name].extend(trades)

    if missing:
        print(f'  ({missing} symbol-days had no tick_data cached — skipped)\n')

    rows = []
    for name, _ in CONFIGS:
        trades = results[name]
        n = len(trades)
        net = sum(t['pnl'] for t in trades)
        wins = [t['pnl'] for t in trades if t['pnl'] > 0]
        losses = [t['pnl'] for t in trades if t['pnl'] <= 0]
        wr = len(wins) / n * 100 if n else 0
        avg_w = sum(wins) / len(wins) if wins else 0
        avg_l = sum(losses) / len(losses) if losses else 0
        rows.append((name, n, net, wr, avg_w, avg_l))

    rows.sort(key=lambda r: -r[2])
    best = rows[0][0] if rows else None

    col = max((len(r[0]) for r in rows), default=len('CONFIG'))
    header = f'  {"CONFIG":<{col}}  {"TRADES":>6}  {"WIN %":>6}  {"AVG WIN":>8}  {"AVG LOSS":>9}  {"NET P&L":>12}'
    print(header)
    print('  ' + '-' * (len(header) - 2))
    for name, n, net, wr, avg_w, avg_l in rows:
        marker = '  <== best' if name == best else ''
        print(f'  {name:<{col}}  {n:>6}  {wr:>5.1f}%  {avg_w:>+8.2f}  {avg_l:>9.2f}  {net:>+12.2f}{marker}')


if __name__ == '__main__':
    run()
