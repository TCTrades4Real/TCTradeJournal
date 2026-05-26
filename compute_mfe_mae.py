"""
compute_mfe_mae.py — add MFE/MAE fields to calendar roundtrips using OHLCV cache

Run AFTER fetch_ohlcv.py (needs dashboard/ohlcv/ohlcv_YYYY-MM-DD.json files).

MFE (Maximum Favorable Excursion) = max interim profit during the trade (position $)
MAE (Maximum Adverse Excursion)   = max interim loss during the trade (position $)

Usage:
    python compute_mfe_mae.py           # update all roundtrips missing mfe/mae
    python compute_mfe_mae.py --refresh # recompute all (overwrite existing)
    python compute_mfe_mae.py --year 2026
"""

import json
import os
import sys
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

_ET       = ZoneInfo('America/New_York')
_HERE     = os.path.dirname(__file__)
CAL_DIR   = os.path.join(_HERE, 'dashboard', 'calendar')
OHLCV_DIR = os.path.join(_HERE, 'dashboard', 'ohlcv')
INDEX_PATH = os.path.join(CAL_DIR, 'calendar_index.json')


def load_ohlcv_day(date_str):
    """Return the full day dict {SYMBOL: [bars...]} or {} if file missing."""
    path = os.path.join(OHLCV_DIR, f'ohlcv_{date_str}.json')
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def bars_in_window(bars, entry_hhmm, exit_hhmm):
    """Return bars whose 1-min ET time falls within [entry_hhmm, exit_hhmm]."""
    result = []
    for b in bars:
        dt       = datetime.fromtimestamp(b['time'], tz=_ET)
        bar_time = dt.strftime('%H:%M')
        if entry_hhmm <= bar_time <= exit_hhmm:
            result.append(b)
    return result


def infer_direction(pnl, entry_price, exit_price):
    """Return 'long' or 'short'. Falls back to 'long' on ambiguous scratch."""
    delta = exit_price - entry_price
    if abs(delta) < 0.0001:
        return 'long'
    return 'long' if (pnl >= 0) == (delta >= 0) else 'short'


def compute_excursions(rt, window_bars):
    """
    Return (mfe, mae) in position dollars, or (None, None) if no bars.

    Long:  MFE = (max_high - entry) * qty   MAE = (entry - min_low) * qty
    Short: MFE = (entry - min_low) * qty    MAE = (max_high - entry) * qty
    Both clamped to >= 0.
    """
    if not window_bars:
        return None, None

    entry = rt['entry_price']
    qty   = rt['qty']
    pnl   = rt['pnl']
    direction = infer_direction(pnl, entry, rt['exit_price'])

    highs    = [b['high'] for b in window_bars]
    lows     = [b['low']  for b in window_bars]
    max_high = max(highs)
    min_low  = min(lows)

    if direction == 'long':
        mfe = max(0.0, max_high - entry) * qty
        mae = max(0.0, entry - min_low)  * qty
    else:
        mfe = max(0.0, entry - min_low)  * qty
        mae = max(0.0, max_high - entry) * qty

    return round(mfe, 2), round(mae, 2)


def process_year(year_path, refresh=False):
    with open(year_path) as f:
        data = json.load(f)

    updated      = 0
    skipped      = 0
    missing_ohlcv = 0

    for year, months in data.items():
        for month, mdata in months.items():
            for day, ddata in (mdata.get('days') or {}).items():
                rts = ddata.get('roundtrips')
                if not rts:
                    continue

                date_str  = f'{year}-{month.zfill(2)}-{day.zfill(2)}'
                day_ohlcv = load_ohlcv_day(date_str)

                if not day_ohlcv:
                    missing_ohlcv += len(rts)
                    continue

                for rt in rts:
                    if not refresh and 'mfe' in rt and 'mae' in rt:
                        skipped += 1
                        continue

                    symbol  = rt.get('symbol', '')
                    bars    = day_ohlcv.get(symbol) or []
                    window  = bars_in_window(bars, rt['entry_time'], rt['exit_time'])
                    mfe, mae = compute_excursions(rt, window)

                    if mfe is not None:
                        rt['mfe'] = mfe
                        rt['mae'] = mae
                        updated += 1
                    else:
                        missing_ohlcv += 1

    with open(year_path, 'w') as f:
        json.dump(data, f, separators=(',', ':'))

    return updated, skipped, missing_ohlcv


def main():
    parser = argparse.ArgumentParser(
        description='Compute MFE/MAE for calendar roundtrips from OHLCV cache'
    )
    parser.add_argument('--refresh', action='store_true',
                        help='Recompute all existing mfe/mae fields')
    parser.add_argument('--year', type=int,
                        help='Process only this year')
    args = parser.parse_args()

    if not os.path.exists(INDEX_PATH):
        print(f'ERROR: {INDEX_PATH} not found.')
        print('Run:  python calendar_data.py  first.')
        sys.exit(1)

    with open(INDEX_PATH) as f:
        idx = json.load(f)

    years = [str(y) for y in idx.get('years', [])]
    if args.year:
        yr_str = str(args.year)
        years  = [yr_str] if yr_str in years else []
        if not years:
            print(f'Year {args.year} not found in index.')
            sys.exit(1)

    total_updated = total_skipped = total_missing = 0

    for yr in years:
        yr_path = os.path.join(CAL_DIR, f'calendar_data_{yr}.json')
        if not os.path.exists(yr_path):
            print(f'[skip] {yr_path} not found')
            continue
        print(f'Processing {yr} ...')
        updated, skipped, missing = process_year(yr_path, refresh=args.refresh)
        print(f'  updated={updated}  skipped={skipped}  no_ohlcv={missing}')
        total_updated  += updated
        total_skipped  += skipped
        total_missing  += missing

    print(f'\nDone.  updated={total_updated}  skipped={total_skipped}  no_ohlcv={total_missing}')
    if total_missing > 0:
        print('Tip: run  python fetch_ohlcv.py  to download missing price data first.')


if __name__ == '__main__':
    main()
