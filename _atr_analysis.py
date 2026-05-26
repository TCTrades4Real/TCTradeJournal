"""
5-bar ATR on 2-min chart at trade entry: best vs worst trades.
ATR window = 5 bars leading UP TO (and including) the entry bar.
"""

import json
import os
import glob
from datetime import datetime, timedelta
from statistics import mean, median, stdev

CALENDAR_DIR = "dashboard/calendar"
OHLCV_DIR = "dashboard/ohlcv"
TOP_N_PCT = 20  # top/bottom percentile


def load_all_roundtrips():
    rts = []
    for path in glob.glob(os.path.join(CALENDAR_DIR, "calendar_data_*.json")):
        year = int(os.path.basename(path).replace("calendar_data_", "").replace(".json", ""))
        with open(path) as f:
            data = json.load(f)[str(year)]
        for month_str, month_data in data.items():
            month = int(month_str)
            for day_str, day_data in month_data.get("days", {}).items():
                day = int(day_str)
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                for rt in day_data.get("roundtrips", []):
                    rt = dict(rt)
                    rt["date"] = date_str
                    rts.append(rt)
    return rts


def load_ohlcv(date_str):
    path = os.path.join(OHLCV_DIR, f"ohlcv_{date_str}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def resample_to_2min(bars_1min):
    """bars_1min: list of dicts {time (unix), open, high, low, close, volume} sorted asc"""
    if not bars_1min:
        return []
    bars_2min = []
    i = 0
    while i < len(bars_1min):
        b1 = bars_1min[i]
        t1 = b1["time"]
        o = b1["open"]
        h = b1["high"]
        l = b1["low"]
        c = b1["close"]
        v = b1.get("volume", 0)
        if i + 1 < len(bars_1min):
            b2 = bars_1min[i + 1]
            h = max(h, b2["high"])
            l = min(l, b2["low"])
            c = b2["close"]
            v += b2.get("volume", 0)
            i += 2
        else:
            i += 1
        bars_2min.append({"time": t1, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return bars_2min


def compute_atr(bars, period=5):
    """True range ATR over `period` bars (simple average, not EMA)."""
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]
        l = bars[i]["low"]
        prev_c = bars[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if len(trs) < period:
        return None
    return mean(trs[-period:])


def parse_time_hhmm(t_str):
    """Parse HH:MM or HH:MM:SS to total minutes since midnight"""
    parts = t_str.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def get_entry_atr(rt, ohlcv_day, date_str):
    symbol = rt["symbol"]
    entry_time = rt.get("entry_time", "")
    if not entry_time or symbol not in ohlcv_day:
        return None

    bars_raw = ohlcv_day[symbol]
    if not bars_raw or not isinstance(bars_raw, list):
        return None

    try:
        bars_sorted = sorted(bars_raw, key=lambda b: b["time"])
    except Exception:
        return None

    # resample 1-min -> 2-min
    bars_2 = resample_to_2min(bars_sorted)
    if len(bars_2) < 3:
        return None

    # entry time as total minutes since midnight
    entry_mins = parse_time_hhmm(entry_time)

    # convert unix timestamps to minutes since midnight (Eastern = UTC-4 or UTC-5)
    # use local offset: first bar time modulo 86400 gives seconds since UTC midnight
    # easier: just use relative ordering - find bar closest to entry_time
    # assume bars are in order and timestamps are UTC; market opens 9:30 ET
    # ET offset: -4 (EDT) or -5 (EST)
    # Use heuristic: find offset from first bar
    first_unix = bars_sorted[0]["time"]
    # market open is 09:30 ET. Try both offsets and pick best fit.
    # Actually: use the date to determine offset roughly
    # For simplicity, try -4h (EDT, March-Nov) and -5h (EST)
    from datetime import datetime, timezone, timedelta
    dt_utc = datetime.fromtimestamp(first_unix, tz=timezone.utc)
    # EDT runs Mar second Sun to Nov first Sun - approximate
    month = int(date_str.split("-")[1])
    et_offset = -4 if 3 <= month <= 11 else -5
    dt_et = dt_utc + timedelta(hours=et_offset)
    bar0_mins = dt_et.hour * 60 + dt_et.minute

    # find entry bar index
    entry_idx = None
    for idx, bar in enumerate(bars_2):
        bar_unix = bar["time"]
        bar_dt_utc = datetime.fromtimestamp(bar_unix, tz=timezone.utc)
        bar_dt_et = bar_dt_utc + timedelta(hours=et_offset)
        bar_mins = bar_dt_et.hour * 60 + bar_dt_et.minute
        if bar_mins >= entry_mins:
            entry_idx = idx
            break

    if entry_idx is None:
        entry_idx = len(bars_2) - 1

    # window: 6 bars ending at entry_idx (gives 5 TR pairs)
    start_idx = max(0, entry_idx - 5)
    window = bars_2[start_idx : entry_idx + 1]

    period = min(5, len(window) - 1)
    if period < 1:
        return None
    return compute_atr(window, period=period)


def main():
    print("Loading roundtrips...")
    rts = load_all_roundtrips()
    print(f"Total roundtrips: {len(rts)}")

    # only trades with OHLCV available (post-2025-09)
    results = []
    missing_ohlcv = 0
    missing_symbol = 0
    missing_atr = 0

    ohlcv_cache = {}

    for rt in rts:
        date = rt["date"]
        if date not in ohlcv_cache:
            ohlcv_cache[date] = load_ohlcv(date)
        ohlcv_day = ohlcv_cache[date]

        if ohlcv_day is None:
            missing_ohlcv += 1
            continue

        atr = get_entry_atr(rt, ohlcv_day, date)
        if atr is None:
            missing_atr += 1
            continue

        results.append({
            "date": date,
            "symbol": rt["symbol"],
            "pnl": rt["pnl"],
            "qty": rt.get("qty", 1),
            "entry_time": rt.get("entry_time"),
            "atr_2min_5bar": atr,
        })

    print(f"Trades with ATR computed: {len(results)}")
    print(f"Missing OHLCV file: {missing_ohlcv}")
    print(f"Missing ATR (insufficient bars): {missing_atr}")
    print()

    if not results:
        print("No results — no OHLCV data overlaps trade dates.")
        return

    # sort by pnl
    results.sort(key=lambda x: x["pnl"])
    n = len(results)
    cutoff = max(1, int(n * TOP_N_PCT / 100))

    worst = results[:cutoff]
    best = results[-cutoff:]
    middle = results[cutoff:-cutoff]

    def stats(group, label):
        atrs = [x["atr_2min_5bar"] for x in group]
        pnls = [x["pnl"] for x in group]
        print(f"=== {label} (n={len(group)}) ===")
        print(f"  PnL range:   ${min(pnls):.2f} to ${max(pnls):.2f}  (median ${median(pnls):.2f})")
        print(f"  ATR mean:    ${mean(atrs):.4f}")
        print(f"  ATR median:  ${median(atrs):.4f}")
        if len(atrs) > 1:
            print(f"  ATR stdev:   ${stdev(atrs):.4f}")
        print(f"  ATR min:     ${min(atrs):.4f}")
        print(f"  ATR max:     ${max(atrs):.4f}")
        # bucket distribution
        buckets = [(0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.50), (0.50, 1.0), (1.0, 999)]
        print("  ATR distribution:")
        for lo, hi in buckets:
            cnt = sum(1 for a in atrs if lo <= a < hi)
            pct = cnt / len(atrs) * 100
            print(f"    ${lo:.2f}-${hi:.2f}: {cnt:4d} ({pct:.1f}%)")
        print()

    stats(best, f"BEST {TOP_N_PCT}% trades (top by PnL)")
    stats(worst, f"WORST {TOP_N_PCT}% trades (bottom by PnL)")
    if middle:
        stats(middle, f"MIDDLE {100 - 2*TOP_N_PCT}% trades")

    # ratio analysis
    best_atrs = [x["atr_2min_5bar"] for x in best]
    worst_atrs = [x["atr_2min_5bar"] for x in worst]
    ratio = mean(best_atrs) / mean(worst_atrs) if mean(worst_atrs) else float("inf")
    print(f"=== EDGE SUMMARY ===")
    print(f"  Best avg ATR:  ${mean(best_atrs):.4f}")
    print(f"  Worst avg ATR: ${mean(worst_atrs):.4f}")
    print(f"  Ratio (best/worst): {ratio:.3f}")
    if ratio > 1.15:
        print("  >> Best trades entered in HIGHER volatility environments")
    elif ratio < 0.85:
        print("  >> Best trades entered in LOWER volatility environments")
    else:
        print("  >> ATR similar across best/worst — no strong ATR edge detected")

    # top 10 best and worst
    print()
    print("--- Top 10 BEST trades ---")
    for x in reversed(best[-10:]):
        print(f"  {x['date']} {x['symbol']:6s} {x['entry_time']}  PnL=${x['pnl']:8.2f}  ATR=${x['atr_2min_5bar']:.4f}")

    print()
    print("--- Top 10 WORST trades ---")
    for x in worst[:10]:
        print(f"  {x['date']} {x['symbol']:6s} {x['entry_time']}  PnL=${x['pnl']:8.2f}  ATR=${x['atr_2min_5bar']:.4f}")

    # ATR quartile PnL analysis
    print()
    print("=== PnL by ATR Quartile ===")
    results_sorted_atr = sorted(results, key=lambda x: x["atr_2min_5bar"])
    q = len(results_sorted_atr) // 4
    quartiles = [
        results_sorted_atr[:q],
        results_sorted_atr[q:2*q],
        results_sorted_atr[2*q:3*q],
        results_sorted_atr[3*q:],
    ]
    for i, grp in enumerate(quartiles):
        atrs = [x["atr_2min_5bar"] for x in grp]
        pnls = [x["pnl"] for x in grp]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        print(f"  Q{i+1} ATR ${min(atrs):.4f}-${max(atrs):.4f}:  "
              f"avg PnL=${mean(pnls):.2f}  total=${total_pnl:.2f}  "
              f"win%={wins/len(pnls)*100:.1f}%  n={len(grp)}")


if __name__ == "__main__":
    main()
