import os, time, math, hmac, hashlib, requests, urllib.parse
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)
BASE_URL = "https://api.binance.com"

def round_to_tick(symbol, price):
    info = client.get_symbol_info(symbol)
    tick_size = float([f for f in info["filters"] if f["filterType"] == "PRICE_FILTER"][0]["tickSize"])
    precision = int(round(-math.log10(tick_size)))
    return round(price, precision)

def _sign(params):
    q = urllib.parse.urlencode(params)
    return hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()

def place_oco(symbol, side, quantity, tp, sl_trigger, sl_limit):
    """Direct REST OCO call (bypasses python-binance limitations) with tick-size rounding."""
    # --- Helper to round prices to valid tick size ---
    def round_to_tick(sym, price):
        info = client.get_symbol_info(sym)
        if not info:
            return price  # fallback if metadata missing
        try:
            filt = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
            tick_size = float(filt["tickSize"])
            precision = int(round(-math.log10(tick_size)))
            return round(float(price), precision)
        except Exception:
            return float(price)

    sym_clean = symbol.replace("/", "")
    tp = round_to_tick(sym_clean, tp)
    sl_trigger = round_to_tick(sym_clean, sl_trigger)
    sl_limit = round_to_tick(sym_clean, sl_limit)

    url = BASE_URL + "/api/v3/order/oco"
    ts = int(time.time() * 1000)
    params = {
        "symbol": sym_clean,
        "side": side,
        "quantity": quantity,
        "price": f"{tp:.8f}",
        "stopPrice": f"{sl_trigger:.8f}",
        "stopLimitPrice": f"{sl_limit:.8f}",
        "stopLimitTimeInForce": "GTC",
        "timestamp": ts,
    }
    params["signature"] = _sign(params)
    headers = {"X-MBX-APIKEY": api_key}

    r = requests.post(url, headers=headers, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"OCO failed ({r.status_code}): {r.text}")
    return r.json()


def execute_market_buy(symbol, usd_amount):
    """Market buy with automatic qty calc."""
    price = float(client.get_symbol_ticker(symbol=symbol.replace("/", ""))["price"])
    qty = usd_amount / price
    qty = math.floor(qty * 100) / 100  # round 2 decimals for most coins
    qty_str = f"{qty:.2f}"
    order = client.order_market_buy(symbol=symbol.replace("/", ""), quantity=qty_str)
    for _ in range(20):
        o = client.get_order(symbol=symbol.replace("/", ""), orderId=order["orderId"])
        if o["status"] == "FILLED":
            return float(o["executedQty"])
        time.sleep(1)
    raise RuntimeError("Market order not filled in time")
