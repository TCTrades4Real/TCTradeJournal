"""
fetch_ohlcv_daily.py — pre-fetch daily OHLCV for symbols called out in watchlists
Writes dashboard/ohlcv_daily/SYMBOL.json per symbol (structure: [bars...], sorted by date)

For each callout in dashboard/watchlist_setups/data.json, the "trade day" is the day
after the callout (he posts watchlists the night before). This fetches a trailing
~2-week window of daily bars ending on that trade day, for the callout's symbol(s),
so watchlists.html can show what the daily chart looked like heading into the trade.

Usage:
    python fetch_ohlcv_daily.py                        # fetch missing symbol/window coverage
    python fetch_ohlcv_daily.py --refresh               # re-fetch every symbol's full window
    python fetch_ohlcv_daily.py --symbol ACXP           # re-fetch one symbol (callout or not);
                                                         # with no callout, windows trailing ~2
                                                         # weeks ending today
    python fetch_ohlcv_daily.py --symbol ACXP --date 2026-03-05
                                                         # ... ending the given date instead

Source: Schwab API only (frequencyType=daily). Unlike 1-min bars, daily history is
retained for years, so once a window is fetched it stays valid — no "only ~10 days"
constraint here.
"""

import json
import os
import sys
import argparse
import ftplib
import urllib.request
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from zoneinfo import ZoneInfo

_ET = ZoneInfo('America/New_York')
WINDOW_DAYS = 14  # trailing window ending on the trade day

# Schwab client — initialised once in main(), shared by fetch helpers
_schwab_client = None

_HERE          = os.path.dirname(__file__)
DASHBOARD      = os.path.join(_HERE, 'dashboard')
OHLCV_DAILY_DIR = os.path.join(DASHBOARD, 'ohlcv_daily')
CALLOUTS_PATH  = os.path.join(DASHBOARD, 'watchlist_setups', 'data.json')
CAL_DIR        = os.path.join(DASHBOARD, 'calendar')
CAL_INDEX_PATH = os.path.join(CAL_DIR, 'calendar_index.json')

# Real edits to watchlists.html land live on tctrades.com via api/setups.php, not in the
# local repo copy of CALLOUTS_PATH (which stays whatever it was last seeded/pushed to).
# Prefer the live copy so this script sees callouts added directly on the site.
LIVE_CALLOUTS_URL = 'https://tctrades.com/watchlist_setups/data.json'


def symbol_path(symbol):
    return os.path.join(OHLCV_DAILY_DIR, f'{symbol}.json')


def ftp_upload(local_path):
    """Upload a single file using credentials from .vscode/sftp.json."""
    try:
        import pathlib
        cfg      = json.loads(pathlib.Path('.vscode/sftp.json').read_text())
        host     = cfg['host']
        port     = cfg.get('port', 21)
        user     = cfg['username']
        password = cfg['password']
        remote   = cfg['remotePath'].rstrip('/')
        filename = os.path.basename(local_path)
        with ftplib.FTP() as ftp:
            ftp.connect(host, port)
            ftp.login(user, password)
            try:
                ftp.mkd(f'{remote}/ohlcv_daily')
            except ftplib.error_perm:
                pass
            with open(local_path, 'rb') as f:
                ftp.storbinary(f'STOR {remote}/ohlcv_daily/{filename}', f)
        print(f'  [ftp] uploaded ohlcv_daily/{filename}')
    except Exception as e:
        print(f'  [ftp error] {e}')


def load_callouts_data():
    """Fetch the live, synced callouts (real edits land on tctrades.com via
    api/setups.php, not the local repo copy). Falls back to the local file if
    the live site can't be reached."""
    try:
        # HostGator's ModSecurity blocks bare/short UAs (including urllib's own default
        # and a plain "Mozilla/5.0") with a 406 — needs something that looks like a real browser.
        ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
        req = urllib.request.Request(LIVE_CALLOUTS_URL, headers={'User-Agent': ua})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f'Using live callouts from {LIVE_CALLOUTS_URL}')
            return json.loads(resp.read())
    except Exception as e:
        print(f'  [warn] could not reach {LIVE_CALLOUTS_URL} ({e})')

    if os.path.exists(CALLOUTS_PATH):
        print(f'Falling back to local {CALLOUTS_PATH}')
        with open(CALLOUTS_PATH) as f:
            return json.load(f)

    print(f'No live data and no local {CALLOUTS_PATH} — nothing to fetch.')
    return None


