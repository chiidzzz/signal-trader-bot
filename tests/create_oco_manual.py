import os, hmac, hashlib, time, requests, urllib.parse
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET").encode()
BASE_URL = "https://api.binance.com"

def sign(params):
    query = urllib.parse.urlencode(params)
    return hmac.new(API_SECRET, query.encode(), hashlib.sha256).hexdigest()

def create_oco_order(symbol, side, quantity, price, stop_price, stop_limit_price, tif="GTC"):
    url = BASE_URL + "/api/v3/order/oco"
    ts = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "stopPrice": stop_price,
        "stopLimitPrice": stop_limit_price,
        "stopLimitTimeInForce": tif,
        "timestamp": ts,
    }
    params["signature"] = sign(params)
    headers = {"X-MBX-APIKEY": API_KEY}

    r = requests.post(url, headers=headers, params=params)
    if r.status_code == 200:
        print("✅ OCO order placed successfully:")
        print(r.json())
    else:
        print(f"❌ Error {r.status_code}: {r.text}")

# Example test
if __name__ == "__main__":
    create_oco_order(
        symbol="SOLUSDC",
        side="SELL",
        quantity="0.05",
        price="186",
        stop_price="183",
        stop_limit_price="182.9"
    )
