import os, ccxt
from dotenv import load_dotenv

load_dotenv()

exchange = ccxt.binance({
    "apiKey": os.environ["BINANCE_API_KEY"],
    "secret": os.environ["BINANCE_API_SECRET"],
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

exchange.set_sandbox_mode(True)
exchange.load_markets()

print("✅ Connected to Binance Testnet (Spot)")
bal = exchange.fetch_balance()
print("Free USDT:", bal["free"].get("USDT", 0))
