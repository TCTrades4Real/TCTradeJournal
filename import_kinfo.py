"""
import_kinfo.py — convert Kinfo monthly .xlsx exports -> Schwab-format CSVs

Each Kinfo round-trip becomes two synthetic executions (entry + exit) that
calendar_data.py can parse via its existing parse_csvs() pipeline.

Usage:
    python import_kinfo.py                        # default Kinfo folder
    python import_kinfo.py --src "C:/path/to/exports"
    python import_kinfo.py --force                # overwrite existing outputs
    python import_kinfo.py --dry-run              # print what would be written
"""

import argparse
import csv
import os

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SRC = os.path.join(
    os.path.expanduser("~"),
    "Documents", "DayTrading", "Trade History",
    "Kinfo Archive - DO NOT DELETE", "Monthly Exports",
)
DEFAULT_DST = os.path.join(_HERE, "trade_data")

CSV_HEADER = [
    "",
    "Exec Time", "Spread", "Side", "Qty", "Pos Effect",
    "Symbol", "Exp", "Strike", "Type", "Price", "Net Price", "Order Type",
]


def kinfo_to_rows(df):
    """Convert a Kinfo DataFrame to Schwab-format CSV data rows (list of lists)."""
    rows = []
    for _, r in df.iterrows():
        symbol = str(r["symbol"]).strip()
        qty    = int(r["quantity"])
        ep     = round(float(r["entryPrice"]), 4)
        xp     = round(float(r["exitPrice"]),  4)
        is_long = str(r["type"]).upper() == "LONG"

        entry_dt = pd.to_datetime(r["entryDateTime"])
        exit_dt  = pd.to_datetime(r["exitDateTime"])

        entry_str = entry_dt.strftime("%m/%d/%Y %H:%M:%S")
        exit_str  = exit_dt.strftime("%m/%d/%Y %H:%M:%S")

        if is_long:
            open_side,  open_qty,  open_effect  = "BUY",  qty,  "TO OPEN"
            close_side, close_qty, close_effect = "SELL", -qty, "TO CLOSE"
        else:  # SHORT
            open_side,  open_qty,  open_effect  = "SELL", -qty, "TO OPEN"
            close_side, close_qty, close_effect = "BUY",  qty,  "TO CLOSE"

        rows.append(["", entry_str, "STOCK", open_side,  open_qty,  open_effect,  symbol, "", "", "STOCK", ep, ep, "MKT"])
        rows.append(["", exit_str,  "STOCK", close_side, close_qty, close_effect, symbol, "", "", "STOCK", xp, xp, "MKT"])

    # sort by exec time ascending (calendar_data.py re-sorts, but nice to have)
    rows.sort(key=lambda x: x[1])
    return rows


def convert_file(xlsx_path, dst_folder, force=False, dry_run=False):
    basename = os.path.splitext(os.path.basename(xlsx_path))[0]  # e.g. 2024-09
    out_name = f"{basename}-kinfo-AccountStatement.csv"
    out_path = os.path.join(dst_folder, out_name)

    if os.path.exists(out_path) and not force:
        print(f"  SKIP (exists): {out_name}")
        return 0

    df = pd.read_excel(xlsx_path)
    if df.empty:
        print(f"  SKIP (empty): {basename}")
        return 0

    data_rows = kinfo_to_rows(df)
    trade_count = len(data_rows) // 2

    if dry_run:
        print(f"  DRY-RUN: {out_name}  ({trade_count} round-trips -> {len(data_rows)} rows)")
        return trade_count

    os.makedirs(dst_folder, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Account Trade History"])
        writer.writerow([])
        writer.writerow(CSV_HEADER)
        writer.writerows(data_rows)

    print(f"  WROTE: {out_name}  ({trade_count} round-trips -> {len(data_rows)} rows)")
    return trade_count


def main():
    parser = argparse.ArgumentParser(description="Convert Kinfo xlsx exports -> Schwab-format CSVs")
    parser.add_argument("--src",     default=DEFAULT_SRC, help="Kinfo Monthly Exports folder")
    parser.add_argument("--dst",     default=DEFAULT_DST, help="Output trade_data folder")
    parser.add_argument("--force",   action="store_true",  help="Overwrite existing output files")
    parser.add_argument("--dry-run", action="store_true",  help="Show what would be written, don't write")
    args = parser.parse_args()

    if not os.path.isdir(args.src):
        print(f"ERROR: source folder not found: {args.src}")
        return

    xlsx_files = []
    for root, _, files in os.walk(args.src):
        for f in files:
            if f.lower().endswith(".xlsx"):
                xlsx_files.append(os.path.join(root, f))
    xlsx_files.sort()

    if not xlsx_files:
        print("No .xlsx files found.")
        return

    print(f"Found {len(xlsx_files)} Kinfo file(s) in: {args.src}")
    print(f"Output -> {args.dst}\n")

    total_trades = 0
    for xlsx_path in xlsx_files:
        total_trades += convert_file(xlsx_path, args.dst, force=args.force, dry_run=args.dry_run)

    print(f"\nDone. {total_trades} total round-trips converted.")
    if not args.dry_run:
        print("Run `python calendar_data.py` to rebuild the dashboard JSON.")


if __name__ == "__main__":
    main()
