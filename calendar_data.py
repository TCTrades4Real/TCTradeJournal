"""
calendar_data.py — reads MrProfit CSVs, computes daily P&L, writes dashboard/calendar_data.json

Usage:
    python calendar_data.py          # all years found in CSVs
    python calendar_data.py --year 2026
"""

import csv
import json
import os
import argparse
from datetime import datetime
from collections import defaultdict

_HERE       = os.path.dirname(__file__)
CSV_FOLDER  = os.environ.get('TRADE_DATA_DIR',  os.path.join(_HERE, 'trade_data'))
OUTPUT_DIR  = os.environ.get('OUTPUT_DIR',       os.path.join(_HERE, 'dashboard'))


def fmt_hold(total_secs):
    mins = total_secs // 60
    secs = total_secs % 60
    return f"{mins:02d}:{secs:02d}"


def parse_csvs(folder):
    """
    Read all CSV files in folder, return list of executions.

    Deduplication strategy: two fills with identical attributes within the
    same file are real separate fills (e.g. Schwab partial fills at the same
    second). Across files, if the same date range is exported twice, we keep
    at most max(count_in_any_single_file) occurrences of each key — so
    re-exports never double-count while genuine multi-fills are preserved.
    """
    from collections import Counter

    # Pass 1: collect per-file rows and count occurrences of each key
    files_data = []   # list of (Counter, list-of-execution-dicts)

    for fname in sorted(os.listdir(folder)):
        if not fname.lower().endswith('.csv'):
            continue
        fpath = os.path.join(folder, fname)
        with open(fpath, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            rows = list(reader)

        file_counter = Counter()
        file_execs   = []

        for row in rows[3:]:
            if len(row) < 11:
                continue
            cols = row[1:]
            if len(cols) < 10:
                continue
            exec_time_str = cols[0].strip()
            qty_str       = cols[3].strip()
            pos_effect    = cols[4].strip()
            symbol        = cols[5].strip()
            price_str     = cols[9].strip()

            if not exec_time_str or not symbol or not qty_str or not price_str:
                continue

            try:
                for fmt in ('%m/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M'):
                    try:
                        exec_dt = datetime.strptime(exec_time_str, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    continue
                qty   = float(qty_str)
                price = float(price_str)
            except ValueError:
                continue

            key = (exec_time_str, symbol, qty_str, price_str, pos_effect)
            file_counter[key] += 1
            file_execs.append({
                'key':        key,
                'dt':         exec_dt,
                'date':       exec_dt.date(),
                'symbol':     symbol,
                'qty':        qty,
                'price':      price,
                'pos_effect': pos_effect,
            })

        files_data.append((file_counter, file_execs))

    # Pass 2: determine max occurrences of each key across all files
    global_max = Counter()
    for fc, _ in files_data:
        for key, count in fc.items():
            global_max[key] = max(global_max[key], count)

    # Pass 3: emit up to global_max[key] executions per key
    global_seen = Counter()
    executions  = []
    for _, file_execs in files_data:
        for ex in file_execs:
            key = ex['key']
            if global_seen[key] < global_max[key]:
                global_seen[key] += 1
                ex_out = {k: v for k, v in ex.items() if k != 'key'}
                executions.append(ex_out)

    executions.sort(key=lambda x: x['dt'])
    return executions


def compute_daily_details(executions):
    """
    Group executions by symbol, run round-trip P&L algorithm.
    A round-trip is one complete cycle: position goes from 0 → nonzero → 0.

    Returns:
      daily_roundtrips: date → list of round-trip dicts
                        {symbol, pnl, qty, exit_time (HH:MM), hold_secs}
      daily_volume:     date → total execution qty (both sides, all execs)
      daily_executions: date → list of individual fill dicts
                        {symbol, time (HH:MM:SS), price, qty, side}
    """
    by_symbol = defaultdict(list)
    for ex in executions:
        by_symbol[ex['symbol']].append(ex)

    daily_roundtrips = defaultdict(list)
    daily_volume     = defaultdict(float)
    daily_executions = defaultdict(list)

    for ex in executions:
        daily_executions[ex['date']].append({
            'symbol': ex['symbol'],
            'time':   ex['dt'].strftime('%H:%M:%S'),
            'price':  round(ex['price'], 4),
            'qty':    abs(int(ex['qty'])),
            'side':   'buy' if ex['qty'] > 0 else 'sell',
        })

    for ex in executions:
        daily_volume[ex['date']] += abs(ex['qty'])

    for symbol, trades in by_symbol.items():
        position     = 0.0
        running_cost = 0.0
        entry_dt     = None
        rt_open_qty  = 0.0
        entry_value  = 0.0
        entry_qty_s  = 0.0
        exit_value   = 0.0
        exit_qty_s   = 0.0

        for t in trades:
            qty   = t['qty']
            price = t['price']
            cost  = -price * qty

            if abs(position) < 1e-9:
                entry_dt    = t['dt']
                rt_open_qty = 0.0
                entry_value = 0.0
                entry_qty_s = 0.0
                exit_value  = 0.0
                exit_qty_s  = 0.0

            position     += qty
            running_cost += cost

            if t['pos_effect'] == 'TO OPEN':
                rt_open_qty += abs(qty)
                entry_value += price * abs(qty)
                entry_qty_s += abs(qty)
            elif t['pos_effect'] == 'TO CLOSE':
                exit_value  += price * abs(qty)
                exit_qty_s  += abs(qty)

            if abs(position) < 1e-9 and entry_dt is not None:
                hold_secs   = int((t['dt'] - entry_dt).total_seconds())
                avg_entry   = round(entry_value / entry_qty_s, 4) if entry_qty_s > 0 else 0
                avg_exit    = round(exit_value  / exit_qty_s,  4) if exit_qty_s  > 0 else 0
                daily_roundtrips[t['date']].append({
                    'symbol':      symbol,
                    'pnl':         round(running_cost, 2),
                    'qty':         rt_open_qty,
                    'entry_time':  entry_dt.strftime('%H:%M'),
                    'exit_time':   t['dt'].strftime('%H:%M'),
                    'hold_secs':   hold_secs,
                    'entry_price': avg_entry,
                    'exit_price':  avg_exit,
                })
                position     = 0.0
                running_cost = 0.0
                entry_dt     = None
                rt_open_qty  = 0.0

    return daily_roundtrips, daily_volume, daily_executions


def build_json(daily_roundtrips, daily_volume, daily_executions, filter_year=None):
    """Organise round-trip data into year → month → day structure with full stats."""
    result = {}

    for date, roundtrips in daily_roundtrips.items():
        yr = str(date.year)
        mo = str(date.month)
        dy = str(date.day)

        if filter_year and yr != str(filter_year):
            continue

        if yr not in result:
            result[yr] = {}
        if mo not in result[yr]:
            result[yr][mo] = {'total': 0.0, 'trades': 0, 'days': {}}

        total_pnl    = round(sum(rt['pnl'] for rt in roundtrips), 2)
        total_trades = len(roundtrips)
        winning = [rt for rt in roundtrips if rt['pnl'] > 0]
        losing  = [rt for rt in roundtrips if rt['pnl'] < 0]

        # ── Per-symbol breakdown ──────────────────────────
        sym_map = {}
        for rt in roundtrips:
            s = rt['symbol']
            if s not in sym_map:
                sym_map[s] = {'trades': 0, 'shares': 0, 'pnl': 0.0}
            sym_map[s]['trades'] += 1
            sym_map[s]['shares'] += int(rt['qty'])
            sym_map[s]['pnl']     = round(sym_map[s]['pnl'] + rt['pnl'], 2)

        # ── Stats ─────────────────────────────────────────
        accuracy       = round(len(winning) / total_trades * 100, 2) if total_trades else 0
        gross_profit   = sum(rt['pnl'] for rt in winning)
        gross_loss_abs = abs(sum(rt['pnl'] for rt in losing))
        profit_factor  = round(gross_profit / gross_loss_abs, 2) if gross_loss_abs > 0 else 0
        volume         = int(daily_volume.get(date, 0))
        avg_trade_pnl  = round(total_pnl / total_trades, 2) if total_trades else 0
        one_sided_qty  = sum(rt['qty'] for rt in roundtrips)
        avg_qty        = round(one_sided_qty / total_trades) if total_trades else 0

        biggest_winner   = round(max((rt['pnl'] for rt in winning), default=0), 2)
        avg_win_trade    = round(gross_profit / len(winning), 2) if winning else 0
        avg_hold_win_s   = round(sum(rt['hold_secs'] for rt in winning) / len(winning)) if winning else 0
        avg_cents_win    = round(
            sum(rt['pnl'] / rt['qty'] for rt in winning if rt['qty'] > 0) / len(winning), 5
        ) if winning else 0

        biggest_loser    = round(min((rt['pnl'] for rt in losing), default=0), 2)
        avg_loss_trade   = round(sum(rt['pnl'] for rt in losing) / len(losing), 2) if losing else 0
        avg_hold_loss_s  = round(sum(rt['hold_secs'] for rt in losing) / len(losing)) if losing else 0
        avg_cents_loss   = round(
            sum(rt['pnl'] / rt['qty'] for rt in losing if rt['qty'] > 0) / len(losing), 5
        ) if losing else 0

        # ── Intraday chart points (cumulative PnL per round-trip close) ──
        sorted_rts = sorted(roundtrips, key=lambda x: x['exit_time'])
        cum = 0.0
        chart = []
        for rt in sorted_rts:
            cum += rt['pnl']
            chart.append({'t': rt['exit_time'], 'pnl': round(cum, 2)})

        result[yr][mo]['days'][dy] = {
            'pnl':     total_pnl,
            'trades':  total_trades,
            'symbols': sym_map,
            'roundtrips': [
                {
                    'symbol':      rt['symbol'],
                    'pnl':         rt['pnl'],
                    'qty':         rt['qty'],
                    'entry_time':  rt['entry_time'],
                    'exit_time':   rt['exit_time'],
                    'hold_secs':   rt['hold_secs'],
                    'entry_price': rt['entry_price'],
                    'exit_price':  rt['exit_price'],
                }
                for rt in roundtrips
            ],
            'executions': daily_executions.get(date, []),
            'stats': {
                'accuracy':          accuracy,
                'profit_factor':     profit_factor,
                'volume':            volume,
                'avg_trade_pnl':     avg_trade_pnl,
                'avg_qty':           int(avg_qty),
                'biggest_winner':    biggest_winner,
                'avg_winning_trade': avg_win_trade,
                'winning_trades':    len(winning),
                'avg_hold_win':      fmt_hold(avg_hold_win_s),
                'avg_cents_win':     avg_cents_win,
                'biggest_loser':     biggest_loser,
                'avg_losing_trade':  avg_loss_trade,
                'losing_trades':     len(losing),
                'avg_hold_loss':     fmt_hold(avg_hold_loss_s),
                'avg_cents_loss':    avg_cents_loss,
            },
            'chart': chart,
        }
        result[yr][mo]['total']  = round(result[yr][mo]['total'] + total_pnl, 2)
        result[yr][mo]['trades'] += total_trades

    return result


def main():
    parser = argparse.ArgumentParser(description='Build calendar P&L JSON from trade CSVs')
    parser.add_argument('--year', type=int, help='Filter to a specific year (default: all years)')
    args = parser.parse_args()

    print(f'Reading CSVs from: {CSV_FOLDER}')
    executions = parse_csvs(CSV_FOLDER)
    print(f'Loaded {len(executions)} executions across all files')

    daily_roundtrips, daily_volume, daily_executions = compute_daily_details(executions)
    print(f'Computed P&L for {len(daily_roundtrips)} trading days')

    data = build_json(daily_roundtrips, daily_volume, daily_executions, filter_year=args.year)

    cal_dir = os.path.join(OUTPUT_DIR, 'calendar')
    os.makedirs(cal_dir, exist_ok=True)

    available_years = []
    for year, year_data in data.items():
        out_path = os.path.join(cal_dir, f'calendar_data_{year}.json')
        with open(out_path, 'w') as f:
            json.dump({year: year_data}, f, indent=2)
        available_years.append(int(year))
        print(f'Written: {out_path}')

    index_path = os.path.join(cal_dir, 'calendar_index.json')
    with open(index_path, 'w') as f:
        json.dump({'years': sorted(available_years)}, f)
    print(f'Written: {index_path}')

    for year in sorted(data):
        for month in sorted(data[year], key=int):
            month_name = datetime(int(year), int(month), 1).strftime('%B')
            total = data[year][month]['total']
            sign  = '+' if total >= 0 else ''
            print(f'  {year}-{month_name:10s} {sign}${total:.2f}')


if __name__ == '__main__':
    main()
