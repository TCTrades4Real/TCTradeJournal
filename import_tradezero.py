"""
import_tradezero.py — pull today's filled TradeZero LIVE + PAPER orders, merge them with
the existing Schwab executions, and rebuild the calendar JSONs. Both TZLive and TZPaper
are blended into every calendar total/stat alongside Cash/Roth (calendar_data.py's
build_json() sums pnl across every account for the headline 'pnl' figure, and breaks out
pnl_cash/pnl_roth/pnl_tz/pnl_paper alongside it) — paper trades show up everywhere real
trades do: index/month/day/reports/trades.html and the candlestick chart.

The one thing that stays real-only: `dashboard/account_balance.json` (today's realized
PnL + account equity) is computed from TZLive executions and the live TradeZero account
only — paper P&L can never be part of an actual brokerage equity figure, blend setting
or not.

Paper trades get one extra treatment live trades don't: specific round-trips can be
permanently excluded (see dashboard/paper/paper_excluded.json, written by
dashboard_server.py's delete endpoint) — applied in build_all_executions() before
anything reaches cal.build_and_write(). There's no size-based filtering — every paper
fill TradeZero reports is imported; drop unwanted ones (fat-finger, test noise) by hand
from the chart or trade log.

Known API limitation (confirmed by direct testing, see utilities/tradezero_client.py):
GET .../orders only returns TODAY's orders — there's no working historical endpoint, so
this script must be run same-day (e.g. after your TradeZero session). Re-running later
the same day is safe: calendar_data.py always rebuilds the full calendar JSON from
scratch, so this never double-counts.

Usage:
    python import_tradezero.py             # fetch, merge, rebuild calendar JSONs
"""
import sys
import os
import json
import argparse
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from utilities import config
from utilities.tradezero_client import TradeZeroClient
import calendar_data as cal

_ET = ZoneInfo('America/New_York')

TZ_ACCOUNT_LABEL    = 'TZLive'
PAPER_ACCOUNT_LABEL = 'TZPaper'

# TradeZero's /orders endpoint only ever returns TODAY's fills (see tradezero_client.py) —
# there's no historical-range endpoint. Persist every day's fetched executions here so
# the calendar rebuild always has the FULL history, not just today's fetch.
TZ_EXECS_LOG    = os.path.join(_HERE, 'trade_data', 'tz_live_executions.json')
PAPER_EXECS_LOG = os.path.join(_HERE, 'trade_data', 'tz_paper_executions.json')

PAPER_DIR           = os.path.join(_HERE, 'dashboard', 'paper')
PAPER_EXCLUDED_PATH = os.path.join(PAPER_DIR, 'paper_excluded.json')


def load_execs_log(path, account_label):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        raw = json.load(f)
    execs = []
    for r in raw:
        dt = datetime.fromisoformat(r['dt'])
        execs.append({
            'dt':         dt,
            'date':       dt.date(),
            'symbol':     r['symbol'],
            'qty':        r['qty'],
            'price':      r['price'],
            'pos_effect': r['pos_effect'],
            'account':    account_label,
        })
    return execs


def save_execs_log(path, execs):
    raw = [{
        'dt':         e['dt'].isoformat(),
        'symbol':     e['symbol'],
        'qty':        e['qty'],
        'price':      e['price'],
        'pos_effect': e['pos_effect'],
    } for e in execs]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(raw, f, indent=2)


def build_live_client():
    return TradeZeroClient(config.TZ_BASE_URL, config.TZ_LIVE_API_KEY_ID,
                            config.TZ_LIVE_API_SECRET_KEY, config.TZ_LIVE_ACCOUNT_ID)


def build_paper_client():
    return TradeZeroClient(config.TZ_BASE_URL, config.TZ_PAPER_API_KEY_ID,
                            config.TZ_PAPER_API_SECRET_KEY, config.TZ_PAPER_ACCOUNT_ID)


def normalize(orders, account_label):
    """Convert raw TradeZero order records into the same execution-dict shape
    calendar_data.parse_csvs() produces: {dt, date, symbol, qty (signed), price,
    pos_effect, account}. Only orders with a nonzero filled quantity are kept — that
    excludes Canceled/Rejected orders without needing to check orderStatus directly."""
    execs = []
    for o in orders:
        qty = o.get('executed') or 0
        price = o.get('priceAvg')
        ts = o.get('lastUpdated')
        symbol = o.get('symbol')
        side = o.get('side')
        open_close = o.get('openClose')
        if not qty or not price or not ts or not symbol or side not in ('Buy', 'Sell'):
            continue

        # Schwab executions use naive local-ET datetimes (see calendar_data.parse_csvs) —
        # convert to ET and drop tzinfo so the two sources sort/compare together.
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(_ET).replace(tzinfo=None)
        signed_qty = float(qty) if side == 'Buy' else -float(qty)
        pos_effect = 'TO OPEN' if open_close == 'Open' else 'TO CLOSE' if open_close == 'Close' else ''

        execs.append({
            'dt':         dt,
            'date':       dt.date(),
            'symbol':     symbol,
            'qty':        signed_qty,
            'price':      float(price),
            'pos_effect': pos_effect,
            'account':    account_label,
        })
    return execs


