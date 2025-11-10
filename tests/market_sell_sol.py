from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv
import os, math, time

load_dotenv()
client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))

quote_asset = "USDC"  # sell everything to USDC

def get_precision(symbol_info):
    """Extracts step size and precision from Binance symbol info."""
    step_size = float(next(f["stepSize"] for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE"))
    precision = abs(int(round(math.log10(step_size), 0)))
    return step_size, precision

# Fetch all account balances
balances = client.get_account()["balances"]

for asset in balances:
    free_amount = float(asset["free"])
    symbol = f"{asset['asset']}{quote_asset}"

    # skip small or zero balances
    if free_amount <= 0:
        continue
    if asset["asset"] == quote_asset:
        continue  # don’t sell quote currency

    # Check if trading pair exists
    info = client.get_symbol_info(symbol)
    if not info:
        print(f"⚠️ No {symbol} market found, skipping {asset['asset']}")
        continue

    # Get precision and filters
    step_size, precision = get_precision(info)

    if free_amount < step_size:
        print(f"⚠️ {asset['asset']} balance {free_amount} below step size {step_size}, skipping.")
        continue

    # Round down to valid precision
    qty = math.floor(free_amount / step_size) * step_size * 0.999  # 99.9% of valid amount
    qty_str = f"{qty:.{precision}f}"

    print(f"💰 Selling {qty_str} {asset['asset']} at market on {symbol} ...")

    try:
        order = client.order_market_sell(symbol=symbol, quantity=qty_str)
        print(f"✅ Sold {qty_str} {asset['asset']} for {quote_asset}")
        print(order)
    except BinanceAPIException as e:
        print(f"❌ Binance API error for {symbol}: {e}")
    except Exception as e:
        print(f"❌ Unexpected error selling {symbol}: {e}")

    time.sleep(1)  # small delay to avoid rate limits
