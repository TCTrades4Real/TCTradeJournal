"""
tradezero_client.py — thin client for the TradeZero Developer API.

Endpoints confirmed by direct testing against the real API (2026-08-24), not just docs
(the docs' documented `GET /orders/start-date/{date}` path returned 404; this is what
actually works):

  GET {base}/v1/api/accounts                       -> {"accounts": [...]}
  GET {base}/v1/api/accounts/{accountId}/orders     -> {"orders": [...]}

Auth: send TZ-API-KEY-ID / TZ-API-SECRET-KEY headers on every request (no token exchange).
Which environment (paper vs live) you hit is determined entirely by which key pair you send.

Known limitation: GET .../orders returns only the current trading day's orders (working +
closed) — date/range query params are silently ignored. There is no confirmed working
endpoint for historical (prior-day) order history. Import scripts must run same-day.
"""
import requests


class TradeZeroClient:
    def __init__(self, base_url, key_id, secret_key, account_id):
        self.base_url    = base_url.rstrip('/')
        self.account_id  = account_id
        self._headers = {
            'Accept':           'application/json',
            'TZ-API-KEY-ID':     key_id,
            'TZ-API-SECRET-KEY': secret_key,
        }

    def _get(self, path):
        resp = requests.get(f'{self.base_url}{path}', headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_orders(self):
        """Today's orders (working + closed) for this account. Each row is one order,
        already blended across any partial fills it received (see 'executed'/'priceAvg')."""
        return self._get(f'/v1/api/accounts/{self.account_id}/orders').get('orders', [])

    def get_account(self):
        """This account's summary record (equity, availableCash, realized, etc.) from
        GET /v1/api/accounts, matched by account id."""
        accounts = self._get('/v1/api/accounts').get('accounts', [])
        for acct in accounts:
            if acct.get('account') == self.account_id:
                return acct
        return None
