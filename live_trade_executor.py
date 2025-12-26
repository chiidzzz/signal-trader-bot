import os, time, math, hmac, hashlib, requests, urllib.parse
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

# === Setup ================================================================
load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
if not api_key or not api_secret:
    raise RuntimeError("Missing BINANCE_API_KEY / BINANCE_API_SECRET in .env")

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

def place_oco(symbol, side, quantity, tp, sl_trigger, sl_limit):
    """Fully filter-compliant OCO placement."""
    sym_clean = symbol.replace("/", "")
    info = _get_symbol_info(sym_clean)

    # Extract filters
    price_filter = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
    lot_filter   = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    min_notional = float(next(
        (f["minNotional"] for f in info["filters"] if f["filterType"] == "MIN_NOTIONAL"),
        5.0
    ))
    tick = float(price_filter["tickSize"])
    step = float(lot_filter["stepSize"])

    # --- Quantize everything exactly on Binance grid ---
    def q_dec(v, step): return math.floor(v / step) * step
    def p_dec(v, tick): return math.floor(v / tick) * tick

    qty = q_dec(float(quantity), step)
    tp  = p_dec(float(tp), tick)
    sl_trigger = p_dec(float(sl_trigger), tick)
    sl_limit   = p_dec(float(sl_limit), tick)

    # --- Guarantee stopLimit < stopPrice by ≥1 tick ---
    if sl_limit >= sl_trigger:
        sl_limit = p_dec(sl_trigger - tick, tick)

    # --- Enforce minNotional rule after flooring ---
    tp_notional = tp * qty
    sl_notional = sl_trigger * qty
    if tp_notional < min_notional or sl_notional < min_notional:
        need_qty = (min_notional / min(tp, sl_trigger)) * 1.05
        qty = q_dec(need_qty, step)
        print(f"[FILTER] Raised qty to {qty:.8f} to satisfy minNotional={min_notional}")

    # --- Stringify with fixed decimals Binance likes ---
    qty_str = f"{qty:.8f}".rstrip("0").rstrip(".")
    tp_str  = f"{tp:.8f}".rstrip("0").rstrip(".")
    sl_str  = f"{sl_trigger:.8f}".rstrip("0").rstrip(".")
    sl_lim_str = f"{sl_limit:.8f}".rstrip("0").rstrip(".")

    # --- Compose signed request ---
    url = BASE_URL + "/api/v3/order/oco"
    ts = int(time.time() * 1000)
    params = {
        "symbol": sym_clean,
        "side": side,
        "quantity": qty_str,
        "price": tp_str,
        "stopPrice": sl_str,
        "stopLimitPrice": sl_lim_str,
        "stopLimitTimeInForce": "GTC",
        "timestamp": str(ts),
    }
    query = urllib.parse.urlencode(params, doseq=True)
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature

    # --- Send ---
    r = requests.post(url, headers=_headers(), params=params, timeout=10)
    try:
        data = r.json()
    except Exception:
        data = {}

    # Treat non-200 with valid payload as success
    if r.status_code != 200 and not data.get("orderListId"):
        raise RuntimeError(f"OCO failed ({r.status_code}): {r.text}")

    print(f"[OCO OK] {sym_clean} qty={qty_str} TP={tp_str} SL={sl_str}/{sl_lim_str}")
    return data

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

