"""
Compares actual fill prices from calendar roundtrips against the theoretical
entry price from the momentum breakout strategy: prev_2m_bar_high * 1.0025.

For each real trade entry:
  1. Find the 2m bar ending just before entry_time
  2. Compute theoretical = prev_2m_bar_high * 1.0025
  3. Slippage = actual_fill - theoretical  (positive = paid more than theory)
"""

import json, os, glob
from datetime import datetime, timezone
from collections import defaultdict

CALENDAR_DIR = "dashboard/calendar"
OHLCV_DIR    = "dashboard/ohlcv"
ENTRY_MULT   = 1.0025
DATE_FROM    = "2025-09-01"   # only analyze trades on/after this date
RTH_ONLY     = True           # only 09:30–16:00 ET entries
MAX_SLIP_ABS = 5.0            # drop rows where |slip| > this (data mismatch)

# ─── helpers ──────────────────────────────────────────────────────────────────

def hhmm_to_seconds(hhmm: str) -> int:
    """'09:31' -> seconds since midnight (local/naive)"""
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60

def bar_time_to_secs(unix_ts: int, date_str: str) -> int:
    """Convert unix timestamp to seconds-since-midnight for given date (ET approx)."""
    # OHLCV timestamps are Unix UTC; Schwab data is Eastern.
    # We only care about the relative ordering within a day, so we convert
    # to seconds-since-midnight using the UTC offset for ET (-4 summer / -5 winter).
    # Detect DST roughly: EDT ends first Sunday of November.
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    # Simple DST check: EDT (UTC-4) Mar second Sun through Nov first Sun
    # Good enough for comparison purposes — we just need intraday order.
    month = dt.month
    if 3 < month < 11:
        offset = -4 * 3600  # EDT
    elif month == 3:
        # After second Sunday
        second_sun = 8 + (6 - datetime(dt.year, 3, 1).weekday()) % 7
        offset = -4 * 3600 if dt.day >= second_sun else -5 * 3600
    elif month == 11:
        first_sun = 1 + (6 - datetime(dt.year, 11, 1).weekday()) % 7
        offset = -5 * 3600 if dt.day >= first_sun else -4 * 3600
    else:
        offset = -5 * 3600  # EST
    local_ts = unix_ts + offset
    return local_ts % 86400  # seconds since midnight local

