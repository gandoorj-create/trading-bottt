"""
bot.py
Оруулах цэг: тохиргоо шалгах, эхлүүлэх, үндсэн давталт.
"""
from datetime import datetime
import os
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

# Нэг алдаа давтагдахад Telegram-ыг хэр олон удаа мэдэгдэх вэ.
ERROR_ALERT_REPEAT_SEC = 1800


class ErrorAlertThrottle:
    """Ижил алдааг давтан мэдэгдэхгүй.

    Гол гогцоо алдаа бүрийн дараа 30 сек унтдаг тул тогтмол алдаа цагт 120
    мессеж болж, Telegram rate-limit-д оруулдаг — тэр үед жинхэнэ дохио
    (drawdown зогсолт) хүрэхгүй. Шинэ төрлийн алдааг тэр дор нь, ижил алдааг
    repeat_sec тутамд нэг л удаа, хэдэн удаа давтагдсаныг нь хамт мэдэгдэнэ.
    """

    # Хэдэн өөр алдааг санах вэ — хязгааргүй өсөхгүйн тулд.
    MAX_TRACKED = 50

    def __init__(self, repeat_sec=ERROR_ALERT_REPEAT_SEC, clock=time.time):
        self.repeat_sec = repeat_sec
        self.clock = clock
        # signature → [сүүлд илгээсэн мөч, түүнээс хойш дарагдсан тоо]. Алдаа
        # бүрийг ТУСАД нь хянана: зөвхөн сүүлийнхийг санавал хоёр алдаа
        # ээлжлэн гарахад бүгд "шинэ" болж шүүлтийг бүрэн тойрно.
        self.seen = {}

    def should_send(self, error):
        """(илгээх эсэх, сүүлийн мэдэгдлээс хойш дарагдсан тоо)."""
        # Traceback биш зөвхөн төрөл+мессеж: мөрийн дугаар өөрчлөгдөхөд ижил
        # алдаа "шинэ" мэт харагдахгүй.
        signature = f"{type(error).__name__}: {error}"[:200]
        now = self.clock()
        entry = self.seen.get(signature)
        if entry is None or now - entry[0] >= self.repeat_sec:
            repeated = entry[1] if entry else 0
            self.seen[signature] = [now, 0]
            if len(self.seen) > self.MAX_TRACKED:
                oldest = min(self.seen, key=lambda key: self.seen[key][0])
                del self.seen[oldest]
            return True, repeated
        entry[1] += 1
        return False, entry[1]


def run_initial_trades():
    """Эхлэх үеийн арилжаа — breaker-ийн шалгалтын ДАРАА.

    Өмнө нь энэ арилжаа гол гогцооны breaker-ээс өмнө ажилладаг байсан тул
    зогссон ботыг redeploy хийхэд 6 хүртэл позиц нээгээд, 30 секундын дараа
    breaker дахин цохиж бүгдийг хаадаг байв — шимтгэл, slippage дэмий.

    Буцаах утга: арилжааг оролдсон эсэх.
    """
    try:
        risk.check_drawdown_circuit_breaker()
    except Exception as e:
        log.error(f"❌ Drawdown check: {e}")
    if state.drawdown_halt or state.safety_lock:
        log.warning("⏸️ Эхлэх арилжааг алгаслаа — бот зогссон төлөвт байна")
        return False

    try:
        selected = screening.screen_coins()
        execution.execute_trades(selected, account.get_usdt_balance())
    except Exception as e:
        error = traceback.format_exc()
        log.error(f"❌ Initial error:\n{error}")
        notifications.send_telegram(format_block("АНХНЫ АЛДАА", "❌", [("Error", str(e)[:400])]))
    return True


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
    # Зогсолт нь дээрх 24 цагийн шалгалтын ГАДНА — хугацаа өнгөрөхөд арилах ёсгүй.
    risk.restore_drawdown_halt(_saved_session, os.environ.get(risk.HALT_RESET_ENV))
    persistence.save_session_state()

    notifications.send_telegram(
        format_block(
            "SMART BOT V2 АСЛАА! (ШИНЭ ҮЗҮҮЛЭЛТҮҮД)",
            "🤖",
            [
                ("Strategies", "6 (SUPERTREND, MACD, BREAKOUT, BOLLINGER, RSI, TREND)"),
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
        # Эхлэхэд портфелийн симуляц — ботын ЖИНХЭНЭ шийдвэрийн кодыг түүхэн
        # лаан дээр ажиллуулж, гарц/хэмжээ/зардлыг бүрэн тооцно. Хэдэн минут
        # үргэлжилдэг тул анхдагчаар унтраалттай; гараар нь `python backtest.py`
        # гэж ажиллуулах нь илүү тохиромжтой.
        try:
            log.info("\n🧪 Портфелийн backtest ажиллуулж байна...")
            _sim, _report = backtest.run(days=BACKTEST_DAYS)
            log.info("\n" + _report)
            backtest.send_report_to_telegram(_report)
        except Exception as e:
            log.error(f"❌ Backtest error: {e}")

    run_initial_trades()

    last_selection_time = time.time()
    performance_report_time = time.time()
    cycle_count = 0
    error_alerts = ErrorAlertThrottle()

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

            # Мэдээний цонх дээр ӨМНӨ нь энд `continue` хийдэг байсан нь
            # drawdown circuit breaker болон target шалгалтыг хамтад нь
            # алгасдаг байв — яг эвентийн үед, өөрөөр хэлбэл тэдгээр
            # хамгаалалт хамгийн хэрэгтэй мөчид. Шинэ арилжааг зогсоох
            # хяналт нь execute_trades дотор байна.
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

            if state.news_mode_active:
                # Screening-ийг хойшлуулна (last_selection_time-ыг урагшлуулахгүй
                # тул цонх хаагдмагц шууд ажиллана). Позицын хяналт, drawdown,
                # target бүгд дээр нь хэвийн үргэлжилсэн.
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
            send, repeated = error_alerts.should_send(e)
            if send:
                rows = [("Traceback", error[:500])]
                if repeated:
                    rows.append(("Давтагдсан", f"сүүлийн мэдэгдлээс хойш {repeated} удаа"))
                try:
                    notifications.send_telegram(format_block("ГОЛ АЛДАА", "❌", rows))
                except Exception:
                    pass
            time.sleep(30)


if __name__ == "__main__":
    main()
