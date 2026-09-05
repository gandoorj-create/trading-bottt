"""
position_manager.py
Нээлттэй позицын амьдралын мөчлөг: хамгаалалт барих, хянах, хаах, сэргээх.
"""
import time
from telegram_format import format_block, format_section, money
from settings import *
from state import state, STRATEGY_NAMES
import account
import market_data
import notifications
import order_api
import persistence
import risk
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def sync_existing_positions():
    # Алдаа гарвал main() барьж авна — "позиц байхгүй" гэж андуурч
    # хамгаалалтгүй позицуудыг орхихоос сэргийлнэ.
    positions = account.get_positions()
    if not positions:
        return

    saved_trades = persistence.load_saved_trades()

    for pos in positions:
        symbol = pos["symbol"]
        if symbol in state.active_trade_info:
            continue
        amount = pos["positionAmt"]
        side = "BUY" if amount > 0 else "SELL"
        position_side = pos.get("positionSide", "BOTH")
        qty = abs(amount)
        entry = pos["entryPrice"]

        # Хадгалсан бүртгэлээс жинхэнэ стратегийг сэргээх оролдлого. Тал (BUY/SELL)
        # таарахгүй бол өөр арилжаа гэж үзээд RECOVERED хэвээр үлдээнэ.
        saved = saved_trades.get(symbol)
        if isinstance(saved, dict) and saved.get("side") == side and saved.get("strategy") in STRATEGY_NAMES:
            strategy = saved["strategy"]
            opened_at = utils.safe_float(saved.get("opened_at"), time.time())
            opened_at_ms = int(utils.safe_float(saved.get("opened_at_ms"), opened_at * 1000))
            log.info(f"🔄 RESTORED {symbol} → strategy={strategy} (хадгалсан бүртгэлээс)")
        else:
            strategy = "RECOVERED"
            opened_at = time.time()
            opened_at_ms = int(opened_at * 1000)

        # None = жагсаалт уншигдсангүй. Тэр тохиолдолд "хамгаалалттай" гэж
        # таамаглахгүй — хамгаалалтгүй позиц үлдэхээс давхардсан SL/TP дээр нь.
        existing_protection = order_api.get_open_algo_orders(symbol)
        has_protection = bool(existing_protection)

        state.active_trade_info[symbol] = {
            "strategy": strategy,
            "side": side,
            "entry_price": entry,
            "quantity": qty,
            "position_side": position_side,
            "opened_at": opened_at,
            "opened_at_ms": opened_at_ms,
            "entry_order_id": None,
            "sl_order_id": None,
            "tp_order_id": None,
            "recovered": True
        }

        state.dca_info[symbol] = {
            "level": 0,
            "avg_price": entry,
            "base_qty": qty,
            "total_qty": qty
        }

        if not has_protection:
            log.info(f"🔄 RECOVERED {symbol} WITHOUT protection – rebuilding...")
            success, _, _ = rebuild_protection_orders(symbol, side, qty, entry, position_side)
            if not success:
                state.unprotected_symbols.add(symbol)
                notifications.send_telegram(format_block("RECOVERED POSITION WITHOUT PROTECTION", "🚨", [("Symbol", symbol)]))
        else:
            log.info(f"🔄 RECOVERED POSITION: {symbol} (protected)")


def finalize_trade(symbol, trade_data):
    # RECOVERED позицын ашгийг ч заавал тооцно. Өмнө нь энд эрт `return 0.0`
    # хийдэг байсан тул restart-ын дараах позицууд ашигтай хаагдсан ч
    # "Session Realized $0.00" гэж худал харагдаж, хаагдсан мэдэгдэл ч ирдэггүй байв.
    strategy = trade_data.get("strategy", "UNKNOWN")
    opened_at_ms = trade_data.get("opened_at_ms", int(trade_data.get("opened_at", time.time()) * 1000))
    pnl = account.get_trade_realized_pnl(symbol, opened_at_ms)
    risk.record_realized_pnl(strategy, pnl)
    log.info(f"🔴 CLOSED {symbol} | Strategy={strategy} | PnL=${pnl:.2f}")
    notifications.send_telegram(
        format_block(
            "ПОЗИЦ ХААГДЛАА",
            "🟢" if pnl > 0 else "🔴",
            [
                ("Symbol", symbol),
                ("Strategy", strategy),
                ("PnL", money(pnl)),
            ]
        )
    )
    if symbol in state.dca_info:
        del state.dca_info[symbol]
    return pnl


