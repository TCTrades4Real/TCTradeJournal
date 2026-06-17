"""
compute_mfe_mae.py — annotate calendar roundtrips with MFE and MAE

MFE (Maximum Favorable Excursion): largest interim profit during the trade ($)
MAE (Maximum Adverse Excursion):   largest interim loss during the trade ($)

Both computed from 1-min OHLCV bars cached in dashboard/ohlcv/ohlcv_YYYY-MM-DD.json.
Direction (long/short) inferred: if pnl > 0 when close > entry → long, else short.

Usage:
    python compute_mfe_mae.py              # fill in missing mfe/mae only
    python compute_mfe_mae.py --refresh    # recompute all roundtrips
    python compute_mfe_mae.py --year 2026  # one year only
"""

import json
import os
import argparse
from datetime import datetime

_HERE    = os.path.dirname(__file__)
CAL_DIR  = os.path.join(_HERE, 'dashboard', 'calendar')
OHLCV_DIR = os.path.join(_HERE, 'dashboard', 'ohlcv')


def load_ohlcv(date_str):
    path = os.path.join(OHLCV_DIR, f'ohlcv_{date_str}.json')
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def time_to_mins(hhmm):
    """'HH:MM' → total minutes since midnight."""
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def compute_mfe_mae(rt, bars):
    """
    Given a roundtrip dict and a list of 1-min bar dicts (each with time, open,
    high, low, close keys), return (mfe, mae) in dollars.

    bars must already be filtered to the symbol and date.
    Each bar: { 'time': unix_ts, 'open': float, 'high': float, 'low': float, 'close': float }
    """
    entry_mins = time_to_mins(rt['entry_time'])
    exit_mins  = time_to_mins(rt['exit_time'])
    qty        = rt.get('qty', 0)
    pnl        = rt.get('pnl', 0)

    if not qty or not bars:
        return None, None

    # Infer direction from entry/exit prices and pnl
    entry_price = rt.get('entry_price', 0)
    exit_price  = rt.get('exit_price', 0)
    if entry_price and exit_price:
        is_long = (exit_price - entry_price) * pnl >= 0
    else:
        is_long = pnl >= 0  # fallback

    window = [b for b in bars
              if _bar_mins_et(b['time']) >= entry_mins
              and _bar_mins_et(b['time']) <= exit_mins]

    if not window:
        return None, None

    ref_price = entry_price if entry_price else (window[0]['open'] if window else 0)
    if not ref_price:
        return None, None

    max_fav = 0.0  # most favorable price excursion (positive)
    max_adv = 0.0  # most adverse price excursion (positive magnitude)

    for bar in window:
        if is_long:
            fav = (bar['high']  - ref_price) * qty
            adv = (ref_price - bar['low'])   * qty
        else:
            fav = (ref_price - bar['low'])   * qty
            adv = (bar['high'] - ref_price)  * qty

        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)

    return round(max_fav, 2), round(max_adv, 2)


_et_offset_cache = {}

def _bar_mins_et(unix_ts):
    """Convert unix timestamp → minutes-since-midnight in ET."""
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
    dt = datetime.fromtimestamp(unix_ts, tz=_ET)
    return dt.hour * 60 + dt.minute


def process_year(year_str, cal_path, refresh=False):
    with open(cal_path) as f:
        data = json.load(f)

    year_data = data.get(year_str, data)
    updated = 0
    skipped = 0

    for month_str, month_data in year_data.items():
        if not isinstance(month_data, dict) or 'days' not in month_data:
            continue
        for day_str, day_data in month_data['days'].items():
            roundtrips = day_data.get('roundtrips')
            if not roundtrips:
                continue

            date_str = f"{year_str}-{int(month_str):02d}-{int(day_str):02d}"
            ohlcv = load_ohlcv(date_str)

            for rt in roundtrips:
                if not refresh and rt.get('mfe') is not None and rt.get('mae') is not None:
                    skipped += 1
                    continue

                sym  = rt.get('symbol', '')
                bars = ohlcv.get(sym, [])

                mfe, mae = compute_mfe_mae(rt, bars)
                if mfe is not None:
                    rt['mfe'] = mfe
                    rt['mae'] = mae
                    updated += 1
                else:
                    skipped += 1

    print(f"  {year_str}: {updated} updated, {skipped} skipped")

    # Write back
    with open(cal_path, 'w') as f:
        json.dump(data, f, indent=2)

    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true', help='Recompute all, even if already set')
    parser.add_argument('--year', type=int, help='Process one year only')
    args = parser.parse_args()

    index_path = os.path.join(CAL_DIR, 'calendar_index.json')
    if not os.path.exists(index_path):
        print(f"calendar_index.json not found at {index_path}")
        return

    with open(index_path) as f:
        idx = json.load(f)

    years = [str(args.year)] if args.year else [str(y) for y in idx.get('years', [])]

    total = 0
    for year in years:
        cal_path = os.path.join(CAL_DIR, f'calendar_data_{year}.json')
        if not os.path.exists(cal_path):
            print(f"  {year}: calendar file not found, skipping")
            continue
        n = process_year(year, cal_path, refresh=args.refresh)
        total += n

    print(f"\nDone — {total} roundtrips annotated with MFE/MAE")


if __name__ == '__main__':
    main()