def load_callout_needs():
    """Return dict: symbol -> (window_start_date, window_end_date) covering every
    callout's trailing window for that symbol, merged to the widest span needed."""
    data = load_callouts_data()
    if data is None:
        return {}

    needs = {}
    for c in data.get('callouts', []):
        date_str = c.get('date')
        if not date_str:
            continue
        try:
            trade_date = datetime.strptime(date_str, '%Y-%m-%d').date() + timedelta(days=1)
        except ValueError:
            continue
        window_start = trade_date - timedelta(days=WINDOW_DAYS)
        for sym in (c.get('symbols') or []):
            sym = sym.upper()
            if sym not in needs:
                needs[sym] = [window_start, trade_date]
            else:
                needs[sym][0] = min(needs[sym][0], window_start)
                needs[sym][1] = max(needs[sym][1], trade_date)
    return {sym: tuple(span) for sym, span in needs.items()}


def load_calendar_needs(current_year_only=True):
    """Return dict: symbol -> (window_start_date, window_end_date) covering every symbol
    actually traded (per calendar_data), so export.py's daily fetch isn't limited to
    watchlist callouts. Bounded to the current year by default to keep routine runs fast —
    `covers()` makes re-fetching cheap either way, so this is just about first-run scope."""
    if not os.path.exists(CAL_INDEX_PATH):
        return {}
    with open(CAL_INDEX_PATH) as f:
        idx = json.load(f)

    cur_year = str(datetime.now().year)
    needs = {}
    for yr in idx.get('years', []):
        if current_year_only and str(yr) != cur_year:
            continue
        yr_path = os.path.join(CAL_DIR, f'calendar_data_{yr}.json')
        if not os.path.exists(yr_path):
            continue
        with open(yr_path) as f:
            cal = json.load(f)
        for year, year_data in cal.items():
            for month, month_data in year_data.items():
                for day, day_data in month_data.get('days', {}).items():
                    try:
                        trade_date = datetime.strptime(
                            f'{year}-{month.zfill(2)}-{day.zfill(2)}', '%Y-%m-%d').date()
                    except ValueError:
                        continue
                    window_start = trade_date - timedelta(days=WINDOW_DAYS)
                    for sym in (day_data.get('symbols') or {}):
                        sym = sym.upper()
                        if sym not in needs:
                            needs[sym] = [window_start, trade_date]
                        else:
                            needs[sym][0] = min(needs[sym][0], window_start)
                            needs[sym][1] = max(needs[sym][1], trade_date)
    return {sym: tuple(span) for sym, span in needs.items()}


def merge_needs(a, b):
    """Union two symbol->(start,end) need dicts, widening spans where both cover a symbol."""
    merged = {sym: list(span) for sym, span in a.items()}
    for sym, (start, end) in b.items():
        if sym not in merged:
            merged[sym] = [start, end]
        else:
            merged[sym][0] = min(merged[sym][0], start)
            merged[sym][1] = max(merged[sym][1], end)
    return {sym: tuple(span) for sym, span in merged.items()}


def _fetch_schwab_daily(symbol, start_date, end_date):
    """Fetch daily bars from Schwab for [start_date, end_date] (inclusive). Returns
    a list of {time: 'YYYY-MM-DD', open, high, low, close, volume} or []."""
    if _schwab_client is None:
        return []
    try:
        dt_start = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=_ET)
        dt_end   = datetime.combine(end_date, datetime.min.time()).replace(
            hour=23, minute=59, tzinfo=_ET)

        resp = _schwab_client.price_history(
            symbol,
            periodType='year',   # Schwab defaults periodType to "day" when omitted, which
                                  # only allows frequencyType=minute — 400s frequencyType=daily
            frequencyType='daily',
            frequency=1,
            startDate=dt_start,
            endDate=dt_end,
            needExtendedHoursData=False,
        )
        resp.raise_for_status()
        candles = resp.json().get('candles') or []
        if not candles:
            return []
        bars = []
        for c in candles:
            dt = datetime.fromtimestamp(c['datetime'] / 1000, tz=timezone.utc).astimezone(_ET)
            bars.append({
                'time':   dt.date().isoformat(),
                'open':   round(c['open'],  4),
                'high':   round(c['high'],  4),
                'low':    round(c['low'],   4),
                'close':  round(c['close'], 4),
                'volume': int(c.get('volume') or 0),
            })
        return bars
    except Exception as e:
        print(f'  [schwab error] {e}', end=' ')
        return []


