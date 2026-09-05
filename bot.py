"""
bot.py
Оруулах цэг: тохиргоо шалгах, эхлүүлэх, үндсэн давталт.
"""
from datetime import datetime
import time
import traceback
from telegram_format import format_block
from settings import *
from state import state, STRATEGY_NAMES
import account
import backtest
import binance_client
import execution
import market_data
import news
import notifications
import persistence
import position_manager
import reports
import risk
import screening
import utils
from logging_setup import get_logger, setup_logging

log = get_logger(__name__)


def main():
    # Log тохиргоог хамгийн түрүүнд — эс тэгвээс эхний мөрүүд цаг хугацаагүй гарна.
    # STATE_DIR нь persistent volume дээр байвал log файл руу ч бичнэ.
    log_path = setup_logging(STATE_DIR, STATE_DIR_IS_PERSISTENT)

    log.info("=" * 70)
    log.info("🤖 SMART BOT V2 (SUPERTREND + CHOP + MTF + VWAP + FUNDING)")
    log.info("🎯 UNREALIZED $300 → REALIZED")
    log.info("😴 10 MIN COOLDOWN")
    log.info("🔄 AUTO RESUME")
    log.info(f"📝 Log: консол{f' + {log_path}' if log_path else ' (файл руу бичихгүй)'}")
    log.info("=" * 70)

    try:
        validate_config()
    except Exception as e:
        log.error(f"❌ CONFIG ERROR: {e}")
        return

    persistence.check_state_storage()

    binance_client.sync_server_time()

    try:
        market_data.load_exchange_info()
        account.get_position_mode()
    except Exception as e:
        log.warning(f"⚠️ Exchange setup: {e}")

    persistence.load_strategy_state()

    try:
        position_manager.sync_existing_positions()
    except Exception as e:
        log.error(f"❌ Position sync: {e}")

    try:
        state.session_start_balance = account.get_usdt_balance()
        state.cycle_start_balance = state.session_start_balance
        state.last_cycle_balance = state.session_start_balance
        state.session_peak_balance = state.session_start_balance
    except Exception:
        state.session_start_balance = 0.0
        state.cycle_start_balance = 0.0
        state.last_cycle_balance = 0.0
        state.session_peak_balance = 0.0
    state.cycle_start_time = time.time()

    # Restore the drawdown high-water mark. Without this, a restart after a loss
    # resets the peak to the (lower) current balance and the circuit breaker
    # silently forgives the drawdown. Only trust a snapshot < 24h old.
    # NOTE: on Railway this needs a mounted volume to survive redeploys.
    _saved_session = persistence.load_session_state()
    if _saved_session and (time.time() - utils.safe_float(_saved_session.get("saved_at"), 0)) < 86400:
        _restored_peak = utils.safe_float(_saved_session.get("session_peak_balance"), 0.0)
        if _restored_peak > state.session_peak_balance:
            state.session_peak_balance = _restored_peak
        state.session_realized_pnl = utils.safe_float(_saved_session.get("session_realized_pnl"), 0.0)
        log.info(f"♻️ Restored session state — peak ${state.session_peak_balance:,.2f}, realized ${state.session_realized_pnl:,.2f}")
    persistence.save_session_state()

    notifications.send_telegram(
        format_block(
            "SMART BOT V2 АСЛАА! (ШИНЭ ҮЗҮҮЛЭЛТҮҮД)",
            "🤖",
            [
                ("Strategies", "6 (SUPERTREND, MACD, GRID, BOLLINGER, RSI, TREND)"),
                ("Regime", "CHOP Index (38.2/61.8)"),
                ("Trend Signal", "Supertrend (EMA-г орлосон)"),
                ("Filters", "MTF (4h/1h) + VWAP + Funding Rate"),
                ("Leverage", f"{LEVERAGE}x"),
                ("Allocation", f"{TRADE_ALLOCATION * 100:.0f}%"),
                ("Target", f"${TARGET_PROFIT:.2f}"),
                ("Max Drawdown", f"{MAX_SESSION_DRAWDOWN_PCT:.1f}%" if MAX_SESSION_DRAWDOWN_PCT else "OFF"),
                ("", ""),
                ("Target хүрэхэд", "TP/SL цуцлаад бүх позиц хаана"),
                ("Дараа нь", "10 мин cooldown → автомат үргэлжлэл"),
            ]
        )
    )

    if BACKTEST_ENABLED:
        try:
            log.info("\n🧪 Running initial backtest for all strategies...")
            test_symbols = SYMBOLS_POOL[:2]
            for strategy in STRATEGY_NAMES:
                if strategy == "GRID_TRADING":
                    continue
                for symbol in test_symbols:
                    report = backtest.run_backtest(symbol, strategy, days=BACKTEST_DAYS, interval=BACKTEST_INTERVAL)
                    if report and "error" not in report.lower() and "хангалттай" not in report:
                        notifications.send_telegram(report)
                    time.sleep(1)
        except Exception as e:
            log.error(f"❌ Backtest error: {e}")

    try:
        selected = screening.screen_coins()
        execution.execute_trades(selected, account.get_usdt_balance())
    except Exception as e:
        error = traceback.format_exc()
        log.error(f"❌ Initial error:\n{error}")
        notifications.send_telegram(format_block("АНХНЫ АЛДАА", "❌", [("Error", str(e)[:400])]))

    last_selection_time = time.time()
    performance_report_time = time.time()
    cycle_count = 0

    while True:
        try:
            current_time = time.time()

            if state.drawdown_halt:
                try:
                    remaining = account.get_positions()
                    if remaining:
                        position_manager.close_all_positions_and_verify()
                except Exception as e:
                    log.error(f"❌ Drawdown halt cleanup: {e}")
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            if state.safety_lock:
                position_manager.safety_recovery()
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            try:
                news.check_news_status()
            except Exception as e:
                log.warning(f"⚠️ News check error: {e}")

            if state.news_mode_active:
                try:
                    position_manager.monitor_positions()
                except Exception as e:
                    log.error(f"❌ Monitor error during news: {e}")
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            try:
                risk.check_drawdown_circuit_breaker()
            except Exception as e:
                log.error(f"❌ Drawdown check: {e}")
            if state.safety_lock:
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            try:
                position_manager.monitor_positions()
            except Exception as e:
                log.error(f"❌ Monitor: {e}")

            try:
                positions = account.get_positions()
                total_unrealized = sum(p["unRealizedProfit"] for p in positions)
            except Exception as e:
                log.error(f"❌ Target check: {e}")
                total_unrealized = 0.0

            log.info(f"📡 {datetime.now().strftime('%H:%M:%S')} | Positions={len(positions) if 'positions' in locals() else 0} | Unrealized=${total_unrealized:.2f} / ${TARGET_PROFIT:.2f}")

            if total_unrealized >= TARGET_PROFIT:
                success = position_manager.handle_target_reached(total_unrealized)
                if success:
                    position_manager.target_cooldown()
                    state.active_trade_info.clear()
                    state.safety_lock = False
                    state.cycle_start_time = time.time()
                    state.cycle_start_balance = account.get_usdt_balance()
                    state.last_cycle_balance = state.cycle_start_balance
                    last_selection_time = time.time()

                    try:
                        selected = screening.screen_coins()
                        execution.execute_trades(selected, account.get_usdt_balance())
                    except Exception as e:
                        log.error(f"❌ Auto-resume screening error: {e}")
                        notifications.send_telegram(format_block("AUTO RESUME ERROR", "❌", [("Error", str(e)[:400])]))
                    continue
                else:
                    state.safety_lock = True
                    time.sleep(MONITOR_INTERVAL_SEC)
                    continue

            if current_time - last_selection_time >= SELECTION_INTERVAL_MINUTES * 60:
                cycle_count += 1
                log.info("\n" + "=" * 70)
                log.info(f"🔄 CYCLE #{cycle_count}")
                log.info(datetime.now())
                log.info("=" * 70)

                try:
                    reports.send_cycle_summary()
                except Exception as e:
                    log.error(f"❌ Summary: {e}")

                try:
                    risk.update_strategy_cooldowns()
                except Exception as e:
                    log.error(f"❌ Cooldown: {e}")

                try:
                    selected = screening.screen_coins()
                except Exception as e:
                    log.error(f"❌ Screening: {e}")
                    selected = []
                    notifications.send_telegram("⚠️ Скрининг хийхэд алдаа гарлаа. Дараагийн циклд дахин оролдоно.")

                try:
                    execution.execute_trades(selected, account.get_usdt_balance())
                except Exception as e:
                    log.error(f"❌ Execute: {e}")
                    notifications.send_telegram(format_block("АРИЛЖААНЫ АЛДАА", "❌", [("Error", str(e)[:400])]))

                try:
                    reports.send_performance_report()
                except Exception as e:
                    log.error(f"❌ Performance: {e}")

                last_selection_time = current_time

            if current_time - performance_report_time >= 86400:
                try:
                    reports.send_performance_report()
                except Exception as e:
                    log.error(f"❌ Daily report: {e}")
                performance_report_time = current_time

            time.sleep(MONITOR_INTERVAL_SEC)

        except KeyboardInterrupt:
            log.info("\n🛑 BOT STOPPED")
            notifications.send_telegram(format_block("БОТ ЗОГСЛОО", "🛑", [("Учир", "KeyboardInterrupt")]))
            break

        except Exception as e:
            error = traceback.format_exc()
            log.error(f"❌ MAIN ERROR\n{error}")
            try:
                notifications.send_telegram(format_block("ГОЛ АЛДАА", "❌", [("Traceback", error[:500])]))
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
