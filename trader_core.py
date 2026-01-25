# trader_core.py
import re
import time
import math
import os
import asyncio
from decimal import Decimal, ROUND_DOWN

# Local imports
import trading_shared as ts
import services as sv
from market_cap_checker import MarketCapChecker
from live_trade_executor import (
    place_bracket_atomic,
    place_oco,
    place_stop_loss_market_sell,
    place_trailing_take_profit_market_sell,
    _fmt,
    _get_tick_and_step,
    execute_limit_buy,
    execute_market_buy,
    client as bin_client  # Keeps the synced client fix
)

# Helpers for formatting
def round_amt(q, step):
    if step <= 0:
        return q
    return float(Decimal(str(q)).quantize(Decimal(str(step)), rounding=ROUND_DOWN))

class Trader:
    def __init__(self, binance: sv.BinanceSpot, tg_client, notifier: sv.Notifier):
        self.x = binance
        self.tg = tg_client
        self.n = notifier
        self.market_cap_checker = MarketCapChecker()

    async def on_signal(self, sig: ts.ParsedSignal):
        ts.maybe_reload_settings()
        s = ts.SETTINGS

        # === Signal debug ===
        ts.emit("signal_parsed", {
            "currency": sig.currency_display,
            "entry": sig.entry,
            "sl": sig.stop,
            "tp1": sig.tps.tp1,
            "tp2": sig.tps.tp2,
            "tp3": sig.tps.tp3
        })

        await self.n.send(
            self.tg,
            f"🚀 *New Signal Detected!*\n"
            f"Currency: `{sig.currency_display}`\n"
            f"Entry: `${sig.entry}`\n"
            f"Stop Loss: `${sig.stop}`\n"
            f"TP1–TP3: `${sig.tps.tp1}`, `${sig.tps.tp2}`, `${sig.tps.tp3}`"
        )

        # === Pair Resolution ===
        base = sig.symbol_hint or sig.currency_display.split("/")[0].strip()
        base_clean = re.sub(r"[^A-Za-z0-9 ]", "", base).strip().upper()
        quote = ts.SETTINGS.quote_asset.upper()
        symbol = None

        if "/" in sig.currency_display:
            direct = sig.currency_display.replace(" ", "").upper()
            if direct in self.x.exchange.markets:
                symbol = direct

        if not symbol:
            paren_match = re.search(r"\(([A-Z0-9]+/[A-Z0-9]+)\)", sig.currency_display)
            if paren_match:
                candidate = paren_match.group(1).upper()
                if candidate in self.x.exchange.markets:
                    symbol = candidate

        if not symbol:
            alias_symbol = ts.TOKEN_ALIASES.get(base_clean)
            if alias_symbol:
                found = self.x.find_market(alias_symbol, quote)
                if found:
                    symbol = found

        if not symbol:
            symbol = self.x.find_market(base_clean, quote)

        if not symbol:
            ts.emit("error", {"msg": f"Pair not found for {sig.currency_display}"})
            await self.n.send(self.tg, f"❌ Pair not found for {sig.currency_display}")
            return

        await self.n.send(self.tg, f"✅ Pair resolved: *{symbol}*")

        # === Market Cap Filter ===
        if s.market_cap_filter_enabled:
            min_cap = s.market_cap_min
            max_cap = s.market_cap_max
            base_symbol = symbol.split("/")[0]
            passes, market_cap = self.market_cap_checker.check_filter(base_symbol, min_cap, max_cap)
            
            if not passes:
                reason = ""
                if market_cap is None:
                    reason = "Could not verify market cap"
                elif min_cap > 0 and market_cap < min_cap:
                    reason = f"Market cap ${market_cap:,.0f} < min ${min_cap:,.0f}"
                elif max_cap > 0 and market_cap > max_cap:
                    reason = f"Market cap ${market_cap:,.0f} > max ${max_cap:,.0f}"
                
                await self.n.send(self.tg, f"⏭️ *Skipped: Market Cap Filter*\nReason: {reason}")
                ts.emit("skip_market_cap", {"symbol": symbol, "market_cap": market_cap, "reason": reason})
                return
            await self.n.send(self.tg, f"✅ Market cap: ${market_cap:,.0f}")
            
        # === Duplicate signal protection ===
        if not hasattr(self, "_recent_signals"):
            self._recent_signals = []
        now = time.time()
        self._recent_signals = [(sym, ent, ts_val) for (sym, ent, ts_val) in self._recent_signals if now - ts_val < 180]
        symbol_clean = symbol.replace(" ", "").upper()
        entry_price = round(float(sig.entry), 6)

        for sym, ent, ts_val in self._recent_signals:
            if sym == symbol_clean and abs(ent - entry_price) < 1e-6:
                await self.n.send(self.tg, f"⚠️ Duplicate signal ignored for {symbol_clean}")
                ts.emit("skip_duplicate", {"symbol": symbol_clean, "entry": sig.entry})
                return
        self._recent_signals.append((symbol_clean, entry_price, now))

        # === Balance and sizing ===
        quote_token = symbol.split("/")[1]
        free_q = self.x.fetch_free_quote(quote_token)

        if s.override_capital_enabled:
            cap_pct = s.capital_entry_pct_default
        else:
            cap_pct = sig.capital_pct if sig.capital_pct is not None else s.capital_entry_pct_default

        spend = free_q * cap_pct
        if not s.dry_run and spend < s.min_notional_usdt:
            ts.emit("skip", {"msg": f"Not enough quote: {free_q:.2f}"})
            await self.n.send(self.tg, f"⚠️ Not enough quote balance")
            return

        last = self.x.fetch_price(symbol)
        acceptable = abs(last - sig.entry) / sig.entry <= s.max_slippage_pct

        if sig.stop is None:
            default_sl = getattr(s, 'default_sl_pct', 0.10)
            effective_sl_pct = default_sl + s.max_slippage_pct
            sig.stop = float(sig.entry) * (1.0 - effective_sl_pct)
            await self.n.send(self.tg, f"⚠️ No SL in signal — using default: ${sig.stop:.6f}")

        if (not acceptable) and (not s.use_limit_if_slippage_exceeds):
            await self.n.send(self.tg, f"⏸️ Skipped (slippage too high)")
            ts.emit("skip_slippage", {"symbol": symbol})
            return

        amt_step, _ = self.x.lot_step_info(symbol)
        px_for_size = last if acceptable or not s.use_limit_if_slippage_exceeds else sig.entry
        amount = round_amt(spend / px_for_size, amt_step)

        if amount <= 0:
            await self.n.send(self.tg, "❌ Computed amount is zero")
            return

        # === Execution ===
        is_live = not s.dry_run
        is_testnet = getattr(s, "use_testnet", False)
        mode_label = "testnet" if (is_live and is_testnet) else "mainnet" if is_live else "sim"

        if s.dry_run:
            sim_tp = float(sig.tps.tp1)
            sim_sl = float(sig.stop)
            if s.override_tp_enabled:
                sim_tp = last * (1.0 + float(s.override_tp_pct))
            if s.override_sl_enabled:
                if s.override_sl_as_absolute:
                    sim_sl = last - float(s.override_sl_pct)
                else:
                    sim_sl = last * (1.0 - float(s.override_sl_pct))
            
            profit_pct = ((sim_tp / last) - 1) * 100
            loss_pct = ((last / sim_sl) - 1) * 100

            await self.n.send(
                self.tg,
                f"🧪 *SIMULATION ONLY — No order placed*\n"
                f"Pair: {symbol}\n"
                f"Balance: {free_q:.4f} {quote_token}\n"
                f"Capital %: {cap_pct*100:.2f}%\n"
                f"Spend: {spend:.4f} {quote_token}\n"
                f"Price: ${last:.6f}\n"
                f"Amount: {amount} {symbol.split('/')[0]}\n"
                f"─────────────────\n"
                f"🎯 TP: ${sim_tp:.6f} (+{profit_pct:.2f}%)\n"
                f"🛑 SL: ${sim_sl:.6f} (-{loss_pct:.2f}%)\n"
                f"{'⚙️ Override enabled' if (s.override_tp_enabled or s.override_sl_enabled) else ''}"
            )
            ts.emit("debug", {"msg": "STOP BEFORE BUY — SIMULATION MODE"})
            return

        # LIVE EXECUTION
        try:
            initial_tp = float(sig.tps.tp1)
            initial_sl = float(sig.stop)
            use_override_direct = getattr(s, "override_tp_enabled", False) or getattr(s, "override_sl_enabled", False)

            if (not acceptable) and s.use_limit_if_slippage_exceeds:
                # Limit Buy Logic
                from live_trade_executor import execute_limit_buy
                tif = int(s.limit_time_in_force_sec)
                loop = asyncio.get_running_loop()

                def notify_limit_placed(order_id):
                    loop.call_soon_threadsafe(
                        asyncio.create_task, 
                        self.n.send(
                            self.tg, 
                            f"🟡 LIMIT order placed (Slippage > Max)\n"
                            f"Pair: {symbol}\n"
                            f"Limit: ${float(sig.entry):.6f}\n"
                            f"TIF: {tif}s\n"
                            f"OrderId: {order_id}"
                        )
                    )

                import functools
                fn = functools.partial(execute_limit_buy, symbol=symbol, usd_amount=spend, limit_price=float(sig.entry), tif_sec=tif, on_placed=notify_limit_placed)
                filled_qty, actual_fill_price, limit_oid = await asyncio.to_thread(fn)

                if not filled_qty:
                    # ✅ RESTORED: Detailed Limit Cancel Message
                    await self.n.send(
                        self.tg,
                        f"⏸️ LIMIT order canceled (not filled)\n"
                        f"Pair: {symbol}\n"
                        f"Limit: ${float(sig.entry):.6f}\n"
                        f"Waited: {tif}s\n"
                        f"OrderId: {limit_oid}"
                    )
                    ts.emit("limit_cancel", {"symbol": symbol})
                    return
                
                res = {
                    "filled_qty": float(filled_qty),
                    "avg_price": float(actual_fill_price),
                    "tp": float(sig.tps.tp1),
                    "sl_trigger": float(sig.stop),
                    "sl_limit": None,
                    "oco_id": None,
                }
            else:
                # Market Buy / Bracket
                if use_override_direct:
                    from live_trade_executor import execute_market_buy
                    filled_qty, actual_fill_price = execute_market_buy(symbol, spend)
                    res = {
                        "filled_qty": filled_qty,
                        "avg_price": actual_fill_price,
                        "tp": float(sig.tps.tp1),
                        "sl_trigger": float(sig.stop),
                        "sl_limit": None,
                        "oco_id": None,
                    }
                else:
                    if getattr(s, "exit_mode", "fixed_oco") == "trailing_tp":
                        from live_trade_executor import execute_market_buy
                        filled_qty, actual_fill_price = execute_market_buy(symbol, spend)
                        res = {
                            "avg_price": float(actual_fill_price),
                            "filled_qty": float(filled_qty),
                            "tp": float(sig.tps.tp1),
                            "sl_trigger": float(sig.stop),
                            "oco_id": None,
                        }
                    else:
                        res = place_bracket_atomic(symbol, spend, float(sig.entry), float(sig.tps.tp1), float(sig.stop))

            # Post-Entry Logic (Override, OCO, Trailing)
            actual_fill_price = float(res['avg_price'])
            filled_qty = float(res['filled_qty'])
            
            final_tp = res['tp']
            final_sl = res['sl_trigger']
            needs_override = False

            if getattr(s, "override_tp_enabled", False):
                final_tp = round(actual_fill_price * (1.0 + float(s.override_tp_pct)), 8)
                needs_override = True

            if getattr(s, "override_sl_enabled", False):
                if getattr(s, "override_sl_as_absolute", False):
                    final_sl = round(actual_fill_price - float(s.override_sl_pct), 8)
                else:
                    final_sl = round(actual_fill_price * (1.0 - float(s.override_sl_pct)), 8)
                needs_override = True

            # Trailing Mode
            if getattr(s, "exit_mode", "fixed_oco") == "trailing_tp":
                safe_qty = sv.get_safe_sell_qty(bin_client, symbol, float(filled_qty))
                
                # Fixed SL
                sl_order = await asyncio.to_thread(place_stop_loss_market_sell, symbol, float(safe_qty), float(final_sl))
                sl_id = sl_order.get("orderId")
                activation_price = float(actual_fill_price) * (1.0 + float(s.trailing_tp_activation_pct))

                await self.n.send(self.tg, f"✅ Filled {symbol} @ {actual_fill_price}\n🛑 Fixed SL: {final_sl}\n🎯 Trailing Act: {activation_price}")

                async def activate_trailing_logic(sym, qty, act_px, current_sl_id):
                    ts.emit("monitor_started", {"symbol": sym, "activation": act_px})
                    while True:
                        try:
                            ticker = await asyncio.to_thread(self.x.exchange.fetch_ticker, sym)
                            curr_px = float(ticker['last'])
                            if curr_px >= act_px:
                                await self.n.send(self.tg, f"🎯 Activation Hit for {sym}. Swapping to Trailing.")
                                await asyncio.to_thread(self.x.exchange.cancel_order, current_sl_id, sym)
                                trailing_order = await asyncio.to_thread(place_trailing_take_profit_market_sell, sym, qty, None, float(s.trailing_tp_pullback_pct))
                                tp_id = trailing_order.get("orderId", "Unknown")
                                await self.n.send(self.tg, f"🚀 Trailing TP Active ({tp_id})")
                                break
                        except Exception as e:
                            print(f"Watcher Error: {e}")
                        await asyncio.sleep(2)

                asyncio.create_task(activate_trailing_logic(symbol, safe_qty, activation_price, sl_id))
                return

            # Override Mode
            if needs_override:
                if res.get("oco_id"):
                    try:
                        self.x.exchange.cancel_order(res["oco_id"], symbol)
                    except Exception:
                        pass
                
                time.sleep(1.0)
                bal = bin_client.get_asset_balance(asset=symbol.split("/")[0], recvWindow=60000)
                safe_qty = sv.get_safe_sell_qty(bin_client, symbol, filled_qty)
                final_sl_limit = round(final_sl * 0.9999, 8)
                
                new_oco = place_oco(symbol, "SELL", _fmt(safe_qty), str(final_tp), str(final_sl), str(final_sl_limit))
                new_oco_id = new_oco.get("orderListId")
                ts.track_oco(symbol, new_oco_id, actual_fill_price)
                
                profit_pct = ((final_tp / actual_fill_price) - 1) * 100
                loss_pct = ((actual_fill_price / final_sl) - 1) * 100

                # ✅ RESTORED: Detailed Override Message
                await self.n.send(
                    self.tg,
                    f"✅ BUY filled {safe_qty:.8f} {symbol} @ ${actual_fill_price:.6f} ({mode_label})\n"
                    f"⚙️ Override applied:\n"
                    f"   • TP ${final_tp:.6f} (+{profit_pct:.2f}%)\n"
                    f"   • SL ${final_sl:.6f} (-{loss_pct:.2f}%)\n"
                    f"🆔 OCO ID: {new_oco_id}"
                )

            else:
                # Standard OCO
                if (not acceptable) and s.use_limit_if_slippage_exceeds and (res.get("oco_id") is None):
                    from live_trade_executor import place_oco_after_fill
                    oco_res = place_oco_after_fill(symbol, float(filled_qty), float(actual_fill_price), float(res["tp"]), float(res["sl_trigger"]))
                    res["oco_id"] = oco_res.get("oco_id")
                    res["sl_limit"] = oco_res.get("sl_limit")

                sl_lim = res.get("sl_limit")
                sl_lim_txt = f"{float(sl_lim):.6f}" if sl_lim is not None else "N/A"

                # ✅ RESTORED: Detailed Standard Message
                await self.n.send(
                    self.tg,
                    f"✅ BUY filled {filled_qty:.8f} {symbol} @ ${actual_fill_price:.6f} ({mode_label})\n"
                    f"🎯 OCO set → TP ${float(res['tp']):.6f}, SL ${float(res['sl_trigger']):.6f}/{sl_lim_txt}\n"
                    f"🆔 OCO ID: {res['oco_id']}"
                )
                
                if res.get("oco_id"):
                    ts.track_oco(symbol, res["oco_id"], actual_fill_price)

        except Exception as e:
            await self.n.send(self.tg, f"❌ Execution failed: {e}")
            ts.emit("error", {"msg": str(e)})
            await ts.log_error(f"Trade error: {e}")