"""
test_binance_connection.py
Quick connectivity and balance checker for Binance Spot API
"""

import os
import ccxt
from dotenv import load_dotenv

def main():
    print("🔍 Testing Binance Spot API connectivity...\n")

    # Load API keys from .env
    load_dotenv()

    try:
        exchange = ccxt.binance({
            "apiKey": os.getenv("BINANCE_API_KEY"),
            "secret": os.getenv("BINANCE_API_SECRET"),
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })

        # Fetch account balance
        balance = exchange.fetch_balance()
        print("✅ Binance connection OK!\n")

        print("💰 Your Spot Wallet Balances (non-zero):")
        has_assets = False
        for symbol, amount in balance["free"].items():
            if amount and amount > 0:
                print(f"  {symbol:<8} {amount}")
                has_assets = True

        if not has_assets:
            print("  (No available assets found)")

    except Exception as e:
        print("❌ Connection failed:")
        print(e)

if __name__ == "__main__":
    main()
