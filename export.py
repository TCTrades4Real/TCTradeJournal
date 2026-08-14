import sys
import os
# When running on the server, pick up vendored packages from ~/app/vendor/
_vendor = os.path.join(os.path.dirname(__file__), 'vendor')
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)

import csv
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from requests.auth import HTTPBasicAuth
from utilities import config

# ────────────────────────────────────────────────
#   Tradervue Configuration
# ────────────────────────────────────────────────
USERNAME = config.TRADERVUE_USERNAME
PASSWORD = config.TRADERVUE_PASSWORD
BASE_URL = "https://app.tradervue.com/api/v1"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": config.TRADERVUE_USER_AGENT
}

POLL_INTERVAL_SECONDS = 1      # How often to check
MAX_POLL_ATTEMPTS = 120        # ~5 minutes max wait
# ────────────────────────────────────────────────

_EASTERN = ZoneInfo('America/New_York')

def _to_eastern(utc_str: str, fmt) -> str:
    """Convert a UTC timestamp string to Eastern Time.
    Pass fmt=None to return isoformat(timespec='seconds') (used by Tradervue).
    Pass a strftime format string to get custom format (used by MrProfit).
    Normalizes '+0000' -> '+00:00' for fromisoformat compatibility.
    """
    normalized = utc_str.replace('+0000', '+00:00')
    utc_time = datetime.fromisoformat(normalized)
    et_time = utc_time.astimezone(_EASTERN)
    if fmt is None:
        return et_time.isoformat(timespec='seconds')
    return et_time.strftime(fmt)


# ────────────────────────────────────────────────
#   Last Trade Detection
# ────────────────────────────────────────────────

def get_last_trade_time(base_folder):
    """
    Scan all AccountStatement CSVs in base_folder and return the latest
    execution datetime as a UTC datetime. Returns None if no files found.
    Checks the 3 most recently dated files so trades that span day boundaries
    are handled correctly.
    """
    import glob
    pattern = os.path.join(base_folder, '*-AccountStatement.csv')
    files = glob.glob(pattern)
    if not files:
        return None

    # Sort descending by filename date prefix (YYYY-MM-DD)
    files.sort(reverse=True)

    latest_dt = None
    _ET = ZoneInfo('America/New_York')

    for filepath in files[:3]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                for i, row in enumerate(reader):
                    if i < 3:          # skip: title row, blank row, header row
                        continue
                    # Data rows: col 0 = blank (A), col 1 = Exec Time (B)
                    if len(row) < 2 or not row[1].strip():
                        continue
                    try:
                        dt_et = datetime.strptime(row[1].strip(), '%m/%d/%Y %H:%M:%S')
                        dt_utc = dt_et.replace(tzinfo=_ET).astimezone(timezone.utc)
                        if latest_dt is None or dt_utc > latest_dt:
                            latest_dt = dt_utc
                    except ValueError:
                        continue
        except Exception:
            continue

    return latest_dt


# ────────────────────────────────────────────────
#   MrProfit Export
# ────────────────────────────────────────────────

def get_unique_filename(base_path, base_name, extension=".csv"):
    """
    If file exists, append -1, -2, etc. until unique name is found
    """
    full_path = os.path.join(base_path, base_name + extension)
    counter = 1
    while os.path.exists(full_path):
        full_path = os.path.join(base_path, f"{base_name}-{counter}{extension}")
        counter += 1
    return full_path

