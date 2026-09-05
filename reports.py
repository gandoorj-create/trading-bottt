"""
reports.py
Telegram тайлангууд ба график.
"""
import io
import matplotlib.pyplot as plt
import time
from telegram_format import format_block, format_section, money
from datetime import datetime
from settings import *
from state import state
import account
import indicators
import notifications
import risk
from logging_setup import get_logger

log = get_logger(__name__)


def send_chart(symbol, df, signal=None, score=None):
    if not CHART_ENABLED:
        return False
    try:
        df_plot = df.tail(100).copy()
        if len(df_plot) < 20:
            return False
        
        fig, ax = plt.subplots(figsize=(10, 6))
        
        ax.plot(df_plot.index, df_plot["close"], color='blue', linewidth=1.5, label='Close')
        
        ema20 = indicators.calculate_ema(df_plot, 20)
        ema50 = indicators.calculate_ema(df_plot, 50)
        ax.plot(df_plot.index, ema20, color='orange', linestyle='--', linewidth=1, label='EMA 20')
        ax.plot(df_plot.index, ema50, color='red', linestyle='--', linewidth=1, label='EMA 50')
        
        upper, middle, lower = indicators.calculate_bollinger(df_plot)
        ax.fill_between(df_plot.index, upper, lower, alpha=0.1, color='gray')
        ax.plot(df_plot.index, upper, color='gray', linestyle=':', linewidth=0.8)
        ax.plot(df_plot.index, lower, color='gray', linestyle=':', linewidth=0.8)
        
        if signal:
            last_price = df_plot["close"].iloc[-1]
            if signal == "BUY":
                ax.scatter(df_plot.index[-1], last_price, color='green', s=100, marker='^', label='BUY')
            elif signal == "SELL":
                ax.scatter(df_plot.index[-1], last_price, color='red', s=100, marker='v', label='SELL')
        
        title = f"{symbol} | {signal if signal else 'No Signal'}"
        if score:
            title += f" | Score: {score:.2f}"
        ax.set_title(title, fontsize=12)
        ax.legend(loc='upper left')
        ax.grid(True, alpha=0.3)
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)
        
        caption = f"📊 {symbol} | Signal: {signal}" if signal else f"📊 {symbol}"
        return notifications.send_telegram_photo(buf.getvalue(), caption)
    except Exception as e:
        log.error(f"❌ Chart generation error: {e}")
        return False


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