def calculate_trailing_activation(symbol, signal, entry_price):
    try:
        positions = account.get_positions()
    except account.PositionFetchError as e:
        # Mark price нь зөвхөн нарийвчлалд хэрэгтэй — уншигдаагүй бол entry-ээр
        # тооцоолно, эс тэгвээс хамгаалалтын захиалга огт үүсэхгүй үлдэнэ.
        log.warning(f"⚠️ Trailing activation {symbol}: mark price уншигдсангүй, entry ашиглав ({e})")
        positions = []

    mark_price = entry_price
    for pos in positions:
        if pos["symbol"] == symbol:
            if pos["markPrice"] > 0:
                mark_price = pos["markPrice"]
            break
    if signal == "BUY":
        activation = max(entry_price * (1 + TRAILING_ACTIVATION_PCT / 100), mark_price * 1.001)
    else:
        activation = min(entry_price * (1 - TRAILING_ACTIVATION_PCT / 100), mark_price * 0.999)
    return market_data.round_price(symbol, activation)


def rebuild_protection_orders(symbol, side, quantity, entry_price, position_side):
    close_side = "SELL" if side == "BUY" else "BUY"
    if quantity <= 0 or entry_price <= 0:
        return False, None, None

    if side == "BUY":
        tp_price = market_data.round_price(symbol, entry_price * (1 + TAKE_PROFIT_PCT / 100))
        emergency_sl_price = market_data.round_price(symbol, entry_price * (1 - EMERGENCY_SL_PCT / 100))
    else:
        tp_price = market_data.round_price(symbol, entry_price * (1 - TAKE_PROFIT_PCT / 100))
        emergency_sl_price = market_data.round_price(symbol, entry_price * (1 + EMERGENCY_SL_PCT / 100))

    if tp_price is None or emergency_sl_price is None:
        return False, None, None

    # Хуучин SL/TP-г эхлээд цэвэрлэнэ. Цуцлалт бүтэхгүй бол шинэ захиалга дээр нь
    # нэмэгдэж, хуучин үнийн түвшний stop амьд үлдэнэ — тиймээс чимээгүй өнгөрөхгүй.
    cleanup = order_api.cancel_all_symbol_orders(symbol)
    if utils.is_api_error(cleanup.get("algo")):
        log.warning(f"⚠️ {symbol}: хуучин conditional захиалгууд цуцлагдсангүй: {cleanup['algo']}")

    # 1) Hard emergency stop — ALWAYS required. A trailing stop only arms after
    #    price moves in our favour by TRAILING_ACTIVATION_PCT, so a position that
    #    goes straight against us would otherwise have no stop at all.
    sl = order_api.place_stop_loss_order(symbol, close_side, quantity, emergency_sl_price, position_side)
    if utils.is_api_error(sl):
        log.error(f"❌ {symbol}: emergency stop-loss failed: {sl}")
        return False, tp_price, None

    # 2) Take profit — required.
    tp = order_api.place_take_profit_order(symbol, close_side, quantity, tp_price, position_side)
    if utils.is_api_error(tp):
        log.error(f"❌ {symbol}: take-profit failed: {tp}")
        return False, tp_price, None

    # 3) Trailing stop — best effort. Adds upside capture on top of the hard
    #    stop; failure here is not fatal because the emergency stop is live.
    activation_price = calculate_trailing_activation(symbol, side, entry_price)
    if activation_price is not None:
        trailing = order_api.place_trailing_stop_order(symbol, close_side, quantity, TRAILING_CALLBACK_RATE, activation_price, position_side)
        if utils.is_api_error(trailing):
            log.warning(f"⚠️ {symbol}: trailing stop not placed ({trailing}) — emergency SL still active")
            activation_price = None

    state.unprotected_symbols.discard(symbol)
    return True, tp_price, activation_price


def manage_dca():
    if not DCA_ENABLED:
        return


def close_one_position(pos):
    symbol = pos["symbol"]
    amount = utils.safe_float(pos["positionAmt"])
    if abs(amount) <= 0:
        return True
    close_side = "SELL" if amount > 0 else "BUY"
    quantity = market_data.round_quantity(symbol, abs(amount))
    if quantity is None:
        quantity = abs(amount)
        log.warning(f"⚠️ {symbol}: no exchange info — closing with raw position size {quantity}")
    position_side = pos.get("positionSide", "BOTH")
    log.info(f"🔒 CLOSE {symbol} | {close_side} | {quantity} | PositionSide={position_side}")
    result = order_api.place_market_order(symbol, close_side, quantity, reduce_only=True, position_side=position_side)
    if utils.is_api_error(result):
        log.error(f"❌ CLOSE FAILED {symbol}: {result}")
        return False
    log.info(f"✅ CLOSE ORDER SENT {symbol}")
    return True