def get_trade_export(client, account_hash, start_date_utc=None, end_date_utc=None, account_label=""):
    """
    Fetch trades and write CSV with exact layout - no pandas
    - A1: "Account Trade History"
    - A2: blank
    - B2: headers (Exec Time, Spread, etc.)
    - B3+: trade data
    Exec Time format: MM/DD/YYYY HH:MM:SS (Eastern Time)
    Filename: YYYY-MM-DD-{account_label}-AccountStatement.csv (using first trade date)
             If exists → appends -1, -2, etc.
    """
    if start_date_utc is None:
        start_date_utc = datetime.now(timezone.utc) - timedelta(days=3)
    if end_date_utc is None:
        end_date_utc = datetime.now(timezone.utc)

    start_date = start_date_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    end_date = end_date_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    print("Fetching transactions from Schwab...")
    response = client.transactions(account_hash, start_date, end_date, types="TRADE", symbol=None)
    raw_data = response.json()

    if not isinstance(raw_data, list):
        print(f"Error fetching transactions ({response.status_code}): {raw_data}")
        return

    if not raw_data:
        print("No transactions found.")
        return

    # Collect trade rows
    trades = []

    for activity in raw_data:
        transfer_items = activity.get('transferItems', [])
        if not transfer_items:
            continue

        main_leg = next(
            (item for item in transfer_items if 'positionEffect' in item),
            None
        )
        if main_leg is None:
            continue

        instrument = main_leg.get('instrument', {})
        symbol = instrument.get('symbol', '')

        amount = main_leg.get('amount', 0)

        trade = [
            activity.get('time'),                    # Exec Time (UTC)
            'STOCK',
            'BUY' if amount > 0 else 'SELL',
            amount,                                  # positive buy, negative sell
            'TO CLOSE' if main_leg.get('positionEffect') == 'CLOSING' else 'TO OPEN',
            symbol,
            '',
            '',
            'STOCK',
            main_leg.get('price', ''),
            main_leg.get('price', ''),
            'MKT'
        ]
        trades.append(trade)

    if not trades:
        print("No equity trades found.")
        return

    # Convert times to Eastern Time with MM/DD/YYYY format
    for trade in trades:
        trade[0] = _to_eastern(trade[0], '%m/%d/%Y %H:%M:%S')

    # ─── Determine base filename using first trade date ───
    if trades:
        first_trade_time_str = trades[0][0]  # MM/DD/YYYY format
        first_date = datetime.strptime(first_trade_time_str.split()[0], '%m/%d/%Y')
        date_str = first_date.strftime('%Y-%m-%d')
    else:
        # Fallback if no trades
        date_str = datetime.now().strftime("%Y-%m-%d")

    base_folder = os.environ.get('TRADE_DATA_DIR', config.MR_PROFIT_BASE_FOLDER)
    prefix = f"{date_str}-{account_label}-" if account_label else f"{date_str}-"
    base_name = f"{prefix}AccountStatement"
    full_path = get_unique_filename(base_folder, base_name)

    print(f"Saving to: {full_path}")

    try:
        os.makedirs(base_folder, exist_ok=True)

        with open(full_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)

            # Row 1: title in A1
            writer.writerow(['Account Trade History'])

            # Row 2: blank in A2
            writer.writerow([])

            # Row 3: headers starting in B3
            headers = [
                'Exec Time', 'Spread', 'Side', 'Qty', 'Pos Effect',
                'Symbol', 'Exp', 'Strike', 'Type', 'Price', 'Net Price', 'Order Type'
            ]
            writer.writerow([''] + headers)  # empty A3, then headers

            # Row 4+: data starting in B4
            for trade in trades:
                writer.writerow([''] + trade)  # empty A, then trade data

        print(f"\nSUCCESS! Saved {len(trades)} trades")
        print(f"File: {full_path}")

        # Optional: show preview
        print("\nFirst few rows (CSV preview):")
        with open(full_path, 'r') as f:
            for i, line in enumerate(f):
                if i < 6:
                    print(line.strip())

    except Exception as e:
        print("\nError saving file:")
        print(e)


# ────────────────────────────────────────────────
#   Tradervue Export
# ────────────────────────────────────────────────

