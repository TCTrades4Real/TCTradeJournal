"""
fetch_ohlcv.py — pre-fetch 1-minute OHLCV for every symbol/day in calendar data
Writes dashboard/ohlcv_YYYY-MM-DD.json per trading day (structure: { "SYMBOL": [bars...] })

Usage:
    python fetch_ohlcv.py                              # fetch all missing dates
    python fetch_ohlcv.py --refresh                    # re-fetch everything (overwrites cache)
    python fetch_ohlcv.py --date 2026-03-16            # re-fetch all symbols for one date
    python fetch_ohlcv.py --symbol ACXP                # re-fetch all dates for one symbol
    python fetch_ohlcv.py --date 2026-03-16 --symbol ACXP  # re-fetch one symbol/date pair

Source priority: Schwab API (primary) → Massive API (fallback).
Schwab only retains ~10 days of 1-min history; Massive covers all dates.
"""

import json
import os
import sys
import argparse
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo('America/New_York')

MASSIVE_API_KEY = 'REDACTED_API_KEY'
MASSIVE_BASE    = 'https://api.massive.com'

# Schwab client — initialised once in main(), shared by fetch helpers
_schwab_client = None

_HERE        = os.path.dirname(__file__)
DASHBOARD    = os.path.join(_HERE, 'dashboard')
CAL_DIR      = os.path.join(DASHBOARD, 'calendar')
OHLCV_DIR    = os.path.join(DASHBOARD, 'ohlcv')
INDEX_PATH   = os.path.join(CAL_DIR, 'calendar_index.json')


def day_path(date_str):
    return os.path.join(OHLCV_DIR, f'ohlcv_{date_str}.json')


def load_calendar_pairs():
    """Return sorted list of (date_str, symbol) pairs from per-year calendar files."""
    if not os.path.exists(INDEX_PATH):
        # Fallback: try legacy monolith
        legacy = os.path.join(CAL_DIR, 'calendar_data.json')
        if not os.path.exists(legacy):
            print(f'ERROR: {INDEX_PATH} not found.')
            print('Run:  python calendar_data.py  first.')
            sys.exit(1)
        with open(legacy) as f:
            cal = json.load(f)
        years_data = [cal]
    else:
        with open(INDEX_PATH) as f:
            idx = json.load(f)
        years_data = []
        for yr in idx.get('years', []):
            yr_path = os.path.join(CAL_DIR, f'calendar_data_{yr}.json')
            if os.path.exists(yr_path):
                with open(yr_path) as f:
                    years_data.append(json.load(f))

    pairs = []
    for cal in years_data:
        for year, year_data in cal.items():
            for month, month_data in year_data.items():
                for day, day_data in month_data.get('days', {}).items():
                    date_str = f'{year}-{month.zfill(2)}-{day.zfill(2)}'
                    for sym in (day_data.get('symbols') or {}):
                        pairs.append((date_str, sym))
    pairs.sort()
    return pairs


def _fetch_schwab(symbol, date_str):
    """Try Schwab price_history for one symbol/date. Returns candle list or []."""
    if _schwab_client is None:
        return []
    try:
        # Build midnight-to-midnight ET window in epoch-ms
        dt_start = datetime.strptime(date_str, '%Y-%m-%d').replace(
            hour=4, minute=0, second=0, tzinfo=_ET)
        dt_end   = datetime.strptime(date_str, '%Y-%m-%d').replace(
            hour=20, minute=0, second=0, tzinfo=_ET)

        resp = _schwab_client.price_history(
            symbol,
            frequencyType='minute',
            frequency=1,
            startDate=dt_start,
            endDate=dt_end,
            needExtendedHoursData=True,
        )
        resp.raise_for_status()
        candles = resp.json().get('candles') or []
        if not candles:
            return []
        return [
            {
                'time':   c['datetime'] // 1000,
                'open':   round(c['open'],   4),
                'high':   round(c['high'],   4),
                'low':    round(c['low'],    4),
                'close':  round(c['close'],  4),
                'volume': int(c.get('volume') or 0),
            }
            for c in candles
        ]
    except Exception as e:
        print(f'  [schwab error] {e}', end=' ')
        return []


