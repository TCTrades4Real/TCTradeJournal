"""
alpaca_client.py — thin wrapper around alpaca-py's StockHistoricalDataClient.

Confirmed by direct API testing (2026-08-24), not just docs:
  - Bar fields: symbol, timestamp (tz-aware datetime), open, high, low, close, volume,
    trade_count, vwap.
  - Extended-hours (pre/post market) bars are included by default — no separate flag
    needed, unlike Schwab's needExtendedHoursData.
  - The SIP feed retains at least 5 years of 1-minute history (spot-checked against
    real trading days) — far beyond Schwab's ~10-day window.
"""
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockTradesRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

_ET = ZoneInfo('America/New_York')

# alpaca-py's own RESTClient already retries a 429 internally (3 attempts, fixed 3s wait)
# before giving up and raising APIError with status_code=429 — fine for one isolated hit,
# not enough when a batch fetch burns through many symbol/days back to back and the limit
# stays exhausted across several of those. This wraps each actual network call with its own
# outer exponential-backoff retry on top, so a sustained 429 gets waited out instead of
# surfacing as a skipped symbol/day after only ~9s.
_BACKOFF_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_MAX_SECONDS  = 60.0


def _with_backoff(fn, *args, **kwargs):
    for attempt in range(_BACKOFF_MAX_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            is_last = attempt == _BACKOFF_MAX_ATTEMPTS - 1
            if e.status_code != 429 or is_last:
                raise
            delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** attempt))
            delay += random.uniform(0, _BACKOFF_BASE_SECONDS)  # jitter, avoids thundering herd
            print(f'  [alpaca 429] rate limited, retrying in {delay:.0f}s '
                  f'(attempt {attempt + 1}/{_BACKOFF_MAX_ATTEMPTS})')
            time.sleep(delay)


class AlpacaClient:
    def __init__(self, api_key_id, api_secret_key, feed='sip'):
        self._client = StockHistoricalDataClient(api_key_id, api_secret_key)
        self._feed = feed

    def _bars(self, symbol, timeframe, start, end):
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe,
                                start=start, end=end, feed=self._feed)
        resp = _with_backoff(self._client.get_stock_bars, req)
        data = resp.data if hasattr(resp, 'data') else resp
        return data.get(symbol, [])

    def get_minute_bars(self, symbol, date_str):
        """1-min bars for one ET trading day (04:00-20:00 ET, extended hours included).
        Returns [{time: epoch_seconds, open, high, low, close, volume}, ...]."""
        day = datetime.strptime(date_str, '%Y-%m-%d')
        start = day.replace(hour=4, minute=0, tzinfo=_ET)
        end = day.replace(hour=20, minute=0, tzinfo=_ET)
        bars = self._bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), start, end)
        return [
            {
                'time':   int(b.timestamp.timestamp()),
                'open':   round(b.open, 4),
                'high':   round(b.high, 4),
                'low':    round(b.low, 4),
                'close':  round(b.close, 4),
                'volume': int(b.volume or 0),
            }
            for b in bars
        ]

    def get_trades(self, symbol, date_str):
        """Raw trade prints for one ET trading day (04:00-20:00 ET, extended hours included).
        Returns [{time: epoch_seconds, price, size}, ...] sorted by time."""
        day = datetime.strptime(date_str, '%Y-%m-%d')
        start = day.replace(hour=4, minute=0, tzinfo=_ET)
        end = day.replace(hour=20, minute=0, tzinfo=_ET)
        req = StockTradesRequest(symbol_or_symbols=symbol, start=start, end=end, feed=self._feed)
        resp = _with_backoff(self._client.get_stock_trades, req)
        data = resp.data if hasattr(resp, 'data') else resp
        trades = data.get(symbol, [])
        return [
            {
                'time':  int(t.timestamp.timestamp()),
                'price': round(t.price, 4),
                'size':  int(t.size or 0),
            }
            for t in sorted(trades, key=lambda t: t.timestamp)
        ]

    def get_daily_bars(self, symbol, start_date, end_date):
        """Daily bars for [start_date, end_date] (date objects, inclusive). Returns
        [{time: 'YYYY-MM-DD', open, high, low, close, volume}, ...]."""
        start = datetime.combine(start_date, datetime.min.time())
        end = datetime.combine(end_date, datetime.min.time())
        bars = self._bars(symbol, TimeFrame.Day, start, end)
        return [
            {
                'time':   b.timestamp.astimezone(_ET).date().isoformat(),
                'open':   round(b.open, 4),
                'high':   round(b.high, 4),
                'low':    round(b.low, 4),
                'close':  round(b.close, 4),
                'volume': int(b.volume or 0),
            }
            for b in bars
        ]