def export_to_tradervue(client, account_hash, start_date_utc=None, end_date_utc=None):
    """
    Fetch trades and write CSV with exact layout - no pandas
    - A1: "Account Trade History"
    - A2: blank
    - B2: headers (Exec Time, Spread, etc.)
    - B3+: trade data
    Exec Time format: MM/DD/YYYY HH:MM:SS (Eastern Time)
    Filename: YYYY-MM-DD-AccountStatement.csv (using first trade date)
             If exists → appends -1, -2, etc.
    """
    if start_date_utc is None:
        start_date_utc = datetime.now(timezone.utc) - timedelta(days=3)
    if end_date_utc is None:
        end_date_utc = datetime.now(timezone.utc)

    start_date = start_date_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    end_date = end_date_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    print("Fetching transactions from Schwab...")
    response = client.transactions(account_hash, start_date, end_date, types="TRADE", symbol=None)
    raw_data = response.json()

    if not isinstance(raw_data, list):
        print(f"Error fetching transactions ({response.status_code}): {raw_data}")
        return

    trade_executions = []

    for activity in raw_data:
        if activity.get('type') != 'TRADE':
            continue

        # Find the main equity/ETF instrument (skip currency fee items)
        equity_item = None
        for item in activity['transferItems']:
            asset_type = item['instrument'].get('assetType')
            if asset_type in ('EQUITY', 'COLLECTIVE_INVESTMENT'):
                equity_item = item
                break

        if not equity_item:
            continue

        # Convert UTC time to Eastern Time (ISO 8601 format for Tradervue)
        datetime_str = _to_eastern(activity['time'], None)

        symbol = equity_item['instrument']['symbol']
        quantity = int(equity_item['amount'])           # positive = buy, negative = sell
        price = f"{equity_item['price']:.4f}"

        # Extract fees from transaction items
        commission = "0.00"
        transfee = "0.00"
        ecnfee = "0.00"

        for item in activity['transferItems']:
            asset_type = item['instrument'].get('assetType')
            if asset_type == 'CASH':
                description = item['instrument'].get('symbol', '')
                amount = f"{abs(float(item['amount'])):.2f}"

                if 'COMMISSION' in description.upper():
                    commission = amount
                elif 'TRANSFER' in description.upper():
                    transfee = amount
                elif 'ECN' in description.upper():
                    ecnfee = amount

        trade_executions.append({
            "datetime":  datetime_str,
            "symbol":    symbol,
            "quantity":  str(quantity),
            "price":     price,
            "option":    "",
            "commission": commission,
            "transfee":   transfee,
            "ecnfee":     ecnfee
        })

    # Sort by time (oldest → newest)
    trade_executions.sort(key=lambda x: x['datetime'])

    success = create_import(
        executions=trade_executions,
        allow_duplicates=False
    )

    if success:
        print("\n✓ Import completed successfully!")
    else:
        print("\n✗ Import had issues or failed.")

def get_import_status():
    """Fetch current import status. Returns dict or None on error."""
    url = f"{BASE_URL}/imports"
    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(USERNAME, PASSWORD),
            headers=HEADERS,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error checking status: {e}")
        return None


def wait_for_import_completion():
    """
    Poll the import status until it's no longer 'queued' or 'processing'.
    Returns final status dict when done, or None if polling failed.
    """
    print("Waiting for import to complete...")

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        status_data = get_import_status()

        if status_data is None:
            print("Failed to get status. Stopping poll.")
            return None

        status = status_data.get("status")
        print(f"[{attempt}/{MAX_POLL_ATTEMPTS}] Status: {status}")

        if status in ("succeeded", "failed"):
            print("\nImport finished!")
            if status == "succeeded":
                info = status_data.get("info", {})
                print("Success details:")
                print(f"  Executions imported: {info.get('exec_count', 0)}")
                print(f"  Duplicates skipped: {info.get('duplicate_count', 0)}")
                print(f"  Over quota skipped: {info.get('overquota_count', 0)}")
            else:  # failed
                info = status_data.get("info", {})
                print("Import FAILED:")
                print(f"  Error: {info.get('error_description', 'Unknown error')}")
                if "error_execnumber" in info:
                    print(f"  Problem at execution #{info['error_execnumber']}")

            return status_data

        elif status not in ("queued", "processing", "ready"):
            print(f"Unexpected status: {status}")
            return status_data

        # Still in progress → wait
        time.sleep(POLL_INTERVAL_SECONDS)

    print(f"Timed out after {MAX_POLL_ATTEMPTS} attempts (~{MAX_POLL_ATTEMPTS*POLL_INTERVAL_SECONDS/60:.1f} min)")
    return None


def create_import(executions, allow_duplicates=False, overlay_commissions=False,
                 tags=None, account_tag=None):
    """
    Create a new import and wait for it to finish processing.
    Returns True if succeeded, False otherwise.
    """
    if tags is None:
        tags = []

    payload = {
        "allow_duplicates": str(allow_duplicates).lower(),
        "overlay_commissions": str(overlay_commissions).lower(),
        "tags": tags,
        "executions": executions
    }
    if account_tag:
        payload["account_tag"] = account_tag

    url = f"{BASE_URL}/imports"

    try:
        response = requests.post(
            url,
            auth=HTTPBasicAuth(USERNAME, PASSWORD),
            headers=HEADERS,
            json=payload,           # cleaner than data=json.dumps()
            timeout=15
        )

        if response.status_code == 200:
            print("Import queued successfully!")
            print(json.dumps(response.json(), indent=2))
        elif response.status_code == 424:
            print("Another import is already in progress.")
            print(response.text)
            return False
        else:
            print(f"Failed to queue import ({response.status_code}):")
            print(response.text)
            return False

    except requests.RequestException as e:
        print(f"Request error while queuing: {e}")
        return False

    # ── Now wait for it to finish ───────────────────────
    final_status = wait_for_import_completion()

    if final_status and final_status.get("status") == "succeeded":
        return True
    else:
        return False