def group_round_trips(execs):
    """Group executions into round-trips per symbol: position walks 0 → nonzero → 0.
    A trailing unflattened position (still open) is returned as its own final group.
    Mirrors candlestick-chart.html's groupExecutionsIntoTrades()."""
    by_symbol = defaultdict(list)
    for e in execs:
        by_symbol[e['symbol']].append(e)

    groups = []
    for symbol_execs in by_symbol.values():
        symbol_execs = sorted(symbol_execs, key=lambda e: e['dt'])
        group, position = [], 0.0
        for e in symbol_execs:
            group.append(e)
            position += e['qty']
            if abs(position) < 1e-9:
                groups.append(group)
                group, position = [], 0.0
        if group:
            groups.append(group)
    return groups


def load_paper_exclusions():
    """List of exclusion records, each identifying one whole paper round-trip by its
    first fill's (date, symbol, entry_time 'HH:MM') plus, when known, its exit_time.
    Written by dashboard_server.py when you click-delete a paper trade (chart or
    trades.html); never touched by the real pipeline. Both fields are truncated to
    minute precision — trades.html's roundtrip data doesn't keep seconds (see
    calendar_data.py's daily_roundtrips), so matching tighter than a minute would
    silently never match a delete that came from that page. Older entries written
    before exit_time existed have no 'exit_time' key and match on entry alone."""
    if not os.path.exists(PAPER_EXCLUDED_PATH):
        return []
    with open(PAPER_EXCLUDED_PATH) as f:
        raw = json.load(f)
    out = []
    for e in raw:
        rec = {'date': e['date'], 'symbol': e['symbol'], 'entry_time': e['entry_time'][:5]}
        if e.get('exit_time'):
            rec['exit_time'] = e['exit_time'][:5]
        out.append(rec)
    return out


def filter_excluded_paper_trades(execs, excluded):
    # entry_time alone is only minute precision (see load_paper_exclusions), so two
    # same-symbol, same-day paper round-trips opened in the same calendar minute would
    # otherwise share a key and a delete of one would silently drop both. Matching
    # exit_time too — when the exclusion record has one — narrows that back down to
    # "also closed in the same minute," which in practice never collides. An exclusion
    # with no exit_time (older entries, or a round-trip that was still open when
    # deleted) falls back to entry-only matching, same as before.
    if not excluded:
        return execs
    kept = []
    for group in group_round_trips(execs):
        first = group[0]
        position = sum(e['qty'] for e in group)
        closed = abs(position) < 1e-9
        entry_key = (first['date'].isoformat(), first['symbol'], first['dt'].strftime('%H:%M'))
        exit_time = group[-1]['dt'].strftime('%H:%M') if closed else None

        matched = False
        for e in excluded:
            if (e['date'], e['symbol'], e['entry_time']) != entry_key:
                continue
            if 'exit_time' in e:
                if exit_time is not None and e['exit_time'] == exit_time:
                    matched = True
                    break
            else:
                matched = True
                break
        if not matched:
            kept.extend(group)
    kept.sort(key=lambda e: e['dt'])
    return kept


def build_all_executions(tz_execs=None, paper_execs=None):
    """Merge every execution source — Schwab CSVs, TZ live log, TZ paper log (exclude-listed) —
    into one sorted list ready for cal.build_and_write(). Pass
    nothing (as rebuild_calendar() does) to load tz_execs/paper_execs fresh from disk;
    main() instead passes the lists it just built in memory, so it doesn't immediately
    re-read the two log files it just wrote to TZ_EXECS_LOG/PAPER_EXECS_LOG. Otherwise
    pure disk reads, no TradeZero API calls, so dashboard_server.py can call this too
    (via rebuild_calendar()) right after a paper-trade delete without needing live
    credentials."""
    schwab_execs = cal.parse_csvs(cal.CSV_FOLDER)
    if tz_execs is None:
        tz_execs = load_execs_log(TZ_EXECS_LOG, TZ_ACCOUNT_LABEL)
    if paper_execs is None:
        paper_execs = load_execs_log(PAPER_EXECS_LOG, PAPER_ACCOUNT_LABEL)
    paper_execs = filter_excluded_paper_trades(paper_execs, load_paper_exclusions())
    return sorted(schwab_execs + tz_execs + paper_execs, key=lambda x: x['dt'])


