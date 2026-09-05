"""
execution.py
Сонгогдсон signal-уудыг захиалга болгон гүйцэтгэх.
"""
import time
from telegram_format import format_block, money
from settings import *
from state import state
import account
import binance_client
import market_data
import notifications
import order_api
import persistence
import position_manager
import risk
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def execute_trades(selected_coins, total_balance):
    if state.safety_lock:
        log.info("🔒 SAFETY LOCK: new trades disabled")
        return
    if not selected_coins:
        return

    positions = account.get_positions()
    existing_symbols = {p["symbol"] for p in positions}
    current_margin_used = 0.0
    for pos in positions:
        actual_lev = account.get_actual_leverage(pos["symbol"])
        current_margin_used += abs(pos["positionAmt"]) * pos["entryPrice"] / actual_lev
    max_margin = total_balance * MAX_TOTAL_MARGIN_USAGE

    for coin in selected_coins:
        if state.safety_lock:
            return
        symbol = coin["symbol"]
        strategy = coin["strategy"]
        signal = coin["signal"]
        if signal not in ["BUY", "SELL"]:
            continue
        if symbol in existing_symbols:
            log.info(f"⏸️ {symbol}: already has position")
            continue
        if total_balance < MIN_BALANCE_USDT:
            notifications.send_telegram(format_block("БАЛАНС БАГА", "⚠️", [("Balance", f"${total_balance:.2f}")]))
            return

        margin = total_balance * TRADE_ALLOCATION
        if symbol in state.unprotected_symbols:
            log.info(f"⏸️ {symbol}: unprotected, skip new trade")
            continue

        if current_margin_used + margin > max_margin:
            log.info(f"⏸️ {symbol}: portfolio margin limit")
            continue

        if not account.ensure_leverage(symbol, LEVERAGE):
            continue

        price = coin["price"]
        notional = margin * LEVERAGE
        raw_quantity = notional / price
        quantity = market_data.round_quantity(symbol, raw_quantity)
        if quantity is None:
            continue
        info = market_data.get_symbol_info(symbol)
        if info and info.get("minQty"):
            min_qty = utils.safe_float(info.get("minQty"))
            if min_qty > 0 and quantity < min_qty:
                log.info(f"⏸️ {symbol}: quantity below minQty")
                continue
        if not market_data.check_min_notional(symbol, price, quantity):
            continue
        if quantity <= 0:
            continue

        try:
            order_api.cancel_all_symbol_orders(symbol)
        except Exception as e:
            log.warning(f"⚠️ Order cleanup {symbol}: {e}")

        order_side = "BUY" if signal == "BUY" else "SELL"
        close_side = "SELL" if signal == "BUY" else "BUY"
        position_side = "LONG" if signal == "BUY" else "SHORT"
        log.info(f"\n🚀 OPEN {symbol}\nStrategy={strategy}\nSignal={signal}\nQty={quantity}")

        order = order_api.place_market_order(symbol, order_side, quantity, reduce_only=False, position_side=position_side)
        if utils.is_api_error(order):
            notifications.send_telegram(format_block("ORDER FAILED", "❌", [("Symbol", symbol), ("Error", str(order)[:300])]))
            continue

        time.sleep(0.5)
        try:
            current_positions = account.get_positions()
        except account.PositionFetchError as e:
            # Захиалга аль хэдийн илгээгдсэн — жагсаалт уншигдаагүй бол доорх
            # fill шалгалт руу шилжиж, захиалгын хариунаас хамгаалалт барина.
            log.warning(f"⚠️ {symbol}: захиалгын дараах позиц уншигдсангүй ({e})")
            current_positions = []
        actual_position = None
        for p in current_positions:
            if p["symbol"] == symbol:
                actual_position = p
                break

        if actual_position:
            entry_price = actual_position["entryPrice"]
            actual_quantity = abs(actual_position["positionAmt"])
            actual_position_side = actual_position.get("positionSide", "BOTH")
        else:
            # No position appeared. Only trust the order response if it actually
            # filled — otherwise we would fabricate a phantom position and build
            # protection orders on a stale screening price.
            executed_qty = utils.safe_float(order.get("executedQty"), 0.0)
            avg_price = utils.safe_float(order.get("avgPrice"), 0.0)
            if executed_qty <= 0 or avg_price <= 0:
                log.warning(f"⚠️ {symbol}: market order did not fill (status={order.get('status')}) — skipping")
                notifications.send_telegram(format_block("ORDER NOT FILLED", "⚠️", [
                    ("Symbol", symbol),
                    ("Status", str(order.get("status"))),
                ]))
                try:
                    order_api.cancel_all_symbol_orders(symbol)
                except Exception:
                    pass
                continue
            entry_price = avg_price
            actual_quantity = executed_qty
            actual_position_side = "BOTH" if not account.get_position_mode() else position_side
        if entry_price <= 0:
            entry_price = price
        opened_at_ms = binance_client.current_timestamp_ms()


        success, tp_price, activation_price = position_manager.rebuild_protection_orders(symbol, signal, actual_quantity, entry_price, actual_position_side)
        if not success:
            notifications.send_telegram(format_block("PROTECTION FAILED", "🚨", [("Symbol", symbol), ("Action", "Closing position")]))
            close_result = order_api.place_market_order(symbol, close_side, actual_quantity, reduce_only=True, position_side=actual_position_side)
            if utils.is_api_error(close_result):
                state.safety_lock = True
                notifications.send_telegram(format_block("CRITICAL CLOSE FAILED", "🚨", [("Symbol", symbol)]))
                continue
            try:
                order_api.cancel_all_symbol_orders(symbol)
            except Exception:
                pass
            existing_symbols.discard(symbol)
            pnl = account.get_trade_realized_pnl(symbol, opened_at_ms)
            risk.record_realized_pnl(strategy, pnl)
            notifications.send_telegram(format_block("EMERGENCY CLOSED", "⚠️", [("Symbol", symbol), ("PnL", money(pnl))]))
            continue

        state.active_trade_info[symbol] = {
            "strategy": strategy,
            "side": signal,
            "entry_price": entry_price,
            "quantity": actual_quantity,
            "position_side": actual_position_side,
            "opened_at": time.time(),
            "opened_at_ms": opened_at_ms,
            "entry_order_id": order.get("orderId"),
            "sl_order_id": None,
            "tp_order_id": None,
            "recovered": False
        }
        # Шинэ позицын стратегийг тэр дороо дискэнд бичнэ — үүний дараа шууд
        # restart болсон ч энэ арилжаа "RECOVERED" болж танигдахгүй.
        persistence.save_session_state()
        existing_symbols.add(symbol)
        current_margin_used += margin

        notifications.send_telegram(
            format_block(
                "ШИНЭ ПОЗИЦ НЭЭГДЛЭЭ",
                "🚀",
                [
                    ("Symbol", symbol),
                    ("Signal", f"{signal} ({position_side})"),
                    ("Strategy", strategy),
                    ("", ""),
                    ("Entry", f"${entry_price:,.6f}"),
                    ("Qty", actual_quantity),
                    ("Margin", f"${margin:.2f} ({LEVERAGE}x)"),
                    ("", ""),
                    ("Take Profit", f"${tp_price:,.6f}"),
                    ("Trailing", f"{TRAILING_CALLBACK_RATE}% @ ${activation_price:,.6f}" if activation_price else "not set — emergency SL active"),
                    ("", ""),
                    ("Score", f"{coin['score']:.2f}"),
                    ("ADX / RSI", f"{coin['adx']:.1f} / {coin['rsi']:.1f}"),
                    ("Regime", coin["regime"]),
                ]
            )
        )
        time.sleep(0.5)
