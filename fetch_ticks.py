"""
fetch_ticks.py — pre-fetch sub-minute OHLCV (built from raw trade prints) for every
symbol/day in calendar data, for use by backtesting.py.

Writes tick_data/ticks_YYYY-MM-DD.json per trading day (structure: { "SYMBOL": [bars...] }),
each bar covering BUCKET_SECONDS of trading (default 5s): {time, open, high, low, close, volume}.

Usage:
    python fetch_ticks.py                              # fetch all missing dates
    python fetch_ticks.py --refresh                    # re-fetch everything (overwrites cache)
    python fetch_ticks.py --date 2026-03-16             # re-fetch all symbols for one date
    python fetch_ticks.py --from-date 2025-09-01        # fetch this date onward, any year
    python fetch_ticks.py --symbol ACXP                 # re-fetch all dates for one symbol
    python fetch_ticks.py --date 2026-03-16 --symbol ACXP  # re-fetch one symbol/date pair

Source: Alpaca API (SIP feed) raw trade prints, bucketed locally — Alpaca has no native
sub-minute bars endpoint. Local-only cache: never FTP-uploaded, not used by the dashboard.
"""

import json
import os
import sys
import argparse
from datetime import datetime, date

from fetch_ohlcv import load_calendar_pairs

_alpaca_client = None

_HERE     = os.path.dirname(__file__)
TICK_DIR  = os.path.join(_HERE, 'tick_data')

BUCKET_SECONDS = 5   # bar width for the local cache; smaller = finer intrabar resolution, more disk


def day_path(date_str):
    return os.path.join(TICK_DIR, f'ticks_{date_str}.json')


def bucket_trades(trades, bucket_seconds):
    """Aggregate raw {time, price, size} prints into bucket_seconds-wide OHLCV+volume bars."""
    bars = {}
    for t in trades:
        slot = (t['time'] // bucket_seconds) * bucket_seconds
        b = bars.get(slot)
        if b is None:
            bars[slot] = {'time': slot, 'open': t['price'], 'high': t['price'],
                          'low': t['price'], 'close': t['price'], 'volume': t['size']}
        else:
            b['high']   = max(b['high'], t['price'])
            b['low']    = min(b['low'],  t['price'])
            b['close']  = t['price']
            b['volume'] += t['size']
    return [bars[k] for k in sorted(bars)]


def _fetch_alpaca(symbol, date_str):
    """Try Alpaca get_trades for one symbol/date, bucketed to BUCKET_SECONDS. Returns bar list or []."""
    if _alpaca_client is None:
        return []
    try:
        trades = _alpaca_client.get_trades(symbol, date_str)
        return bucket_trades(trades, BUCKET_SECONDS)
    except Exception as e:
        print(f'  [alpaca error] {e}', end=' ')
        return []


def latest_cached_date():
    """Return the most recent date string with a tick cache file, or None."""
    import glob
    files = glob.glob(os.path.join(TICK_DIR, 'ticks_*.json'))
    if not files:
        return None
    dates = []
    for f in files:
        base = os.path.basename(f)  # ticks_YYYY-MM-DD.json
        part = base[len('ticks_'):-len('.json')]
        if len(part) == 10:
            dates.append(part)
    return max(dates) if dates else None


def main():
    global _alpaca_client

    parser = argparse.ArgumentParser(description='Fetch sub-minute OHLCV (from trade prints) for all trade days')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch all dates, overwriting any cached data')
    parser.add_argument('--date',   help='Re-fetch only this date (YYYY-MM-DD)')
    parser.add_argument('--from-date', help='Fetch this date onward (YYYY-MM-DD), any year')
    parser.add_argument('--symbol', help='Re-fetch only this symbol')
    parser.add_argument('--all', action='store_true',
                        help='Process all years (default is current year only)')
    args = parser.parse_args()

    try:
        from utilities import config
        from utilities.alpaca_client import AlpacaClient
        _alpaca_client = AlpacaClient(config.ALPACA_API_KEY_ID, config.ALPACA_API_SECRET_KEY,
                                       feed=config.ALPACA_DATA_FEED)
        print('Alpaca client ready')
    except Exception as e:
        print(f'ERROR: Alpaca client unavailable ({e}) — cannot fetch trades')
        sys.exit(1)

    pairs = load_calendar_pairs()
    print(f'Found {len(pairs)} symbol/date pairs in calendar data\n')

    fetched = skipped = failed = 0

    since = None
    if not args.refresh and not args.date and not args.from_date:
        since = latest_cached_date()
        if since:
            print(f'Most recent cache: {since} — skipping earlier dates')
        else:
            print('No existing cache — fetching all dates')

    today = date.today().isoformat()

    from collections import defaultdict
    by_date = defaultdict(list)
    for date_str, sym in pairs:
        by_date[date_str].append(sym)

    cur_year = str(datetime.now().year)

    for date_str in sorted(by_date):
        if args.date and date_str != args.date:
            skipped += len(by_date[date_str])
            continue

        if args.from_date and date_str < args.from_date:
            skipped += len(by_date[date_str])
            continue

        if not args.all and not args.date and not args.from_date and not args.refresh:
            if not date_str.startswith(cur_year):
                skipped += len(by_date[date_str])
                continue

        if since and date_str < since:
            skipped += len(by_date[date_str])
            continue

        path = day_path(date_str)
        force = args.refresh or bool(args.date or args.symbol) or date_str == today

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
            bars = _fetch_alpaca(sym, date_str)

            if bars:
                day_cache[sym] = bars
                changed = True
                print(f'{len(bars)} bars')
                fetched += 1
            else:
                print('no data')
                failed += 1

        if changed:
            os.makedirs(TICK_DIR, exist_ok=True)
            with open(path, 'w') as f:
                json.dump(day_cache, f, separators=(',', ':'))

    print(f'\nFetched: {fetched}  |  Skipped (cached): {skipped}  |  No data: {failed}')


if __name__ == '__main__':
    main()