def close_all_positions_and_verify():
    log.info("\n" + "=" * 70)
    log.info("🔒 CLOSE ALL POSITIONS")
    log.info("=" * 70)
    try:
        positions = account.get_positions()
    except account.PositionFetchError as e:
        # Позиц үлдсэн эсэхийг мэдэхгүй байж "бүгд хаагдлаа" гэж хэлэх нь
        # хамгийн аюултай худал — бүтэлгүйтсэн гэж тооцно.
        log.error(f"🚨 CLOSE ALL: позиц уншиж чадсангүй ({e})")
        return False

    if not positions:
        log.info("✅ No open positions.")
        return True
    symbols = {p["symbol"] for p in positions}

    for symbol in symbols:
        try:
            result = order_api.cancel_all_symbol_orders(symbol)
            log.info(f"🧹 Cancel {symbol}: {result}")
        except Exception as e:
            log.warning(f"⚠️ Cancel error {symbol}: {e}")

    time.sleep(1)

    for pos in positions:
        close_one_position(pos)
        time.sleep(0.4)

    for attempt in range(1, CLOSE_VERIFY_ATTEMPTS + 1):
        time.sleep(CLOSE_VERIFY_DELAY_SEC)
        try:
            remaining = account.get_positions()
        except account.PositionFetchError as e:
            # Баталгаажуулж чадаагүй мөчлөгийг "хаагдсан" гэж үзэхгүй, дахин оролдоно
            log.info(f"⏳ CLOSE VERIFY {attempt}/{CLOSE_VERIFY_ATTEMPTS} | позиц уншигдсангүй ({e})")
            continue
        if not remaining:
            log.info("✅ ALL POSITIONS CLOSED")
            for symbol in symbols:
                try:
                    order_api.cancel_all_symbol_orders(symbol)
                except Exception:
                    pass
            return True
        log.info(f"⏳ CLOSE VERIFY {attempt}/{CLOSE_VERIFY_ATTEMPTS} | Remaining={len(remaining)}")
        for pos in remaining:
            close_one_position(pos)
            time.sleep(0.4)

    try:
        remaining = account.get_positions()
    except account.PositionFetchError as e:
        log.error(f"🚨 POSITION CLOSE UNVERIFIED ({e})")
        return False
    if remaining:
        log.error("🚨 POSITION CLOSE INCOMPLETE")
        return False
    return True


def handle_target_reached(total_unrealized):
    state.safety_lock = True
    balance_before = account.get_usdt_balance()
    notifications.send_telegram(
        format_block(
            "TARGET REACHED!",
            "🎯",
            [
                ("Unrealized PnL", money(total_unrealized)),
                ("Target", f"${TARGET_PROFIT:.2f}"),
                ("", ""),
                ("Статус", "Бүх позицыг хааж байна..."),
            ]
        )
    )
    success = close_all_positions_and_verify()
    if not success:
        notifications.send_telegram(
            format_block(
                "CRITICAL: CLOSE INCOMPLETE",
                "🚨",
                [
                    ("Статус", "Бүх позиц бүрэн хаагдсангүй"),
                    ("Дараагийн алхам", "SAFETY LOCK ACTIVE — шинэ trade нээхгүй"),
                ]
            )
        )
        return False

    time.sleep(2)
    balance_after = account.get_usdt_balance()
    balance_delta = balance_after - balance_before

    target_symbols = list(state.active_trade_info.keys())
    target_realized = 0.0
    for symbol in target_symbols:
        trade_data = state.active_trade_info.pop(symbol, None)
        if not trade_data:
            continue
        target_realized += finalize_trade(symbol, trade_data)

    try:
        final_positions = account.get_positions()
    except account.PositionFetchError as e:
        # Эцсийн шалгалтыг хийж чадаагүй бол амжилттай гэж зарлахгүй
        state.safety_lock = True
        notifications.send_telegram(
            format_block(
                "FINAL SAFETY CHECK UNVERIFIED",
                "🚨",
                [("Статус", "Позиц шалгагдсангүй — шинэ trade нээхгүй"), ("Error", str(e)[:200])]
            )
        )
        return False

    if final_positions:
        state.safety_lock = True
        notifications.send_telegram(
            format_block(
                "FINAL SAFETY CHECK FAILED",
                "🚨",
                [("Статус", "Position үлдсэн — шинэ trade нээхгүй")]
            )
        )
        return False

    notifications.send_telegram(
        format_block(
            "TARGET REALIZED!",
            "✅",
            [
                ("Target Unrealized", money(total_unrealized)),
                ("Realized PnL", money(target_realized)),
                ("Balance change", money(balance_delta)),
                ("New Balance", f"${balance_after:,.2f}"),
                ("Open Positions", 0),
                ("", ""),
                ("Дараагийн алхам", "10 минут cooldown → автомат үргэлжлэл"),
            ]
        )
    )
    return True


