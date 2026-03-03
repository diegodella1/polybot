"""One-time setup: derive Polymarket API credentials from your private key.

Usage:
    1. Put your POLYMARKET_PRIVATE_KEY in .env
    2. Run: source venv/bin/activate && python setup_keys.py
    3. It will print the API_KEY, API_SECRET, API_PASSPHRASE to add to .env
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv

load_dotenv()


def main():
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        print("ERROR: Set POLYMARKET_PRIVATE_KEY in .env first")
        sys.exit(1)

    # Ensure it has 0x prefix
    if not pk.startswith("0x"):
        pk = "0x" + pk

    from py_clob_client.client import ClobClient

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
    )

    print("Deriving API credentials from your private key...")
    print(f"Wallet address: {client.signer.address()}")
    print()

    try:
        creds = client.create_or_derive_api_creds()
    except Exception as e:
        print(f"Error: {e}")
        print("Trying derive_api_key instead...")
        creds = client.derive_api_key()

    print("=== Add these to your .env ===")
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    print()

    # Also try to get balance
    try:
        from py_clob_client.clob_types import ApiCreds as AC

        client2 = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            creds=AC(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            ),
        )
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        bal = client2.get_balance_allowance(
            params=BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
            )
        )
        usdc = float(bal.get("balance", "0")) / 1e6
        allowance = float(bal.get("allowance", "0")) / 1e6
        print(f"USDC Balance: ${usdc:.2f}")
        print(f"USDC Allowance: ${allowance:.2f}")
        if allowance < usdc:
            print("WARNING: Allowance < balance. You may need to approve the exchange contract.")
    except Exception as e:
        print(f"Could not fetch balance (may need to deposit USDC first): {e}")


if __name__ == "__main__":
    main()
