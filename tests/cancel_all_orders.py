from binance.client import Client
from dotenv import load_dotenv
import os

load_dotenv()
client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))

symbol = "BTCUSDC"

print(f"🔎 Fetching open orders for {symbol}...")
orders = client.get_open_orders(symbol=symbol)

if not orders:
    print("✅ No open orders found.")
else:
    print(f"📋 Found {len(orders)} open orders:")
    for o in orders:
        print(f" - id={o['orderId']} | type={o['type']} | price={o['price']} | status={o['status']}")
        client.cancel_order(symbol=symbol, orderId=o["orderId"])
        print("   → Cancelled.")
print("✅ Done.")