def target_cooldown():
    log.info("\n😴 TARGET COOLDOWN")
    cooldown_end = time.time() + TARGET_COOLDOWN_SEC
    while True:
        remaining = cooldown_end - time.time()
        if remaining <= 0:
            break
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        print(f"\r😴 COOLDOWN {minutes:02d}:{seconds:02d}", end="", flush=True)
        time.sleep(5)
    log.info("\n")
    notifications.send_telegram(
        format_block(
            "10 МИНУТЫН COOLDOWN ДУУСЛАА",
            "🚀",
            [("Статус", "Бот дахин ажиллаж, шинэ screening эхэллээ")]
        )
    )


def monitor_positions():
    try:
        positions = account.get_positions()
    except account.PositionFetchError as e:
        # Позицын жагсаалт тодорхойгүй бол юу ч хаагдсан гэж үзэхгүй — эс тэгвээс
        # хамгаалалтын захиалгуудыг амьд позиц дээрээс цуцалчихна.
        log.warning(f"⚠️ Monitor: позиц уншиж чадсангүй — энэ мөчлөгийг алгаслаа ({e})")
        return

    current_symbols = {p["symbol"] for p in positions}
    tracked_symbols = set(state.active_trade_info.keys())

    closed_symbols = tracked_symbols - current_symbols
    for symbol in closed_symbols:
        trade_data = state.active_trade_info.pop(symbol, None)
        if not trade_data:
            continue
        pnl = finalize_trade(symbol, trade_data)
        try:
            order_api.cancel_all_symbol_orders(symbol)
        except Exception:
            pass

    if not positions:
        return

    manage_dca()

    now = time.time()
    if now - state.last_telegram_report_time < TELEGRAM_REPORT_INTERVAL_SEC:
        return

    sections = []
    total_unrealized = 0.0
    for pos in positions:
        symbol = pos["symbol"]
        pnl = pos["unRealizedProfit"]
        total_unrealized += pnl
        trade_data = state.active_trade_info.get(symbol, {})
        strategy = trade_data.get("strategy", "UNKNOWN")
        side = trade_data.get("side", "UNKNOWN")
        dca_level = state.dca_info.get(symbol, {}).get("level", 0)
        sections.append((
            f"🔹 {symbol} ({side}) [DCA: {dca_level}/{DCA_LEVELS}]",
            [
                ("Strategy", strategy),
                ("Entry", f"${pos['entryPrice']:,.6f}"),
                ("Mark", f"${pos['markPrice']:,.6f}"),
                ("PnL", money(pnl)),
                ("Qty", abs(pos["positionAmt"])),
            ]
        ))

    current_balance = account.get_usdt_balance()
    sections.append((
        "📊 НИЙТ",
        [
            ("Unrealized", money(total_unrealized)),
            ("Target", f"${TARGET_PROFIT:.2f}"),
            ("Balance", f"${current_balance:,.2f}"),
            ("Session Realized", money(state.session_realized_pnl)),
        ]
    ))

    notifications.send_telegram(format_section("ПОЗИЦЫН МОНИТОР", "📊", sections))
    state.last_telegram_report_time = now


def safety_recovery():
    if not state.safety_lock:
        return True
    try:
        positions = account.get_positions()
    except account.PositionFetchError as e:
        # Позиц үлдсэн эсэхийг мэдэхгүй бол түгжээг тайлахгүй — тайлчихвал
        # хамгаалалтгүй позиц дээр шинэ арилжаа нэмэгдэх эрсдэлтэй.
        log.warning(f"⚠️ Safety recovery: позиц уншиж чадсангүй — түгжээ хэвээр ({e})")
        return False

    if not positions:
        state.safety_lock = False
        notifications.send_telegram(
            format_block(
                "SAFETY LOCK CLEARED",
                "✅",
                [("Статус", "Position = 0 — trading үргэлжлэхэд бэлэн")]
            )
        )
        return True
    notifications.send_telegram(
        format_block(
            "SAFETY LOCK",
            "🔒",
            [("Статус", "Position үлдсэн — хаалтыг дахин оролдож байна")]
        )
    )
    success = close_all_positions_and_verify()
    if success:
        state.safety_lock = False
        state.active_trade_info.clear()
        persistence.save_session_state()
        notifications.send_telegram(
            format_block(
                "SAFETY RECOVERY SUCCESS",
                "✅",
                [("Статус", "Бүх позиц хаагдлаа — trading үргэлжилнэ")]
            )
        )
        return True
    return False
