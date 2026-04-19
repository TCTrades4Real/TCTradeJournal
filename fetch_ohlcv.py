"""
fetch_ohlcv.py — pre-fetch 1-minute OHLCV for every symbol/day in calendar_data.json
Writes dashboard/ohlcv_data.json, which candlestick.html reads locally (faster than live API).

Usage:
    python fetch_ohlcv.py                              # fetch all missing dates
    python fetch_ohlcv.py --refresh                    # re-fetch everything (overwrites cache)
    python fetch_ohlcv.py --date 2026-03-16            # re-fetch all symbols for one date
    python fetch_ohlcv.py --symbol ACXP                # re-fetch all dates for one symbol
    python fetch_ohlcv.py --date 2026-03-16 --symbol ACXP  # re-fetch one symbol/date pair
"""

import json
import os
import sys
import argparse
import requests

MASSIVE_API_KEY = 'REDACTED_API_KEY'
MASSIVE_BASE    = 'https://api.massive.com'

CALENDAR_PATH = os.path.join(os.path.dirname(__file__), 'dashboard', 'calendar_data.json')
OUTPUT_PATH   = os.path.join(os.path.dirname(__file__), 'dashboard', 'ohlcv_data.json')


def fetch_day(symbol, date_str):
    """
    Fetch 1-minute OHLCV (including pre/post market) for a single symbol + date.
    Returns list of candle dicts or [] if unavailable.
    """
    url = f'{MASSIVE_BASE}/v2/aggs/ticker/{symbol}/range/1/minute/{date_str}/{date_str}'
    params = {
        'adjusted': 'false',
        'sort':     'asc',
        'limit':    50000,
        'apiKey':   MASSIVE_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        results = data.get('results') or []
        if not results:
            return []

        return [
            {
                'time':   r['t'] // 1000,       # ms → Unix seconds
                'open':   round(r['o'], 4),
                'high':   round(r['h'], 4),
                'low':    round(r['l'], 4),
                'close':  round(r['c'], 4),
                'volume': int(r.get('v') or 0),
            }
            for r in results
        ]

    except Exception as e:
        print(f'  [error] {e}')
        return []


def main():
    parser = argparse.ArgumentParser(description='Fetch 1-min OHLCV for all trade days')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch all dates, overwriting any cached data')
    parser.add_argument('--date',   help='Re-fetch only this date (YYYY-MM-DD)')
    parser.add_argument('--symbol', help='Re-fetch only this symbol')
    args = parser.parse_args()

    if not os.path.exists(CALENDAR_PATH):
        print(f'ERROR: {CALENDAR_PATH} not found.')
        print('Run:  python calendar_data.py  first.')
        sys.exit(1)

    with open(CALENDAR_PATH) as f:
        cal = json.load(f)

    # Load existing cache unless --refresh
    if not args.refresh and os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as f:
            result = json.load(f)
        print(f'Loaded cache: {OUTPUT_PATH}  ({sum(len(v) for v in result.values())} symbol/date entries)')
    else:
        result = {}
        if args.refresh:
            print('--refresh: clearing cache')

    # Collect all (date_str, symbol) pairs from calendar_data.json
    pairs = []
    for year, year_data in cal.items():
        for month, month_data in year_data.items():
            for day, day_data in month_data.get('days', {}).items():
                date_str = f'{year}-{month.zfill(2)}-{day.zfill(2)}'
                for sym in (day_data.get('symbols') or {}):
                    pairs.append((date_str, sym))
    pairs.sort()
    print(f'Found {len(pairs)} symbol/date pairs in calendar_data.json\n')

    fetched = skipped = failed = 0

    for date_str, sym in pairs:
        if args.date and date_str != args.date:
            skipped += 1
            continue
        if args.symbol and sym != args.symbol:
            skipped += 1
            continue

        # Skip cached entries only when not forcing a re-fetch
        force = args.refresh or bool(args.date or args.symbol)
        if not force and result.get(date_str, {}).get(sym):
            skipped += 1
            continue

        print(f'  Fetching {date_str} / {sym} ...', end=' ', flush=True)
        candles = fetch_day(sym, date_str)

        if candles:
            result.setdefault(date_str, {})[sym] = candles
            print(f'{len(candles)} candles')
            fetched += 1
        else:
            print('no data')
            failed += 1

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(result, f, separators=(',', ':'))

    print(f'\nFetched: {fetched}  |  Skipped (cached): {skipped}  |  No data: {failed}')
    print(f'Written: {OUTPUT_PATH}')


if __name__ == '__main__':
    main()
