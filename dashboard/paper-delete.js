// Paper-trade delete flow for trades.html — POSTs {date, symbol, entry_time, exit_time?}
// to dashboard_server.py's write endpoint and shows a shared error message on failure.
// The write endpoint rebuilds the whole calendar (parses every execution on disk) before
// responding, so a single delete can take a few seconds. Without this guard, clicking
// delete again while the first request is still in flight fires a second full rebuild
// for the same round-trip instead of doing nothing.
const _paperDeleteInFlight = new Set();

async function deletePaperTradeRequest({ date, symbol, entry_time, exit_time }) {
  const key = `${date}|${symbol}|${entry_time}`;
  if (_paperDeleteInFlight.has(key)) return;
  _paperDeleteInFlight.add(key);
  try {
    const body = { date, symbol, entry_time };
    if (exit_time) body.exit_time = exit_time;
    const r = await fetch('/api/exclude-paper-trade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
  } finally {
    _paperDeleteInFlight.delete(key);
  }
}

// Bulk variant for trades.html's multi-select delete — one POST, one calendar rebuild
// for the whole batch instead of one rebuild per trade (see dashboard_server.py).
let _bulkDeleteInFlight = false;

async function deletePaperTradesRequest(trades) {
  if (_bulkDeleteInFlight) return;
  _bulkDeleteInFlight = true;
  try {
    const r = await fetch('/api/exclude-paper-trades', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trades }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
  } finally {
    _bulkDeleteInFlight = false;
  }
}

function alertPaperDeleteFailure(err) {
  alert(`Could not delete trade: ${err.message}\n\nDelete needs the dashboard served via ` +
        `dashboard_server.py or launch_dashboard.py (plain "python -m http.server" has no write endpoint).`);
}
