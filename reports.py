"""
reports.py
Telegram тайлангууд.
"""
import time
from telegram_format import format_block, format_section, money
from datetime import datetime
from settings import *
from state import state
import account
import notifications
import risk
from logging_setup import get_logger

log = get_logger(__name__)


def send_selection_report(selected, all_candidates=None, skipped_reasons=None):
    msg = ""

    if selected:
        msg += "🏆 ШИНЭ TOP SIGNALS\n━━━━━━━━━━━━━━━━━\n"
        for i, coin in enumerate(selected, 1):
            msg += f"{i}. {coin['symbol']}\n"
            msg += f"Стратеги / Strategy: {coin['strategy']}\n"
            msg += f"Дохио / Signal: {coin['signal']}\n"
            msg += f"Оноо / Score: {coin['score']:.2f}\n"
            msg += f"ADX / RSI: {coin['adx']:.1f} / {coin['rsi']:.1f}\n"
            msg += f"Regime: {coin['regime']} | CHOP: {coin.get('chop', 50):.1f}\n"
            msg += f"MTF: {coin.get('mtf', 'NEUTRAL')} | Funding: {coin.get('funding', 0)*100:.3f}%\n"
            msg += "─────────────────\n"
    else:
        msg += "⚠️ SIGNAL ОЛДСОНГҮЙ\n━━━━━━━━━━━━━━━━━\n"

    if skipped_reasons:
        msg += "\n\n📋 АРИЛЖАА НЭЭГДЭЭГҮЙ ШАЛТГААНУУД / WHY TRADES NOT OPENED:\n"
        msg += "━━━━━━━━━━━━━━━━━\n"
        for reason in skipped_reasons:
            msg += f"• {reason}\n"

    if all_candidates:
        msg += "\n\n📊 БҮХ СТРАТЕГИЙН ШИЛДЭГ ДОХИОНУУД / TOP SIGNALS PER STRATEGY:\n"
        msg += "━━━━━━━━━━━━━━━━━\n"
        for cand in all_candidates[:10]:
            msg += f"• {cand['strategy']:<20} → {cand['symbol']:<8} {cand['signal']:<4} Score={cand['score']:.1f}\n"

    notifications.send_telegram(msg)


def send_performance_report():
    if not STRATEGY_PERFORMANCE_TRACKING:
        return
    total_pnl = 0.0
    sections = []
    for strategy, stats in state.strategy_stats.items():
        if stats["trades"] == 0:
            continue
        win_rate = stats["wins"] / stats["trades"] * 100
        status = "🟢 ACTIVE" if stats["active"] else "🔴 PAUSED"
        pnl = stats["total_pnl"]
        total_pnl += pnl
        sections.append((
            f"🔹 {strategy} — {status}",
            [
                ("Trades", stats["trades"]),
                ("Win / Loss", f"{stats['wins']} / {stats['losses']}"),
                ("Win rate", f"{win_rate:.1f}%"),
                ("PnL", money(pnl)),
                ("Loss streak", stats["consecutive_losses"]),
            ]
        ))
    sections.append(("📊 НИЙТ", [("Total PnL", money(total_pnl))]))
    notifications.send_telegram(format_section("СТРАТЕГИЙН ГҮЙЦЭТГЭЛ", "📊", sections))


def send_cycle_summary():
    current_balance = account.get_usdt_balance()
    balance_change = current_balance - state.last_cycle_balance
    period = f"{datetime.fromtimestamp(state.cycle_start_time).strftime('%H:%M:%S')} → {datetime.now().strftime('%H:%M:%S')}"
    notifications.send_telegram(
        format_block(
            "6 ЦАГИЙН ЦИКЛ",
            "📆",
            [
                ("Хугацаа", period),
                ("Balance change", money(balance_change)),
                ("Balance", f"${current_balance:.2f}"),
                ("Active strategies", len(risk.get_active_strategies())),
            ]
        ),
        pin=True
    )
    state.cycle_start_time = time.time()
    state.last_cycle_balance = current_balance