def rebuild_calendar():
    """Rebuild dashboard/calendar/*.json from whatever's currently on disk (no API calls)
    — used by dashboard_server.py right after a paper-trade delete so the blended totals
    update immediately instead of waiting for the next scheduled import."""
    cal.build_and_write(build_all_executions())


def main():
    parser = argparse.ArgumentParser(description='Import TradeZero live trades into the calendar JSONs')
    args = parser.parse_args()

    client = build_live_client()

    # tz_execs stays None on any failure so build_all_executions() below falls back to
    # loading TZ_EXECS_LOG from disk (mirrors the paper block below) — a live-fetch
    # failure must not block the paper fetch/import that follows.
    today = datetime.now(_ET).date()
    tz_execs = None
    try:
        print('Fetching TradeZero live orders (today)...')
        orders = client.get_orders()
        tz_execs_today = normalize(orders, TZ_ACCOUNT_LABEL)
        print(f'  {len(tz_execs_today)} filled TradeZero live executions')

        tz_execs = load_execs_log(TZ_EXECS_LOG, TZ_ACCOUNT_LABEL)
        tz_execs = [e for e in tz_execs if e['date'] != today]  # replace today's entries, keep history
        tz_execs.extend(tz_execs_today)
        tz_execs.sort(key=lambda x: x['dt'])
        save_execs_log(TZ_EXECS_LOG, tz_execs)
        print(f'  {len(tz_execs)} total TradeZero live executions in history log')
    except Exception as e:
        print(f'\nWarning: could not fetch live trades: {e}')
        tz_execs = None

    # Paper trades: fetched and persisted to their own raw log, then blended into the
    # same calendar build as everything else below (exclude-list applied in
    # build_all_executions()) — see the module docstring for what stays real-only.
    # paper_execs stays None on any failure so build_all_executions() below falls back
    # to loading PAPER_EXECS_LOG from disk instead of reusing a partial in-memory list.
    paper_execs = None
    try:
        paper_client = build_paper_client()
        print('\nFetching TradeZero paper orders (today)...')
        paper_orders = paper_client.get_orders()
        paper_execs_today = normalize(paper_orders, PAPER_ACCOUNT_LABEL)
        print(f'  {len(paper_execs_today)} filled TradeZero paper executions')

        paper_execs = load_execs_log(PAPER_EXECS_LOG, PAPER_ACCOUNT_LABEL)
        paper_execs = [e for e in paper_execs if e['date'] != today]
        paper_execs.extend(paper_execs_today)
        paper_execs.sort(key=lambda x: x['dt'])
        save_execs_log(PAPER_EXECS_LOG, paper_execs)
        print(f'  {len(paper_execs)} total TradeZero paper executions in history log')
    except Exception as e:
        print(f'\nWarning: could not fetch paper trades: {e}')
        paper_execs = None

    print(f'\nReading Schwab CSVs from: {cal.CSV_FOLDER}')
    executions = build_all_executions(tz_execs, paper_execs)
    print(f'  {len(executions)} total executions (Schwab + TZ live + TZ paper) feeding the calendar')
    cal.build_and_write(executions)

    try:
        tz_daily_roundtrips, _, _ = cal.compute_daily_details(tz_execs)
        pnl_today = round(sum(rt['pnl'] for rt in tz_daily_roundtrips.get(today, [])), 2)
        account = client.get_account()
        balance_path = os.path.join(_HERE, 'dashboard', 'account_balance.json')
        with open(balance_path, 'w') as f:
            json.dump({'balance': account.get('equity') if account else None,
                       'pnl_today': pnl_today}, f)
        print(f"\nAccount balance: ${account.get('equity'):,.2f}  (today PnL: ${pnl_today:+,.2f})"
              if account else '\nWarning: could not find TZ live account in accounts list')
    except Exception as e:
        print(f'\nWarning: could not fetch account balance: {e}')

    print('\nRefreshing OHLCV / MFE-MAE for any newly-added symbols...')
    subprocess.run([sys.executable, 'fetch_ohlcv.py'], check=True, cwd=_HERE)
    if os.path.exists(os.path.join(_HERE, 'compute_mfe_mae.py')):
        subprocess.run([sys.executable, 'compute_mfe_mae.py'], check=True, cwd=_HERE)


if __name__ == '__main__':
    main()