def load_symbol_cache(symbol):
    path = symbol_path(symbol)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def merge_bars(existing, new_bars):
    """Merge by date, new_bars taking precedence, sorted ascending."""
    by_date = {b['time']: b for b in existing}
    for b in new_bars:
        by_date[b['time']] = b
    return [by_date[d] for d in sorted(by_date)]


def covers(existing, start_date, end_date):
    """True if existing bars already span the full requested window."""
    if not existing:
        return False
    dates = [b['time'] for b in existing]
    return min(dates) <= start_date.isoformat() and max(dates) >= end_date.isoformat()


def main():
    global _schwab_client

    parser = argparse.ArgumentParser(description='Fetch daily OHLCV for watchlist callout symbols, or any symbol on demand')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch every symbol\'s full window, overwriting cached data')
    parser.add_argument('--symbol', help='Fetch this symbol, even if it has no callout')
    parser.add_argument('--date', help='Trade day (YYYY-MM-DD) to center --symbol\'s window on; '
                        'defaults to today. Ignored without --symbol.')
    args = parser.parse_args()

    try:
        import schwabdev
        from utilities import config
        _schwab_client = schwabdev.Client(config.SCHWAB_API_KEY, config.SCHWAB_CLIENT_ID)
        print('Schwab client ready')
    except Exception as e:
        print(f'ERROR: Schwab client unavailable ({e}) — cannot fetch OHLCV')
        sys.exit(1)

    needs = load_callout_needs()
    if args.symbol:
        symbol = args.symbol.upper()
        if args.date:
            try:
                trade_date = datetime.strptime(args.date, '%Y-%m-%d').date()
            except ValueError:
                print(f'ERROR: --date must be YYYY-MM-DD, got {args.date!r}')
                sys.exit(1)
        else:
            trade_date = datetime.now(_ET).date()
        window_start = trade_date - timedelta(days=WINDOW_DAYS)
        if symbol in needs:
            window_start = min(window_start, needs[symbol][0])
            trade_date   = max(trade_date, needs[symbol][1])
        needs = {symbol: (window_start, trade_date)}
        print(f'Fetching {symbol} on demand\n')
    else:
        callout_count = len(needs)
        needs = merge_needs(needs, load_calendar_needs())
        print(f'Found {callout_count} symbol(s) with callouts, {len(needs)} total with traded symbols included\n')

    fetched = skipped = failed = 0

    for symbol in sorted(needs):
        window_start, window_end = needs[symbol]
        existing = [] if args.refresh else load_symbol_cache(symbol)

        if not args.refresh and covers(existing, window_start, window_end):
            print(f'  {symbol}: already covers {window_start} .. {window_end} — skipping')
            skipped += 1
            continue

        print(f'  Fetching {symbol} daily bars {window_start} .. {window_end} ...', end=' ', flush=True)
        bars = _fetch_schwab_daily(symbol, window_start, window_end)

        if bars:
            merged = merge_bars(existing, bars)
            os.makedirs(OHLCV_DAILY_DIR, exist_ok=True)
            path = symbol_path(symbol)
            with open(path, 'w') as f:
                json.dump(merged, f, separators=(',', ':'))
            ftp_upload(path)
            print(f'{len(bars)} bars ({len(merged)} total cached)')
            fetched += 1
        else:
            print('no data')
            failed += 1

    print(f'\nFetched: {fetched}  |  Skipped (cached): {skipped}  |  No data: {failed}')


if __name__ == '__main__':
    main()