# ────────────────────────────────────────────────
#   CLI Entry Point
# ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import schwabdev
    parser = argparse.ArgumentParser(description="Export trades from Schwab")
    parser.add_argument("--tradervue", action="store_true", help="Export to Tradervue API")
    parser.add_argument("--mrprofit", action="store_true", help="Export to MrProfit CSV")
    parser.add_argument("--start", metavar="YYYY-MM-DD", help="Start date (Eastern). Defaults to 4 days ago.")
    parser.add_argument("--end", metavar="YYYY-MM-DD", help="End date (Eastern). Defaults to today.")
    parser.add_argument("--reauth", action="store_true", help="Force re-authorization by deleting cached token file.")
    args = parser.parse_args()

    if not args.tradervue and not args.mrprofit:
        args.mrprofit = True

    _ET = ZoneInfo('America/New_York')

    if args.start:
        start_date_utc = datetime.strptime(args.start, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, tzinfo=_ET
        ).astimezone(timezone.utc)
    else:
        last_trade = get_last_trade_time(os.environ.get('TRADE_DATA_DIR', config.MR_PROFIT_BASE_FOLDER))
        if last_trade:
            start_date_utc = last_trade + timedelta(seconds=1)
            print(f"Last recorded trade: {last_trade.astimezone(_ET).strftime('%m/%d/%Y %H:%M:%S')} ET — fetching from there.")
        else:
            now_et = datetime.now(_ET)
            start_date_utc = datetime(now_et.year, now_et.month, 1, tzinfo=_ET).astimezone(timezone.utc)
            print("No existing trade files found — fetching from start of current month.")

    if args.end:
        end_date_utc = datetime.strptime(args.end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=_ET
        ).astimezone(timezone.utc)
    else:
        end_date_utc = datetime.now(timezone.utc)

    _tokens_db = os.path.expanduser("~/.schwabdev/tokens.db")
    if args.reauth and os.path.exists(_tokens_db):
        os.remove(_tokens_db)
        print(f"Deleted token file: {_tokens_db}")

    client = schwabdev.Client(
        config.SCHWAB_API_KEY,
        config.SCHWAB_CLIENT_ID
    )

    # Write account balances and today's PnL for dashboard
    try:
        import pathlib as _pathlib
        from datetime import timezone as _tz

        _today_s = datetime.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        _today_e = datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

        def _acct_pnl(h):
            acct = client.account_details(h).json()
            bal  = acct['securitiesAccount']['currentBalances']['liquidationValue']
            txns = client.transactions(h, _today_s, _today_e, types='TRADE').json()
            pnl  = sum(t.get('netAmount', 0) for t in txns) if txns else 0.0
            return bal, pnl

        _cash_bal,   _cash_pnl   = _acct_pnl(config.cash_account_hash)
        _roth_bal,   _roth_pnl   = _acct_pnl(config.roth_account_hash)

        _bal_path = _pathlib.Path('dashboard/account_balance.json')
        _bal_path.write_text(json.dumps({
            'balance':        _cash_bal,
            'cash_balance':   _cash_bal,
            'cash_pnl_today': _cash_pnl,
            'roth_balance':   _roth_bal,
            'roth_pnl_today': _roth_pnl,
        }))
        print(f"Cash: ${_cash_bal:,.2f}  (today PnL: ${_cash_pnl:+,.2f})")
        print(f"Roth: ${_roth_bal:,.2f}  (today PnL: ${_roth_pnl:+,.2f})")
    except Exception as _e:
        print(f"Warning: could not fetch account balance: {_e}")

    active_accounts = [
        ("Cash",   config.cash_account_hash),
        ("Roth",   config.roth_account_hash),
    ]
    if config.margin_account_hash:
        active_accounts.append(("Margin", config.margin_account_hash))

    if args.tradervue:
        for label, h in active_accounts:
            print(f"\n--- Tradervue export: {label} account ---")
            export_to_tradervue(client, h, start_date_utc, end_date_utc)
    if args.mrprofit:
        for label, h in active_accounts:
            print(f"\n--- MrProfit export: {label} account ---")
            get_trade_export(client, h, start_date_utc, end_date_utc, account_label=label)

    import subprocess, sys
    subprocess.run([sys.executable, "calendar_data.py"], check=True)
    subprocess.run([sys.executable, "fetch_ohlcv.py"], check=True)
    if os.path.exists("fetch_ohlcv_daily.py"):
        subprocess.run([sys.executable, "fetch_ohlcv_daily.py"], check=True)
    else:
        print("fetch_ohlcv_daily.py not found — skipping daily OHLCV step")
    if os.path.exists("compute_mfe_mae.py"):
        subprocess.run([sys.executable, "compute_mfe_mae.py"], check=True)
    else:
        print("compute_mfe_mae.py not found — skipping MFE/MAE step")
    if os.path.exists("backtesting.py"):
        subprocess.run([sys.executable, "backtesting.py", "--export"], check=True)
    else:
        print("backtesting.py not found — skipping backtest step")

    import ftplib, pathlib
    from datetime import date as _date

    _sftp_cfg = json.loads(pathlib.Path(".vscode/sftp.json").read_text())
    _host     = _sftp_cfg["host"]
    _port     = _sftp_cfg.get("port", 21)
    _user     = _sftp_cfg["username"]
    _password = _sftp_cfg["password"]
    _remote   = _sftp_cfg["remotePath"].rstrip("/")
    _cal_dir  = pathlib.Path("dashboard/calendar")
    _ohlcv_dir = pathlib.Path("dashboard/ohlcv")
    _manifest  = _ohlcv_dir / ".uploaded"
    _today_ohlcv = f"ohlcv_{_date.today().isoformat()}.json"

    # Load manifest of already-uploaded ohlcv files
    _uploaded = set()
    if _manifest.exists():
        _uploaded = set(_manifest.read_text().splitlines())

    # Determine which ohlcv files to upload
    _ohlcv_to_upload = [
        f for f in sorted(_ohlcv_dir.glob("ohlcv_*.json"))
        if f.name not in _uploaded or f.name == _today_ohlcv
    ]

    print("\nUploading to FTP...")
    with ftplib.FTP() as ftp:
        ftp.connect(_host, _port)
        ftp.login(_user, _password)

        # Upload calendar files
        try:
            ftp.mkd(f"{_remote}/calendar")
        except ftplib.error_perm:
            pass
        for f in sorted(_cal_dir.glob("*.json")):
            with open(f, "rb") as fh:
                ftp.storbinary(f"STOR {_remote}/calendar/{f.name}", fh)
            print(f"  Uploaded: calendar/{f.name}")

        # Upload ohlcv files
        try:
            ftp.mkd(f"{_remote}/ohlcv")
        except ftplib.error_perm:
            pass
        newly_uploaded = []
        for f in _ohlcv_to_upload:
            with open(f, "rb") as fh:
                ftp.storbinary(f"STOR {_remote}/ohlcv/{f.name}", fh)
            print(f"  Uploaded: ohlcv/{f.name}")
            newly_uploaded.append(f.name)

        # Upload backtest files
        try:
            ftp.mkd(f"{_remote}/backtest")
        except ftplib.error_perm:
            pass
        for f in sorted(pathlib.Path("dashboard/backtest").glob("*.json")):
            with open(f, "rb") as fh:
                ftp.storbinary(f"STOR {_remote}/backtest/{f.name}", fh)
            print(f"  Uploaded: backtest/{f.name}")

        # Upload dashboard HTML + JSON files
        for local, remote_name in [
            ("dashboard/account_balance.json", "account_balance.json"),
            ("dashboard/candlestick-chart.html", "candlestick-chart.html"),
            ("dashboard/monte_carlo.html",     "monte_carlo.html"),
            ("dashboard/reports.html",         "reports.html"),
            ("dashboard/day.html",             "day.html"),
            ("dashboard/trades.html",          "trades.html"),
            ("dashboard/index.html",           "index.html"),
            ("dashboard/nav.js",               "nav.js"),
            ("dashboard/auth.js",              "auth.js"),
        ]:
            _lp = pathlib.Path(local)
            if _lp.exists():
                with open(_lp, "rb") as fh:
                    ftp.storbinary(f"STOR {_remote}/{remote_name}", fh)
                print(f"  Uploaded: {remote_name}")

    # Update manifest
    _uploaded.update(newly_uploaded)
    _manifest.write_text("\n".join(sorted(_uploaded)))

    print("FTP upload complete.")
