import os, time, math, hmac, hashlib, requests, urllib.parse
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

# === Setup ================================================================
load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)
BASE_URL = "https://api.binance.com"

# === Helpers ==============================================================
def _sign(params: dict) -> str:
    q = urllib.parse.urlencode(params)
    return hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()

def _headers():
    return {"X-MBX-APIKEY": api_key}

def _get_symbol_info(sym: str):
    info = client.get_symbol_info(sym)
    if not info:
        raise RuntimeError(f"Symbol info not found for {sym}")
    return info

def _get_tick_and_step(sym: str):
    info = _get_symbol_info(sym)
    tick = float(next(f["tickSize"] for f in info["filters"] if f["filterType"] == "PRICE_FILTER"))
    step = float(next(f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"))
    return tick, step

def _round_tick(px: float, tick: float) -> float:
    return round(math.floor(px / tick) * tick, 12)

def _round_step(qty: float, step: float) -> float:
    return math.floor(qty / step) * step

def _fmt(v: float, digits: int = 8) -> str:
    return f"{v:.{digits}f}".rstrip("0").rstrip(".")

# === OCO ================================================================
def place_oco(symbol, side, quantity, tp, sl_trigger, sl_limit):
    """Direct REST OCO call (bypasses python-binance limitations)."""
    sym_clean = symbol.replace("/", "")
    tick, _ = _get_tick_and_step(sym_clean)
    tp = _round_tick(float(tp), tick)
    sl_trigger = _round_tick(float(sl_trigger), tick)
    sl_limit = _round_tick(float(sl_limit), tick)

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
    r = requests.post(url, headers=_headers(), params=params, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"OCO failed ({r.status_code}): {r.text}")
    return r.json()

# === Market buy ==========================================================
def execute_market_buy(symbol, usd_amount):
    """Market buy with automatic qty calc and return of actual fill price.
    
    Returns:
        tuple: (filled_qty, actual_fill_price)
    """
    sym_clean = symbol.replace("/", "")
    
    # Get current price and symbol info for proper rounding
    price = float(client.get_symbol_ticker(symbol=sym_clean)["price"])
    _, step = _get_tick_and_step(sym_clean)
    
    # Calculate quantity and round DOWN to step size
    qty = usd_amount / price
    qty = _round_step(qty, step)
    
    # Format quantity string (remove trailing zeros)
    qty_str = _fmt(qty)

    print(f"[BUY DEBUG] Attempting to buy {qty_str} {sym_clean} (${usd_amount:.2f} @ ${price:.6f})")

    order = client.order_market_buy(symbol=sym_clean, quantity=qty_str)

    # --- Wait for fill ---
    for i in range(20):
        o = client.get_order(symbol=sym_clean, orderId=order["orderId"])
        if o["status"] == "FILLED":
            filled_qty = float(o["executedQty"])
            
            # Actual fill price: cummulativeQuoteQty / executedQty
            actual_fill_price = float(o["cummulativeQuoteQty"]) / filled_qty
            
            print(f"[BUY DEBUG] Filled {filled_qty} @ ${actual_fill_price:.6f}")
            return filled_qty, actual_fill_price

        time.sleep(1)

    raise RuntimeError("Market order not filled in time")

# === Emergency flatten ===================================================
def market_sell(symbol, qty):
    sym = symbol.replace("/", "")
    qty_str = _fmt(qty)
    order = client.order_market_sell(symbol=sym, quantity=qty_str)
    return order

# === Verify OCO ==========================================================
def verify_oco(oco_id, timeout_sec=5):
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        try:
            data = client.get_oco_order(orderListId=oco_id)
            if "orders" in data and len(data["orders"]) >= 2:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

# === Atomic Bracket ======================================================
def place_bracket_atomic(
    symbol,
    spend_usd,
    entry_hint,
    tp_price,
    sl_trigger,
    sl_limit_offset_frac=0.001,
    verify_timeout_sec=5,
):
    """
    1. Market buy (verify fill)
    2. Place OCO (TP+SL)
    3. Verify OCO exists
    4. On any failure: market-sell immediately to flatten position
    
    Returns:
        dict with keys: filled_qty, avg_price, tp, sl_trigger, sl_limit, oco_id
    """
    sym = symbol.replace("/", "")
    tick, step = _get_tick_and_step(sym)
    last_px = float(client.get_symbol_ticker(symbol=sym)["price"])
    sl_limit = sl_trigger * (1 - sl_limit_offset_frac)

    filled_qty = 0.0
    avg_price = 0.0
    
    try:
        # FIXED: Execute buy FIRST to get actual fill price
        filled_qty, avg_price = execute_market_buy(symbol, spend_usd)
    except Exception as e:
        raise RuntimeError(f"Market buy failed: {e}")
    
    # FIXED: Now validate and round using ACTUAL fill price, not entry hint
    tp_r = _round_tick(tp_price, tick)
    sl_tr_r = _round_tick(sl_trigger, tick)
    sl_lim_r = _round_tick(sl_limit, tick)

    # Debug: print actual values
    print(f"[OCO DEBUG] Fill: {avg_price:.8f}")
    print(f"[OCO DEBUG] TP:   {tp_r:.8f} (must be > fill)")
    print(f"[OCO DEBUG] SL:   {sl_tr_r:.8f} (must be < fill)")
    print(f"[OCO DEBUG] SL_L: {sl_lim_r:.8f} (must be < SL trigger)")

    # Validate price relationship using actual fill
    if not (tp_r > avg_price > sl_tr_r):
        try:
            market_sell(symbol, filled_qty)
        except Exception:
            pass
        raise RuntimeError(
            f"Invalid OCO price relation after fill:\n"
            f"  TP: {tp_r} | Fill: {avg_price} | SL: {sl_tr_r}\n"
            f"  Required: TP > Fill > SL\n"
            f"  Position flattened for safety"
        )
    
    # Additional Binance validation: SL limit must be below SL trigger
    if sl_lim_r >= sl_tr_r:
        try:
            market_sell(symbol, filled_qty)
        except Exception:
            pass
        raise RuntimeError(
            f"Invalid SL prices: limit {sl_lim_r} must be < trigger {sl_tr_r}\n"
            f"Position flattened for safety"
        )

    # --- OCO placement ---
    try:
        # --- Wait for Binance to update the free balance after fill ---
        base_asset = sym.replace("USDC", "").replace("USDT", "").replace("/", "").upper()
        free_balance = 0.0
        settled = False
        for attempt in range(10):  # up to ~3s total
            try:
                bal = client.get_asset_balance(asset=base_asset)
                free_balance = float(bal["free"])
                if free_balance >= filled_qty * 0.999:
                    settled = True
                    break  # good enough
            except Exception as e:
                print(f"[OCO WARN] balance fetch attempt {attempt+1} failed: {e}")
            time.sleep(0.3)

        print(f"[OCO DEBUG] Free balance after wait: {free_balance:.8f} (need {filled_qty:.8f})")
        if settled:
            print(f"[OCO DEBUG] Balance settled after {attempt * 0.3:.1f}s")

        # Adjust if still not enough
        if free_balance < filled_qty * 0.999:
            print(f"[OCO WARN] Balance not fully updated yet; trimming to available {free_balance:.8f}")
            filled_qty = _round_step(max(free_balance * 0.999, step), step)
        
                # --- Final sanity vs LIVE price so Binance accepts the OCO ---
        last_now = float(client.get_symbol_ticker(symbol=sym)["price"])

        # If live price is already <= SL trigger, push SL one tick below market
        if last_now <= sl_tr_r:
            old_sl_tr, old_sl_lim = sl_tr_r, sl_lim_r
            sl_tr_r = _round_tick(last_now - tick, tick)
            sl_lim_r = _round_tick(sl_tr_r * (1 - sl_limit_offset_frac), tick)
            print(f"[OCO ADJUST] SL moved {old_sl_tr}->{sl_tr_r} / {old_sl_lim}->{sl_lim_r} "
                  f"because last={last_now} <= SL trigger")

        # If live price is already >= TP, push TP one tick above market
        if last_now >= tp_r:
            old_tp = tp_r
            tp_r = _round_tick(last_now + tick, tick)
            print(f"[OCO ADJUST] TP moved {old_tp}->{tp_r} because last={last_now} >= TP")

        # --- Place OCO ---
        oco = place_oco(
            symbol,
            "SELL",
            _fmt(filled_qty),
            str(tp_r),
            str(sl_tr_r),
            str(sl_lim_r),
        )
        oco_id = oco.get("orderListId")
        if not oco_id:
            raise RuntimeError("OCO response missing orderListId")

        # Verify OCO actually exists
        if not verify_oco(oco_id, verify_timeout_sec):
            market_sell(symbol, filled_qty)
            raise RuntimeError(f"OCO verification failed (ID={oco_id}); flattened position")
        
        # Record OCO for monitoring
        try:
            from signal_trader import track_oco
            track_oco(symbol, oco_id)
        except Exception as e:
            print(f"[OCO TRACK] Could not record OCO {oco_id}: {e}")


    except Exception as e:
        try:
            market_sell(symbol, filled_qty)
        except Exception as se:
            raise RuntimeError(f"OCO failed: {e}; flatten also failed: {se}")
        else:
            raise RuntimeError(f"OCO failed: {e}; position flattened")

    # FIXED: Return avg_price in result dict
    return {
        "filled_qty": filled_qty,
        "avg_price": avg_price,
        "tp": tp_r,
        "sl_trigger": sl_tr_r,
        "sl_limit": sl_lim_r,
        "oco_id": oco_id,
    }
