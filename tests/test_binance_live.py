from binance.client import Client
from dotenv import load_dotenv
import os

# === Load keys from .env file ===
load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

if not api_key or not api_secret:
    raise ValueError("❌ API keys not found. Make sure your .env file has BINANCE_API_KEY and BINANCE_API_SECRET.")

# === Initialize client ===
client = Client(api_key, api_secret)

# === Test connection ===
try:
    info = client.get_account()
    print("✅ Connected successfully to Binance Real Account!")
    print("Balances with non-zero amount:")
    for asset in info["balances"]:
        if float(asset["free"]) > 0 or float(asset["locked"]) > 0:
            print(f"  {asset['asset']}: {asset['free']} free, {asset['locked']} locked")
except Exception as e:
    print("❌ Connection failed:")
    print(e)
