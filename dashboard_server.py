"""
dashboard_server.py — local static file server for dashboard/, plus two write endpoints:

    POST /api/exclude-paper-trade    {date, symbol, entry_time, exit_time?}
    POST /api/exclude-paper-trades   {trades: [{date, symbol, entry_time, exit_time?}, ...]}

Lets candlestick-chart.html's paper-trade "delete" button (single) and trades.html's
multi-select bulk delete remove one or more paper round-trips. Both append to
dashboard/paper/paper_excluded.json, then call import_tradezero.rebuild_calendar() so
every dashboard/calendar/calendar_data_YYYY.json on disk is regenerated from the raw
execution logs (Schwab + TZ live + TZ paper, minus the exclusions) — paper trades are
blended into every real total/stat, so a delete has to rebuild the whole calendar, not
just a paper-only file. The plural endpoint rebuilds once for the whole batch rather
than once per trade — the rebuild re-parses every execution on disk (a few seconds), so
doing it N times for an N-trade selection would be N times slower for no benefit.

Same-origin only (page and API share http://localhost:PORT) — no CORS, no auth needed
for a server that only ever listens on 127.0.0.1.

Usage:
    python dashboard_server.py [port]      # default port 8000
"""
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import import_tradezero as itz

DASHBOARD_DIR = os.path.join(_HERE, 'dashboard')


def _parse_trade_key(body):
    """Pull {date, symbol, entry_time, exit_time?} out of a dict, normalized to minute
    precision (see the module docstring / filter_excluded_paper_trades() for why).
    Raises ValueError/KeyError on anything malformed — caller turns that into a 400."""
    date, symbol, entry_time = body['date'], body['symbol'], body['entry_time']
    if not (date and symbol and entry_time):
        raise ValueError('date, symbol, and entry_time are all required')
    entry_time = entry_time[:5]
    exit_time = body.get('exit_time') or None
    if exit_time:
        exit_time = exit_time[:5]
    return date, symbol, entry_time, exit_time


def _rec_key(e):
    ex = e.get('exit_time')
    return (e['date'], e['symbol'], e['entry_time'][:5], ex[:5] if ex else None)


def _add_exclusion(existing, date, symbol, entry_time, exit_time):
    """Append one exclusion record if it isn't already present. Returns True if added."""
    key = (date, symbol, entry_time, exit_time)
    if any(_rec_key(e) == key for e in existing):
        return False
    entry = {'date': date, 'symbol': symbol, 'entry_time': entry_time}
    if exit_time:
        entry['exit_time'] = exit_time
    existing.append(entry)
    return True


def _load_exclusions():
    if not os.path.exists(itz.PAPER_EXCLUDED_PATH):
        return []
    with open(itz.PAPER_EXCLUDED_PATH) as f:
        return json.load(f)


def _save_exclusions(existing):
    os.makedirs(os.path.dirname(itz.PAPER_EXCLUDED_PATH), exist_ok=True)
    with open(itz.PAPER_EXCLUDED_PATH, 'w') as f:
        json.dump(existing, f, indent=2)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def log_message(self, fmt, *args):
        pass  # quiet, matches the plain http.server this replaces

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == '/api/exclude-paper-trade':
            self._handle_exclude_one()
        elif self.path == '/api/exclude-paper-trades':
            self._handle_exclude_many()
        else:
            self._json(404, {'error': 'not found'})

    def _handle_exclude_one(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            date, symbol, entry_time, exit_time = _parse_trade_key(body)
        except Exception as e:
            self._json(400, {'error': str(e)})
            return

        try:
            existing = _load_exclusions()
            if _add_exclusion(existing, date, symbol, entry_time, exit_time):
                _save_exclusions(existing)
            itz.rebuild_calendar()
        except Exception as e:
            self._json(500, {'error': str(e)})
            return

        self._json(200, {'ok': True})

    def _handle_exclude_many(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            trades = body['trades']
            if not isinstance(trades, list) or not trades:
                raise ValueError('trades must be a non-empty list')
            keys = [_parse_trade_key(t) for t in trades]
        except Exception as e:
            self._json(400, {'error': str(e)})
            return

        try:
            existing = _load_exclusions()
            added = sum(_add_exclusion(existing, *k) for k in keys)
            if added:
                _save_exclusions(existing)
            itz.rebuild_calendar()
        except Exception as e:
            self._json(500, {'error': str(e)})
            return

        self._json(200, {'ok': True, 'deleted': len(keys)})


def serve(port):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Serving {DASHBOARD_DIR} on http://localhost:{port}')
    server.serve_forever()


if __name__ == '__main__':
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
