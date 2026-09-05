"""
backtest.py
Түүхэн өгөгдөл дээр стратеги турших.
"""
import numpy as np
from telegram_format import format_block
from settings import *
import indicators
import market_data
import strategies
from logging_setup import get_logger

log = get_logger(__name__)


# STRATEGY_STATE_FILE / SESSION_STATE_FILE нь settings.py-д STATE_DIR дээр
# тулгуурлаж тодорхойлогддог. Өмнө нь энд дахин тодорхойлдог байсан тул
# config.json дахь тохиргоо болон STATE_DIR хоёулаа үл хэрэгсэгддэг байв.
BACKTEST_FEE_RATE = 0.0004


BACKTEST_SLIPPAGE_RATE = 0.0005


def run_backtest(symbol, strategy, days=30, interval="1h"):
    log.info(f"\n🧪 Backtesting {strategy} on {symbol} for {days} days ({interval})")
    try:
        limit = days * 24 if interval == "1h" else days * 24 * 4 if interval == "15m" else days * 6
        df = market_data.get_klines(symbol, interval=interval, limit=min(limit, 1500))
        if len(df) < 220:
            return f"⚠️ {symbol}: Хангалттай өгөгдөл байхгүй ({len(df)} лаа)"

        trades = []
        position = None
        equity = 0.0
        peak_equity = 0.0
        max_dd = 0.0

        for i in range(200, len(df) - 1):
            window = df.iloc[:i + 1]
            close = float(window["close"].iloc[-1])
            adx = float(indicators.calculate_adx(window).iloc[-1])
            rsi = float(indicators.calculate_rsi(window).iloc[-1])
            atr = float(indicators.calculate_atr(window).iloc[-1])
            atr_pct = atr / close * 100 if close else 0.0
            ema50 = indicators.calculate_ema(window, 50)
            ema_slope = ((ema50.iloc[-1] - ema50.iloc[-5]) / ema50.iloc[-5] * 100) if ema50.iloc[-5] else 0.0
            volume_ratio = indicators.calculate_volume_ratio(window)
            sentiment = 0.0
            chop = indicators.calculate_chop(window, CHOP_PERIOD).iloc[-1]
            regime = strategies.determine_regime(chop, adx, ema_slope, atr_pct)
            signal = strategies.generate_strategy_signal(strategy, window, sentiment, regime, chop)

            next_open = float(df["open"].iloc[i + 1])
            fee = BACKTEST_FEE_RATE
            slip = BACKTEST_SLIPPAGE_RATE

            if position is None and signal in ("BUY", "SELL"):
                entry = next_open * (1 + slip if signal == "BUY" else 1 - slip)
                position = {"side": "LONG" if signal == "BUY" else "SHORT", "entry": entry, "entry_i": i + 1}
                continue

            if position is not None:
                reverse_signal = (position["side"] == "LONG" and signal == "SELL") or (position["side"] == "SHORT" and signal == "BUY")
                if reverse_signal:
                    exit_price = next_open * (1 - slip if position["side"] == "LONG" else 1 + slip)
                    if position["side"] == "LONG":
                        gross_pct = (exit_price - position["entry"]) / position["entry"] * 100
                    else:
                        gross_pct = (position["entry"] - exit_price) / position["entry"] * 100
                    net_pct = gross_pct - fee * 2 * 100
                    trades.append({
                        "entry_time": int(df.iloc[position["entry_i"]]["time"]),
                        "exit_time": int(df.iloc[i + 1]["time"]),
                        "side": position["side"],
                        "entry_price": position["entry"],
                        "exit_price": exit_price,
                        "gross_pct": gross_pct,
                        "net_pct": net_pct,
                    })
                    equity += net_pct
                    peak_equity = max(peak_equity, equity)
                    max_dd = max(max_dd, peak_equity - equity)
                    position = None

        if position is not None:
            exit_price = float(df["close"].iloc[-1]) * (1 - BACKTEST_SLIPPAGE_RATE if position["side"] == "LONG" else 1 + BACKTEST_SLIPPAGE_RATE)
            if position["side"] == "LONG":
                gross_pct = (exit_price - position["entry"]) / position["entry"] * 100
            else:
                gross_pct = (position["entry"] - exit_price) / position["entry"] * 100
            net_pct = gross_pct - BACKTEST_FEE_RATE * 2 * 100
            trades.append({
                "entry_time": int(df.iloc[position["entry_i"]]["time"]),
                "exit_time": int(df.iloc[-1]["time"]),
                "side": position["side"],
                "entry_price": position["entry"],
                "exit_price": exit_price,
                "gross_pct": gross_pct,
                "net_pct": net_pct,
            })
            equity += net_pct
            peak_equity = max(peak_equity, equity)
            max_dd = max(max_dd, peak_equity - equity)

        if not trades:
            return f"⚠️ {symbol} ({strategy}): Арилжаа гүйцэтгэгдээгүй"

        net = np.array([t["net_pct"] for t in trades], dtype=float)
        wins = net[net > 0]
        losses = net[net < 0]
        win_rate = float((net > 0).mean() * 100)
        profit_factor = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
        expectancy = float(net.mean())
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0

        rows = [
            ("Symbol", symbol),
            ("Strategy", strategy),
            ("Period", f"сүүлийн {days} өдөр ({interval})"),
            ("Trades", str(len(trades))),
            ("Win Rate", f"{win_rate:.1f}%"),
            ("Net PnL", f"{net.sum():+.2f}%"),
            ("Avg Win", f"{avg_win:+.2f}%"),
            ("Avg Loss", f"{avg_loss:+.2f}%"),
            ("Profit Factor", f"{profit_factor:.2f}"),
            ("Expectancy/Trade", f"{expectancy:+.3f}%"),
            ("Max Drawdown", f"{max_dd:.2f}%"),
            ("Fee model", f"{BACKTEST_FEE_RATE * 100:.03f}%/side"),
            ("Slippage model", f"{BACKTEST_SLIPPAGE_RATE * 100:.03f}%/side"),
            ("Note", "энэ нь simulation; funding, liquidation болон exact exchange fills бүрэн моделдоогүй."),
        ]
        return format_block("🧪 BACKTEST REPORT", "🧪", rows)

    except Exception as e:
        return f"❌ Backtest error ({symbol}, {strategy}): {e}"
