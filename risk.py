"""
risk.py
Эрсдэлийн хяналт: drawdown circuit breaker, стратегийн түр зогсоолт, realized PnL бүртгэл.
"""
from telegram_format import format_block
from settings import *
from state import state
import account
import notifications
import persistence
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def exit_levels(atr_pct):
    """Тухайн coin-ы хэлбэлзэлд тохируулсан гарцын түвшнүүд (%).

    Гарцуудын харьцаа (R-бүтэц) нь config-оос хэвээр гарна: TP нь SL-ийн 1.5
    дахин, partial TP нь 0.667 дахин гэх мэт. ATR нь зөвхөн бүх бүтцийг нэг
    дор сунгаж/агшаана. Ингэснээр шинэ тохиргооны товчлуур нэмэгдэхгүй, ATR
    унтраалттай үед яг өмнөх зан төлөв гарна.

    Тогтмол 3% stop нь тайван coin дээр 6 ATR зайд (бараг хэзээ ч цохихгүй),
    хэлбэлзэлтэй дээр 1 ATR зайд (байнга цохино) байрладаг — өөрөөр хэлбэл
    ижил тоо огт ижил утга илэрхийлдэггүй байв.
    """
    base = {
        "sl": EMERGENCY_SL_PCT,
        "tp": TAKE_PROFIT_PCT,
        "partial": PARTIAL_TP_PCT,
        "trail": TRAILING_ACTIVATION_PCT,
    }
    if not ATR_RISK_SIZING_ENABLED or EMERGENCY_SL_PCT <= 0:
        return base

    atr = utils.safe_float(atr_pct, 0.0)
    if atr <= 0:
        return base

    sl = min(max(ATR_SL_MULTIPLIER * atr, ATR_SL_MIN_PCT), ATR_SL_MAX_PCT)
    scale = sl / EMERGENCY_SL_PCT
    return {
        "sl": sl,
        "tp": TAKE_PROFIT_PCT * scale,
        "partial": PARTIAL_TP_PCT * scale,
        "trail": TRAILING_ACTIVATION_PCT * scale,
    }


def position_margin(balance, sl_pct):
    """Stop цохиход балансын тогтмол хувийг алдахаар маржиныг тооцоолно.

    notional * sl = balance * risk  =>  notional = balance * risk / sl
    Өргөн stop-той (хэлбэлзэлтэй) coin автоматаар бага хэмжээ авна.
    """
    if not ATR_RISK_SIZING_ENABLED or LEVERAGE <= 0:
        return balance * TRADE_ALLOCATION
    sl = utils.safe_float(sl_pct, 0.0)
    if sl <= 0:
        return balance * TRADE_ALLOCATION

    notional = balance * (RISK_PER_TRADE_PCT / 100) / (sl / 100)
    margin = notional / LEVERAGE
    return min(max(margin, balance * MIN_TRADE_ALLOCATION), balance * MAX_TRADE_ALLOCATION)


def record_realized_pnl(strategy, pnl):
    """Хаагдсан арилжааны бодит ашгийг бүртгэнэ.

    Сессийн ашгийг стратегиэс үл хамааран нэмнэ — өмнө нь энэ нь зөвхөн
    update_strategy_performance дотор байсан тул танигдаагүй стратегитай
    (RECOVERED гэх мэт) арилжааны ашиг тайланд огт тусдаггүй байв.
    """
    state.session_realized_pnl += pnl
    update_strategy_performance(strategy, pnl)
    persistence.save_session_state()


def update_strategy_performance(strategy, pnl):
    if strategy not in state.strategy_stats:
        return
    stats = state.strategy_stats[strategy]
    stats["trades"] += 1
    stats["total_pnl"] += pnl
    if pnl > 0:
        stats["wins"] += 1
        stats["consecutive_losses"] = 0
    else:
        stats["losses"] += 1
        stats["consecutive_losses"] += 1
        if ADAPTIVE_STRATEGY and stats["consecutive_losses"] >= CONSECUTIVE_LOSS_LIMIT:
            stats["active"] = False
            stats["paused_cycles"] = STRATEGY_COOLDOWN_CYCLES
            notifications.send_telegram(
                format_block(
                    "STRATEGY PAUSED",
                    "⚠️",
                    [
                        ("Strategy", strategy),
                        ("Loss streak", stats["consecutive_losses"]),
                        ("Pause", f"{STRATEGY_COOLDOWN_CYCLES} cycles"),
                    ]
                )
            )
    persistence.save_strategy_state()
    persistence.save_session_state()


def check_drawdown_circuit_breaker():

    if not MAX_SESSION_DRAWDOWN_PCT or MAX_SESSION_DRAWDOWN_PCT <= 0:
        return

    balance = account.get_usdt_balance()
    if balance <= 0:
        return

    if balance > state.session_peak_balance:
        state.session_peak_balance = balance
        if state.drawdown_lock_active:
            state.drawdown_lock_active = False
        persistence.save_session_state()
        return

    if state.session_peak_balance <= 0:
        return

    drawdown_pct = (state.session_peak_balance - balance) / state.session_peak_balance * 100
    if drawdown_pct >= MAX_SESSION_DRAWDOWN_PCT and not state.safety_lock:
        state.safety_lock = True
        state.drawdown_lock_active = True
        state.drawdown_halt = True
        log.error(f"🚨 MAX DRAWDOWN HIT: {drawdown_pct:.2f}% (limit {MAX_SESSION_DRAWDOWN_PCT}%) — HARD STOP")
        notifications.send_telegram(
            format_block(
                "MAX DRAWDOWN CIRCUIT BREAKER",
                "🚨",
                [
                    ("Peak Balance", f"${state.session_peak_balance:,.2f}"),
                    ("Current Balance", f"${balance:,.2f}"),
                    ("Drawdown", f"{drawdown_pct:.2f}% (limit {MAX_SESSION_DRAWDOWN_PCT:.1f}%)"),
                    ("", ""),
                    ("Статус", "БОТ БҮРМӨСӨН ЗОГСЛОО"),
                    ("Дараагийн алхам", "Бүх позиц хаагдана. Гараар restart хийтэл автоматаар үргэлжлэхгүй"),
                ]
            )
        )


def update_strategy_cooldowns():
    for strategy, stats in state.strategy_stats.items():
        if stats["paused_cycles"] <= 0:
            continue
        stats["paused_cycles"] -= 1
        if stats["paused_cycles"] <= 0:
            stats["active"] = True
            stats["consecutive_losses"] = 0
            persistence.save_strategy_state()
            notifications.send_telegram(
                format_block(
                    "STRATEGY REACTIVATED",
                    "🔄",
                    [("Strategy", strategy)]
                )
            )


def get_active_strategies():
    return [s for s, stats in state.strategy_stats.items() if stats["active"]]
