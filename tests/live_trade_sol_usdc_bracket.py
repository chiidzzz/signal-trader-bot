import os, time, math, hmac, hashlib, requests, urllib.parse
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

# === Load keys ===
load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")  # keep as plain string
client = Client(api_key, api_secret)
BASE_URL = "https://api.binance.com"

# === Helper: sign + manual OCO ===
def sign_params(params: dict):
    """Generate Binance HMAC SHA256 signature"""
    query = urllib.parse.urlencode(params)
    return hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def place_oco_order(symbol, side, quantity, tp_price, sl_trigger, sl_limit):
    """Send a manual OCO order to Binance REST endpoint"""
    ts = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": tp_price,
        "stopPrice": sl_trigger,
        "stopLimitPrice": sl_limit,
        "stopLimitTimeInForce": "GTC",
        "timestamp": ts,
    }
    params["signature"] = sign_params(params)
    headers = {"X-MBX-APIKEY": api_key}
    url = BASE_URL + "/api/v3/order/oco"

    r = requests.post(url, headers=headers, params=params)
    if r.status_code == 200:
        print("✅ OCO (TP + SL) placed successfully:")
        print(r.json())
    else:
        print(f"❌ Error placing OCO: {r.status_code} → {r.text}")

# === Trading parameters ===
symbol = "SOLUSDC"
tp_price = 186.0
sl_trigger = 183.0
sl_limit = 182.9
usd_amount = 11  # ensure > $10 min notional

# === Step 1. Calculate quantity ===
price = float(client.get_symbol_ticker(symbol=symbol)["price"])
qty = usd_amount / price
qty = math.floor(qty * 100) / 100  # 2 decimals for SOL
qty_str = f"{qty:.2f}"
print(f"Targeting {qty_str} SOL (~${usd_amount}) at market price {price}")

# === Step 2. Place market buy ===
try:
    order = client.order_market_buy(symbol=symbol, quantity=qty_str)
    order_id = order["orderId"]
    print(f"✅ Market buy placed (orderId={order_id})")
except BinanceAPIException as e:
    print("❌ API error placing buy order:", e)
    raise SystemExit
except Exception as e:
    print("❌ Unexpected error:", e)
    raise SystemExit

# === Step 3. Wait for fill ===
print("⏳ Waiting for order to fill...")
for _ in range(20):
    o = client.get_order(symbol=symbol, orderId=order_id)
    if o["status"] == "FILLED":
        print("✅ Buy order filled.")
        break
    time.sleep(1)
else:
    print("⚠️ Order not filled yet, aborting TP/SL placement.")
    raise SystemExit

# === Step 4. Get filled qty ===
filled_qty = float(o["executedQty"])
filled_qty_str = f"{filled_qty:.2f}"
print(f"Filled quantity: {filled_qty_str} SOL")

# === Step 5. Place manual OCO (TP + SL) ===
place_oco_order(
    symbol=symbol,
    side="SELL",
    quantity=filled_qty_str,
    tp_price=str(tp_price),
    sl_trigger=str(sl_trigger),
    sl_limit=str(sl_limit),
)
