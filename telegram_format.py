"""
telegram_format.py
Telegram мэдэгдлүүдийг цэвэр, албан ёсны хэлбэрт оруулах.
Монгол + Англи хэлний тайлбартай, backtick ашиглахгүй.
"""

from datetime import datetime


def money(value, decimals=2):
    """Мөнгөний дүнг форматлах (жишээ: +$123.45)"""
    sign = "+" if value >= 0 else ""
    return f"{sign}${value:,.{decimals}f}"


def fmt_price(value, decimals=4):
    return f"${value:,.{decimals}f}"


def fmt_qty(value):
    if value >= 1:
        return f"{value:,.2f}"
    else:
        return f"{value:,.6f}"


def _build_block(title, emoji, rows):
    """
    rows: (label_mon, label_en, value) хэлбэрийн жагсаалт
    """
    line = "━" * 30
    header = f"{emoji} *{title}*\n{line}"

    if not rows:
        return header

    # Label-ийн хамгийн уртыг тооцоолох (монгол + англи нийлсэн)
    max_len = max(len(f"{mon} / {eng}") for mon, eng, val in rows if val is not None)
    lines = []
    for mon, eng, val in rows:
        if val is None:
            continue
        label = f"{mon} / {eng}"
        padded = label.ljust(max_len)
        lines.append(f"  {padded} : {val}")

    body = "\n".join(lines)
    return f"{header}\n{body}"


# ---------- Гол функцууд ----------

def format_start_message(balance, target, leverage, allocation):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        ("Цаг", "Time", now),
        ("Дансны үлдэгдэл", "Balance", money(balance)),
        ("Зорилт", "Target", money(target)),
        ("Хөшүүрэг", "Leverage", f"{leverage}x"),
        ("Хуваарилалт", "Allocation", f"{allocation * 100:.0f}%"),
    ]
    return _build_block("БОТ АСЛАА / BOT STARTED", "🚀", rows)


def format_recovered_positions(positions):
    """ХУУЧИН ПОЗИЦ ОЛДЛОО (Restart)"""
    if not positions:
        return None

    parts = []
    for pos in positions:
        symbol = pos.get("symbol")
        side = pos.get("side", "BUY")
        strategy = pos.get("strategy", "RECOVERED")
        entry = pos.get("entry_price", 0)
        mark = pos.get("mark_price", 0)
        pnl = pos.get("pnl", 0)
        qty = pos.get("quantity", 0)

        rows = [
            ("Стратеги", "Strategy", strategy),
            ("Чиглэл", "Side", side),
            ("Нээсэн үнэ", "Entry", fmt_price(entry)),
            ("Одоогийн үнэ", "Mark", fmt_price(mark)),
            ("Ашиг/Алдагдал", "PnL", money(pnl)),
            ("Хэмжээ", "Qty", fmt_qty(qty)),
        ]
        parts.append(_build_block(symbol, "📌", rows))

    header = "🔄 *ХУУЧИН ПОЗИЦ ОЛДЛОО / RECOVERED POSITIONS*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    return header + "\n\n".join(parts)


def format_monitor_report(positions, total_unrealized, balance, session_pnl, target):
    if not positions:
        return "📊 *ПОЗИЦ БАЙХГҮЙ / NO POSITIONS*"

    parts = []
    for pos in positions:
        symbol = pos.get("symbol")
        side = pos.get("side", "UNKNOWN")
        strategy = pos.get("strategy", "UNKNOWN")
        entry = pos.get("entry_price", 0)
        mark = pos.get("mark_price", 0)
        pnl = pos.get("pnl", 0)
        qty = pos.get("quantity", 0)

        rows = [
            ("Стратеги", "Strategy", strategy),
            ("Чиглэл", "Side", side),
            ("Нээсэн үнэ", "Entry", fmt_price(entry)),
            ("Одоогийн үнэ", "Mark", fmt_price(mark)),
            ("Ашиг/Алдагдал", "PnL", money(pnl)),
            ("Хэмжээ", "Qty", fmt_qty(qty)),
        ]
        parts.append(_build_block(symbol, "🔹", rows))

    header = "📊 *ПОЗИЦЫН ТАЙЛАН / POSITION REPORT*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    body = "\n\n".join(parts)

    remaining = target - session_pnl
    summary = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Нийт реализаагүй / Unrealized* : {money(total_unrealized)}\n"
        f"💵 *Сессийн ашиг / Session PnL*     : {money(session_pnl)}\n"
        f"🎯 *Үлдсэн зорилт / Remaining*      : {money(remaining)}"
    )

    return header + "\n\n".join(parts) + summary


def format_new_trade(symbol, strategy, side, entry, sl, tp, margin, leverage, score, adx, rsi, regime):
    rows = [
        ("Стратеги", "Strategy", strategy),
        ("Чиглэл", "Side", side),
        ("Нээсэн үнэ", "Entry", fmt_price(entry)),
        ("Stop Loss", "Stop Loss", fmt_price(sl)),
        ("Take Profit", "Take Profit", fmt_price(tp)),
        ("Дансны хувь", "Margin", f"{margin:.2f} USDT"),
        ("Хөшүүрэг", "Leverage", f"{leverage}x"),
        ("Оноо", "Score", f"{score:.2f}"),
        ("ADX", "ADX", f"{adx:.1f}"),
        ("RSI", "RSI", f"{rsi:.1f}"),
        ("Зах зээлийн төлөв", "Regime", regime),
    ]
    return _build_block(f"{symbol} - ШИНЭ ПОЗИЦ / NEW POSITION", "🚀", rows)


def format_position_closed(symbol, strategy, pnl):
    emoji = "🟢" if pnl >= 0 else "🔴"
    rows = [
        ("Стратеги", "Strategy", strategy),
        ("Ашиг/Алдагдал", "PnL", money(pnl)),
    ]
    return _build_block(f"{symbol} - ПОЗИЦ ХААГДЛАА / POSITION CLOSED", emoji, rows)


def format_selection_report(selected):
    if not selected:
        return "⚠️ *SIGNAL ОЛДСОНГҮЙ / NO SIGNAL*"

    parts = []
    for i, coin in enumerate(selected, 1):
        rows = [
            ("Стратеги", "Strategy", coin.get("strategy")),
            ("Дохио", "Signal", coin.get("signal")),
            ("Оноо", "Score", f"{coin.get('score', 0):.2f}"),
            ("ADX", "ADX", f"{coin.get('adx', 0):.1f}"),
            ("RSI", "RSI", f"{coin.get('rsi', 0):.1f}"),
            ("Төлөв", "Regime", coin.get("regime")),
        ]
        parts.append(_build_block(f"{i}. {coin.get('symbol')}", "🏆", rows))

    header = "🏆 *ШИНЭ TOP SIGNALS*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    return header + "\n\n".join(parts)