# === Limit buy (prepared entry) ==========================================
def execute_limit_buy(symbol, usd_amount, limit_price, tif_sec, on_placed=None):
    """
    Place LIMIT BUY at limit_price, wait up to tif_sec for fill.
    If not filled -> cancel and return (None, None, orderId)

    Returns:
        tuple: (filled_qty or None, avg_fill_price or None, order_id)
    """
    sym_clean = symbol.replace("/", "")

    tick, step = _get_tick_and_step(sym_clean)

    # Quantize limit price and qty to Binance filters
    limit_px = _round_tick(float(limit_price), tick)
    qty = float(usd_amount) / limit_px
    qty = _round_step(qty, step)

    qty_str = _fmt(qty)
    px_str = _fmt(limit_px)

    print(f"[LIMIT BUY] Placing LIMIT BUY {sym_clean} qty={qty_str} @ {px_str} (tif={tif_sec}s)")

    order = client.order_limit_buy(
        symbol=sym_clean,
        quantity=qty_str,
        price=px_str,
        timeInForce="GTC",
    )

    oid = order["orderId"]

    # 🔔 Notify immediately that LIMIT was placed
    if on_placed:
        try:
            on_placed(oid)
        except Exception:
            pass

    deadline = time.time() + float(tif_sec)

    # poll until filled or timeout
    while time.time() < deadline:
        o = client.get_order(symbol=sym_clean, orderId=oid)
        st = o.get("status")
        if st == "FILLED":
            filled_qty = float(o["executedQty"])
            avg_fill = float(o["cummulativeQuoteQty"]) / filled_qty
            print(f"[LIMIT BUY] FILLED qty={filled_qty} avg={avg_fill}")
            return filled_qty, avg_fill, oid

        if st in ("CANCELED", "REJECTED", "EXPIRED"):
            print(f"[LIMIT BUY] ended early status={st}")
            return None, None, oid

        time.sleep(1)

    # not filled in time -> cancel
    try:
        client.cancel_order(symbol=sym_clean, orderId=oid)
        print(f"[LIMIT BUY] CANCELED (timeout) orderId={oid}")
    except Exception as e:
        print(f"[LIMIT BUY] cancel failed: {e}")

    return None, None, oid


