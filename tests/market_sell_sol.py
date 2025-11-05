from binance.client import Client
from dotenv import load_dotenv
from binance.exceptions import BinanceAPIException
import os, math

load_dotenv()
client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))

symbol = "SOLUSDC"

# --- Fetch precision and filters dynamically ---
info = client.get_symbol_info(symbol)
step_size = float(next(f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"))
precision = abs(int(round(math.log10(step_size), 0)))

bal = float(client.get_asset_balance(asset="SOL")["free"])
print(f"🔎 Free SOL balance: {bal}")

if bal < step_size:
    print(f"⚠️ Balance {bal} below minimum tradable step {step_size}. Nothing to sell.")
    exit()

# --- Round down to valid precision ---
qty = math.floor(bal / step_size) * step_size * 0.999  # 99.9% of valid amount
qty_str = f"{qty:.{precision}f}"

try:
    print(f"💰 Selling {qty_str} SOL at market on {symbol} ...")
    order = client.order_market_sell(symbol=symbol, quantity=qty_str)
    print("✅ Market sell placed successfully.")
    print(order)
except BinanceAPIException as e:
    print("❌ Binance API error:", e)
except Exception as e:
    print("❌ Unexpected error:", e)
