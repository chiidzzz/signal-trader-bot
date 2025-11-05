# verify_orders.py
import ccxt, os, json
from dotenv import load_dotenv

print("✅ Connecting to Binance Testnet (Spot)...")

load_dotenv()

exchange = ccxt.binance({
    "apiKey": os.getenv("BINANCE_API_KEY"),
    "secret": os.getenv("BINANCE_API_SECRET"),
})
exchange.set_sandbox_mode(True)
exchange.options["warnOnFetchOpenOrdersWithoutSymbol"] = False

# --- Fetch balances ---
balance = exchange.fetch_balance()
free_balances = {k: v for k, v in balance["free"].items() if v and v > 0}

print(f"\n💰 Free balances:")
for asset, amt in free_balances.items():
    print(f"   - {asset}: {amt}")

# --- Fetch open orders ---
print("\n🔎 Fetching all open orders...")
open_orders = exchange.fetch_open_orders()
print(f"📋 Found {len(open_orders)} open orders.")

# --- Try to fetch closed orders symbol by symbol (for popular symbols only) ---
symbols_to_check = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DASH/USDT", "ZEN/USDT", "TRB/USDT"
]
closed_orders = []
for sym in symbols_to_check:
    try:
        closed_orders += exchange.fetch_closed_orders(sym)
    except Exception:
        continue

print(f"📋 Found {len(closed_orders)} closed orders.")

# --- Combine all ---
all_orders = open_orders + closed_orders
if not all_orders:
    print("❕ No orders found.")
else:
    print(f"\n🧾 Total orders: {len(all_orders)}\n")
    all_orders.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
    for o in all_orders:
        ts = exchange.iso8601(o["timestamp"]) if o.get("timestamp") else "—"
        symbol = o.get("symbol", "—")
        side = o.get("side", "—").upper()
        status = o.get("status", "—")
        price = o.get("price") or "MKT"
        amount = o.get("amount", 0)
        filled = o.get("filled", 0)
        print(f"🕒 {ts} | {symbol} | {side} {amount} @ {price} | filled={filled} | status={status}")

# --- Save snapshot ---
snapshot = {
    "balances": free_balances,
    "orders": all_orders,
}
os.makedirs("runtime", exist_ok=True)
with open("runtime/orders_snapshot.json", "w", encoding="utf-8") as f:
    json.dump(snapshot, f, indent=2, ensure_ascii=False)

print("\n📂 Saved snapshot → runtime/orders_snapshot.json")
print("✅ Done.")
