import sys
import os
_vendor = os.path.join(os.path.dirname(__file__), 'vendor')
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)

import schwabdev
from utilities import config

def main():
    client = schwabdev.Client(config.SCHWAB_API_KEY, config.SCHWAB_CLIENT_ID)
    resp = client.linked_accounts()
    if resp.status_code != 200:
        print(f"Failed to fetch linked accounts ({resp.status_code}): {resp.text}")
        return

    live = {acct['hashValue']: acct['accountNumber'] for acct in resp.json()}

    configured = {
        'cash_account_hash':   config.cash_account_hash,
        'roth_account_hash':   config.roth_account_hash,
        'margin_account_hash': config.margin_account_hash,
    }

    print(f"Linked accounts on Schwab: {len(live)}")
    for hash_val, acct_num in live.items():
        print(f"  {acct_num}  {hash_val}")

    print("\nConfig check:")
    for name, hash_val in configured.items():
        if not hash_val:
            print(f"  {name}: (not set) - skipped")
        elif hash_val in live:
            print(f"  {name}: OK -> account {live[hash_val]}")
        else:
            print(f"  {name}: MISMATCH - not in linked accounts. Update utilities/config.py")

if __name__ == "__main__":
    main()
