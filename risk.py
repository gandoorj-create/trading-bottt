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
from logging_setup import get_logger

log = get_logger(__name__)


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
