"""
import_tradezero.py — pull today's filled TradeZero LIVE orders, merge them with the
existing Schwab executions, and rebuild the calendar JSONs so TradeZero trades show up
as a "TZLive" account bucket alongside Cash/Roth (real P&L, full dashboard parity).

TradeZero paper trades are intentionally NOT handled here (planned as a separate isolated
overlay covering only paper trades) — this script only ever touches real money.

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

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from utilities import config
from utilities.tradezero_client import TradeZeroClient
import calendar_data as cal

_ET = ZoneInfo('America/New_York')

TZ_ACCOUNT_LABEL = 'TZLive'

# TradeZero's /orders endpoint only ever returns TODAY's fills (see tradezero_client.py) —
# there's no historical-range endpoint. Persist every day's fetched executions here so
# the calendar rebuild always has the FULL TZLive history, not just today's fetch.
TZ_EXECS_LOG = os.path.join(_HERE, 'trade_data', 'tz_live_executions.json')


def load_tz_execs_log():
    if not os.path.exists(TZ_EXECS_LOG):
        return []
    with open(TZ_EXECS_LOG) as f:
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
            'account':    TZ_ACCOUNT_LABEL,
        })
    return execs


def save_tz_execs_log(execs):
    raw = [{
        'dt':         e['dt'].isoformat(),
        'symbol':     e['symbol'],
        'qty':        e['qty'],
        'price':      e['price'],
        'pos_effect': e['pos_effect'],
    } for e in execs]
    os.makedirs(os.path.dirname(TZ_EXECS_LOG), exist_ok=True)
    with open(TZ_EXECS_LOG, 'w') as f:
        json.dump(raw, f, indent=2)


def build_live_client():
    return TradeZeroClient(config.TZ_BASE_URL, config.TZ_LIVE_API_KEY_ID,
                            config.TZ_LIVE_API_SECRET_KEY, config.TZ_LIVE_ACCOUNT_ID)


def normalize(orders):
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
            'account':    TZ_ACCOUNT_LABEL,
        })
    return execs


def main():
    parser = argparse.ArgumentParser(description='Import TradeZero live trades into the calendar JSONs')
    args = parser.parse_args()

    client = build_live_client()

    print('Fetching TradeZero live orders (today)...')
    orders = client.get_orders()
    tz_execs_today = normalize(orders)
    print(f'  {len(tz_execs_today)} filled TradeZero live executions')

    today = datetime.now(_ET).date()
    tz_execs = load_tz_execs_log()
    tz_execs = [e for e in tz_execs if e['date'] != today]  # replace today's entries, keep history
    tz_execs.extend(tz_execs_today)
    tz_execs.sort(key=lambda x: x['dt'])
    save_tz_execs_log(tz_execs)
    print(f'  {len(tz_execs)} total TradeZero live executions in history log')

    print(f'Reading Schwab CSVs from: {cal.CSV_FOLDER}')
    schwab_execs = cal.parse_csvs(cal.CSV_FOLDER)
    print(f'  {len(schwab_execs)} Schwab executions')

    executions = sorted(schwab_execs + tz_execs, key=lambda x: x['dt'])
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