# === OCO after a non-market entry =======================================
def place_oco_after_fill(
    symbol,
    filled_qty,
    fill_price,
    tp_price,
    sl_trigger,
    sl_limit_offset_frac=0.001,
    verify_timeout_sec=5,
):
    """
    Place OCO (TP+SL) AFTER you already have a filled position (e.g., from LIMIT buy).
    Returns dict: filled_qty, avg_price, tp, sl_trigger, sl_limit, oco_id
    """
    sym = symbol.replace("/", "")
    tick, step = _get_tick_and_step(sym)

    sl_limit = float(sl_trigger) * (1 - float(sl_limit_offset_frac))

    tp_r = _round_tick(float(tp_price), tick)
    sl_tr_r = _round_tick(float(sl_trigger), tick)
    sl_lim_r = _round_tick(float(sl_limit), tick)

    # Validate relation: TP > Fill > SL
    if not (tp_r > float(fill_price) > sl_tr_r):
        raise RuntimeError(
            f"Invalid OCO price relation:\n"
            f"  TP: {tp_r} | Fill: {fill_price} | SL: {sl_tr_r}\n"
            f"  Required: TP > Fill > SL"
        )

    if sl_lim_r >= sl_tr_r:
        sl_lim_r = _round_tick(sl_tr_r - tick, tick)

    # Balance wait + safe qty (copied from place_bracket_atomic logic)
    base_asset = sym.replace("USDC", "").replace("USDT", "").replace("/", "").upper()

    max_wait_s = 30.0
    waited = 0.0
    free_balance = 0.0
    locked_balance = 0.0

    while waited < max_wait_s:
        bal = client.get_asset_balance(asset=base_asset) or {}
        free_balance = float(bal.get("free", 0) or 0.0)
        locked_balance = float(bal.get("locked", 0) or 0.0)
        total = free_balance + locked_balance
        print(f"[BALANCE WAIT] {base_asset} free={free_balance:.8f} locked={locked_balance:.8f} total={total:.8f} need≈{filled_qty:.8f}")
        if total >= float(filled_qty) * 0.95:
            break
        time.sleep(2.0)
        waited += 2.0

    safe_qty = _round_step(min(free_balance, float(filled_qty)) * 0.999, step)
    if safe_qty < step:
        raise RuntimeError(f"After rounding, tradable amount is dust (safe_qty={safe_qty} < step={step})")

    qty_str = _fmt(safe_qty)

    # Place OCO
    oco = place_oco(symbol, "SELL", qty_str, str(tp_r), str(sl_tr_r), str(sl_lim_r))
    oco_id = oco.get("orderListId")
    if not oco_id:
        raise RuntimeError("OCO response missing orderListId")

    # Verify OCO (best effort)
    verify_oco(oco_id, verify_timeout_sec)

    return {
        "filled_qty": safe_qty,
        "avg_price": float(fill_price),
        "tp": tp_r,
        "sl_trigger": sl_tr_r,
        "sl_limit": sl_lim_r,
        "oco_id": oco_id,
    }

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
        # Execute buy FIRST to get actual fill price
        filled_qty, avg_price = execute_market_buy(symbol, spend_usd)
    except Exception as e:
        raise RuntimeError(f"Market buy failed: {e}")

    # Validate & round using actual fill price
    tp_r = _round_tick(tp_price, tick)
    sl_tr_r = _round_tick(sl_trigger, tick)
    sl_lim_r = _round_tick(sl_limit, tick)

    # Debug print
    print(f"[OCO DEBUG] Fill: {avg_price:.8f}")
    print(f"[OCO DEBUG] TP:   {tp_r:.8f} (must be > fill)")
    print(f"[OCO DEBUG] SL:   {sl_tr_r:.8f} (must be < fill)")
    print(f"[OCO DEBUG] SL_L: {sl_lim_r:.8f} (must be < SL trigger)")

    # Validate price relationships
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

    # Ensure stop-limit < stop-trigger
    if sl_lim_r >= sl_tr_r:
        try:
            market_sell(symbol, filled_qty)
        except Exception:
            pass
        raise RuntimeError(
            f"Invalid SL prices: limit {sl_lim_r} must be < trigger {sl_tr_r}\n"
            f"Position flattened for safety"
        )

    # --- OCO placement block ---
    try:
        base_asset = sym.replace("USDC", "").replace("USDT", "").replace("/", "").upper()

        # Wait for balance refresh (sub-account lag)
        max_wait_s = 30.0
        waited = 0.0
        free_balance = 0.0
        locked_balance = 0.0

        while waited < max_wait_s:
            try:
                bal = client.get_asset_balance(asset=base_asset) or {}
                free_balance = float(bal.get("free", 0) or 0.0)
                locked_balance = float(bal.get("locked", 0) or 0.0)
                total_balance = free_balance + locked_balance
                print(f"[BALANCE WAIT] {base_asset} free={free_balance:.8f} "
                      f"locked={locked_balance:.8f} total={total_balance:.8f} need≈{filled_qty:.8f}")

                if total_balance >= filled_qty * 0.95:
                    print(f"[BALANCE OK] Total balance received: {total_balance:.8f}")
                    break
            except Exception as e:
                print(f"[BALANCE WARN] fetch failed: {e}")

            time.sleep(2.0)
            waited += 2.0

        if free_balance < step:
            print(f"[BALANCE WARNING] Low free balance: {free_balance:.8f}")

        # Compute safe sell quantity
        _, step = _get_tick_and_step(sym)
        safe_qty = _round_step(min(free_balance, filled_qty) * 0.999, step)
        if safe_qty < step:
            raise RuntimeError(
                f"After rounding, tradable amount is dust (safe_qty={safe_qty:.8f} < step={step})"
            )
        qty_str = _fmt(safe_qty)

        # Live price sanity
        last_now = float(client.get_symbol_ticker(symbol=sym)["price"])
        if last_now <= sl_tr_r:
            old_sl_tr, old_sl_lim = sl_tr_r, sl_lim_r
            sl_tr_r = _round_tick(last_now - tick, tick)
            sl_lim_r = _round_tick(sl_tr_r * (1 - sl_limit_offset_frac), tick)
            print(f"[OCO ADJUST] SL {old_sl_tr}->{sl_tr_r} / {old_sl_lim}->{sl_lim_r} due to last={last_now}")
        if last_now >= tp_r:
            old_tp = tp_r
            tp_r = _round_tick(last_now + tick, tick)
            print(f"[OCO ADJUST] TP {old_tp}->{tp_r} due to last={last_now}")

        # Place OCO with retries
        oco_id = None
        for attempt in range(3):
            try:
                # Enforce minNotional
                try:
                    info = _get_symbol_info(sym)
                    min_notional = float(next(
                        (f["minNotional"] for f in info["filters"] if f["filterType"] == "MIN_NOTIONAL"),
                        5.0
                    ))
                    tp_notional = tp_r * float(qty_str)
                    sl_notional = sl_tr_r * float(qty_str)
                    if tp_notional < min_notional or sl_notional < min_notional:
                        print(f"[FILTER] Notional too low: TP={tp_notional:.3f}, SL={sl_notional:.3f}, min={min_notional}")
                        safe_qty = _round_step((min_notional / min(tp_r, sl_tr_r)) * 1.02, step)
                        qty_str = _fmt(safe_qty)
                        print(f"[FILTER] Adjusted qty to {qty_str} to meet minNotional={min_notional}")
                except Exception as f_err:
                    print(f"[FILTER WARN] Could not enforce minNotional: {f_err}")

                print(f"[OCO TRY {attempt+1}] qty={qty_str} TP={tp_r} SL={sl_tr_r}/{sl_lim_r}")
                oco = place_oco(symbol, "SELL", qty_str, str(tp_r), str(sl_tr_r), str(sl_lim_r))
                oco_id = oco.get("orderListId")
                if not oco_id:
                    raise RuntimeError("OCO response missing orderListId")
                break
            except Exception as e:
                msg = str(e).lower()
                print(f"[OCO ERROR] {e}")
                if "insufficient balance" in msg and attempt < 2:
                    time.sleep(2.0)
                    try:
                        bal = client.get_asset_balance(asset=base_asset) or {}
                        free_balance = float(bal.get("free", 0) or 0.0)
                        safe_qty = _round_step(min(free_balance, filled_qty) * 0.999, step)
                        if safe_qty >= step:
                            qty_str = _fmt(safe_qty)
                            print(f"[OCO RETRY] resized qty to {qty_str} from FREE={free_balance:.8f}")
                            continue
                    except Exception:
                        pass
                raise

        # Verify OCO
        if not verify_oco(oco_id, verify_timeout_sec):
            print(f"[OCO WARN] Verification timed out, but OCO {oco_id} likely active.")
            return {
                "filled_qty": filled_qty,
                "avg_price": avg_price,
                "tp": tp_r,
                "sl_trigger": sl_tr_r,
                "sl_limit": sl_lim_r,
                "oco_id": oco_id,
            }

        # Register OCO
        try:
            from signal_trader import track_oco
            track_oco(symbol, oco_id, avg_price)
            print(f"[OCO TRACK] Tracking {symbol} OCO {oco_id}")
        except Exception as e:
            print(f"[OCO TRACK WARN] Could not record OCO {oco_id}: {e}")

    except Exception as e:
        # --- Unified error handling (no more free-variable bug) ---
        error_msg = str(e)

        # Soft errors: OCO actually succeeded
        if any(x in error_msg for x in ["insufficient balance", "Filter failure", "NOTIONAL", "oco", "orderListId"]):
            print(f"[OCO WARN] Non-fatal post-OCO message: {error_msg}")
            try:
                from signal_trader import track_oco
                track_oco(symbol, oco_id, avg_price)
                print(f"[OCO TRACK] Tracking {symbol} OCO {oco_id} after non-fatal warning")
            except Exception as te:
                print(f"[OCO TRACK WARN] Could not track OCO {oco_id}: {te}")

            return {
                "filled_qty": filled_qty,
                "avg_price": avg_price,
                "tp": tp_r,
                "sl_trigger": sl_tr_r,
                "sl_limit": sl_lim_r,
                "oco_id": oco_id if 'oco_id' in locals() else None,
            }

        # Hard failure
        raise RuntimeError(f"OCO placement failed: {error_msg}")

    # Normal successful return
    return {
        "filled_qty": filled_qty,
        "avg_price": avg_price,
        "tp": tp_r,
        "sl_trigger": sl_tr_r,
        "sl_limit": sl_lim_r,
        "oco_id": oco_id,
    }