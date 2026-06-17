import schwabdev
from utilities import config



if __name__ == "__main__":
    client = schwabdev.Client(
            config.SCHWAB_API_KEY,
            config.SCHWAB_CLIENT_ID
        )
    
    accounts = client.linked_accounts().json()
    print(accounts)