def make_2m_bars(bars_1m):
    """Aggregate 1m bars into 2m bars (even-minute boundaries)."""
    buckets = defaultdict(list)
    for b in bars_1m:
        buckets[b['time'] // 120 * 120].append(b)
    result = []
    for t in sorted(buckets):
        group = buckets[t]
        result.append({
            'time': t,
            'open':   group[0]['open'],
            'high':   max(b['high'] for b in group),
            'low':    min(b['low']  for b in group),
            'close':  group[-1]['close'],
            'volume': sum(b['volume'] for b in group),
        })
    return result

# ─── main ─────────────────────────────────────────────────────────────────────

slippages = []   # list of dicts
skipped   = 0
matched   = 0

calendar_files = sorted(glob.glob(f"{CALENDAR_DIR}/calendar_data_*.json"))

for cal_file in calendar_files:
    with open(cal_file) as f:
        cal = json.load(f)

    for year_str, year_data in cal.items():
        for month_str, month_data in year_data.items():
            days = month_data.get("days", {})
            for day_str, day_data in days.items():
                date_str = f"{year_str}-{int(month_str):02d}-{int(day_str):02d}"
                ohlcv_file = os.path.join(OHLCV_DIR, f"ohlcv_{date_str}.json")
                if not os.path.exists(ohlcv_file):
                    continue

                with open(ohlcv_file) as f:
                    ohlcv = json.load(f)

                roundtrips = day_data.get("roundtrips", [])
                for rt in roundtrips:
                    sym          = rt["symbol"]
                    entry_time   = rt["entry_time"]   # "HH:MM"
                    actual_fill  = rt["entry_price"]

                    # date filter
                    if date_str < DATE_FROM:
                        skipped += 1
                        continue

                    # RTH filter
                    if RTH_ONLY:
                        es = hhmm_to_seconds(entry_time)
                        if es < hhmm_to_seconds("09:30") or es >= hhmm_to_seconds("16:00"):
                            skipped += 1
                            continue

                    if sym not in ohlcv:
                        skipped += 1
                        continue

                    bars_1m = ohlcv[sym]
                    bars_2m = make_2m_bars(bars_1m)

                    entry_secs = hhmm_to_seconds(entry_time)

                    # Find 2m bars whose close_time <= entry_secs
                    # A 2m bar at bucket T covers [T, T+120); it "closes" at T+120
                    prev_bar = None
                    for b in bars_2m:
                        close_secs = bar_time_to_secs(b['time'] + 120, date_str)
                        if close_secs <= entry_secs:
                            prev_bar = b
                        else:
                            break

                    if prev_bar is None:
                        skipped += 1
                        continue

                    theoretical = prev_bar['high'] * ENTRY_MULT
                    slip = actual_fill - theoretical

                    if abs(slip) > MAX_SLIP_ABS:
                        skipped += 1
                        continue

                    slippages.append({
                        'date':        date_str,
                        'symbol':      sym,
                        'entry_time':  entry_time,
                        'actual':      actual_fill,
                        'theoretical': theoretical,
                        'prev2m_high': prev_bar['high'],
                        'slip':        slip,
                    })
                    matched += 1

# ─── report ───────────────────────────────────────────────────────────────────

print(f"\nMatched: {matched}  |  Skipped (no OHLCV data): {skipped}\n")

if not slippages:
    print("No data to analyze.")
    exit()

slips = sorted(s['slip'] for s in slippages)
n     = len(slips)
avg   = sum(slips) / n
pos   = sum(1 for s in slips if s > 0)
neg   = sum(1 for s in slips if s < 0)
zero  = n - pos - neg

def pct(v): return f"{v/n*100:.1f}%"

print(f"{'Metric':<28} {'Value':>10}")
print("-" * 40)
print(f"{'Trades analyzed':<28} {n:>10}")
print(f"{'Average slippage':<28} {avg:>+10.4f}")
print(f"{'Median slippage':<28} {slips[n//2]:>+10.4f}")
print(f"{'Min (best fill)':<28} {slips[0]:>+10.4f}")
print(f"{'Max (worst fill)':<28} {slips[-1]:>+10.4f}")
print(f"{'P10':<28} {slips[int(n*.10)]:>+10.4f}")
print(f"{'P25':<28} {slips[int(n*.25)]:>+10.4f}")
print(f"{'P75':<28} {slips[int(n*.75)]:>+10.4f}")
print(f"{'P90':<28} {slips[int(n*.90)]:>+10.4f}")
print(f"{'Paid more than theory':<28} {pos:>6} ({pct(pos)})")
print(f"{'Paid less than theory':<28} {neg:>6} ({pct(neg)})")
print(f"{'Exact theory':<28} {zero:>6} ({pct(zero)})")

# Bucket distribution
print("\nSlippage distribution (cents):")
buckets = [
    ("< -$0.10 (great fill)", lambda s: s < -0.10),
    ("-$0.10 to -$0.05",      lambda s: -0.10 <= s < -0.05),
    ("-$0.05 to  $0.00",      lambda s: -0.05 <= s < 0.00),
    (" $0.00 to +$0.05",      lambda s:  0.00 <= s < 0.05),
    ("+$0.05 to +$0.10",      lambda s:  0.05 <= s < 0.10),
    ("+$0.10 to +$0.20",      lambda s:  0.10 <= s < 0.20),
    ("> +$0.20 (bad fill)",   lambda s: s >= 0.20),
]
for label, fn in buckets:
    cnt = sum(1 for s in slips if fn(s))
    bar = "#" * (cnt * 40 // max(n, 1))
    print(f"  {label:<28} {cnt:>4} ({pct(cnt)}) {bar}")

# Worst 10 overfills
print("\nTop 10 worst fills (most slippage paid):")
worst = sorted(slippages, key=lambda x: x['slip'], reverse=True)[:10]
print(f"  {'Date':<12} {'Sym':<8} {'Time':<6} {'Theory':>8} {'Actual':>8} {'Slip':>8}")
for w in worst:
    print(f"  {w['date']:<12} {w['symbol']:<8} {w['entry_time']:<6} "
          f"{w['theoretical']:>8.4f} {w['actual']:>8.4f} {w['slip']:>+8.4f}")

# Best 10 fills
print("\nTop 10 best fills (filled below theory):")
best = sorted(slippages, key=lambda x: x['slip'])[:10]
for b in best:
    print(f"  {b['date']:<12} {b['symbol']:<8} {b['entry_time']:<6} "
          f"{b['theoretical']:>8.4f} {b['actual']:>8.4f} {b['slip']:>+8.4f}")