def _fetch_massive(symbol, date_str):
    """Fetch from Massive API. Returns candle list or []."""
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
        results = resp.json().get('results') or []
        if not results:
            return []
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
        print(f'  [massive error] {e}', end=' ')
        return []


def fetch_day(symbol, date_str):
    """Fetch 1-min OHLCV: Schwab primary, Massive fallback."""
    candles = _fetch_schwab(symbol, date_str)
    if candles:
        return candles
    if _schwab_client is not None:
        print('→ fallback massive', end=' ')
    return _fetch_massive(symbol, date_str)


def latest_cached_date():
    """Return the most recent date string with an ohlcv cache file, or None."""
    import glob
    files = glob.glob(os.path.join(OHLCV_DIR, 'ohlcv_*.json'))
    if not files:
        return None
    dates = []
    for f in files:
        base = os.path.basename(f)  # ohlcv_YYYY-MM-DD.json
        part = base[len('ohlcv_'):-len('.json')]
        if len(part) == 10:
            dates.append(part)
    return max(dates) if dates else None


def today_str():
    from datetime import date
    return date.today().isoformat()


def main():
    global _schwab_client

    parser = argparse.ArgumentParser(description='Fetch 1-min OHLCV for all trade days')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch all dates, overwriting any cached data')
    parser.add_argument('--date',   help='Re-fetch only this date (YYYY-MM-DD)')
    parser.add_argument('--symbol', help='Re-fetch only this symbol')
    args = parser.parse_args()

    try:
        import schwabdev
        from utilities import config
        _schwab_client = schwabdev.Client(config.SCHWAB_API_KEY, config.SCHWAB_CLIENT_ID)
        print('Schwab client ready (primary source)')
    except Exception as e:
        print(f'Schwab unavailable ({e}) — using Massive only')

    pairs = load_calendar_pairs()
    print(f'Found {len(pairs)} symbol/date pairs in calendar data\n')

    fetched = skipped = failed = 0

    # Default mode: only process dates from the most recent cached date onward
    since = None
    if not args.refresh and not args.date:
        since = latest_cached_date()
        if since:
            print(f'Most recent cache: {since} — skipping earlier dates')
        else:
            print('No existing cache — fetching all dates')

    today = today_str()

    # Group pairs by date so we read/write each day file once per date
    from collections import defaultdict
    by_date = defaultdict(list)
    for date_str, sym in pairs:
        by_date[date_str].append(sym)

    for date_str in sorted(by_date):
        if args.date and date_str != args.date:
            skipped += len(by_date[date_str])
            continue

        # Skip dates before the most recent cached date
        if since and date_str < since:
            skipped += len(by_date[date_str])
            continue

        path = day_path(date_str)
        # Force re-fetch if --refresh, --date/--symbol filter, or it's today
        force = args.refresh or bool(args.date or args.symbol) or date_str == today

        # Load existing day file
        if not force and os.path.exists(path):
            with open(path) as f:
                day_cache = json.load(f)
        else:
            day_cache = {}

        if force and date_str == today and os.path.exists(path):
            print(f'  Refreshing today ({today}) — overwriting cache')

        changed = False
        for sym in by_date[date_str]:
            if args.symbol and sym != args.symbol:
                skipped += 1
                continue

            if not force and day_cache.get(sym):
                skipped += 1
                continue

            print(f'  Fetching {date_str} / {sym} ...', end=' ', flush=True)
            candles = fetch_day(sym, date_str)

            if candles:
                day_cache[sym] = candles
                changed = True
                print(f'{len(candles)} candles')
                fetched += 1
            else:
                print('no data')
                failed += 1

        if changed:
            os.makedirs(OHLCV_DIR, exist_ok=True)
            with open(path, 'w') as f:
                json.dump(day_cache, f, separators=(',', ':'))

    print(f'\nFetched: {fetched}  |  Skipped (cached): {skipped}  |  No data: {failed}')


if __name__ == '__main__':
    main()
