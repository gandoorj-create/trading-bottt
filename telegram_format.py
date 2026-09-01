"""
telegram_format.py
Бүх Telegram мессежийг ижил дараалал, ижил хэлбэрээр цэгцлэх helper.

Дараалал (position-той холбоотой мессежүүдэд):
  1. Symbol + Side/Strategy
  2. Price info (Entry/Mark/PnL)
  3. Quantity / Margin
  4. Protection (TP/SL/Trailing)
  5. Analysis (Score/ADX/RSI/Regime) — байвал
  6. Нэмэлт тэмдэглэл
"""


def format_block(title, emoji, rows):
    """
    title: гарчиг (жишээ нь "ШИНЭ ПОЗИЦ НЭЭГДЛЭЭ")
    emoji: гарчгийн урд эмодзи
    rows: [(label, value), ...] — дараалал чинь мессежинд харагдах дараалал шүү

    Row мөр бүрийг ижил өргөнтэй болгож, monospace code block-д
    ороосноор Telegram дээр шулуун шугам шиг эгнэж харагдана.
    """

    header = f"{emoji} *{title}*\n"

    if not rows:
        return header

    label_width = max(len(label) for label, _ in rows)

    lines = []

    for label, value in rows:

        if label == "" and value == "":
            lines.append("")
            continue

        if value is None:
            continue

        padded_label = label.ljust(label_width)

        lines.append(f"{padded_label} : {value}")

    body = "\n".join(lines)

    return f"{header}```\n{body}\n```"


def format_section(title, emoji, sections):
    """
    Олон блоктой мессеж (жишээ нь monitor report — position бүр
    өөрийн блоктой). sections: [(sub_title, [(label, value), ...]), ...]
    Блокуудын хооронд хоосон мөр байна — гол дараалал алдагдахгүй.
    """

    header = f"{emoji} *{title}*\n"

    parts = []

    for sub_title, rows in sections:

        if not rows:
            continue

        label_width = max(len(label) for label, _ in rows)

        lines = [sub_title] if sub_title else []

        for label, value in rows:

            if value is None:
                continue

            lines.append(f"  {label.ljust(label_width)} : {value}")

        parts.append("\n".join(lines))

    body = "\n\n".join(parts)

    return f"{header}```\n{body}\n```"


def money(value, decimals=2):

    sign = "+" if value >= 0 else ""

    return f"{sign}${value:,.{decimals}f}"
