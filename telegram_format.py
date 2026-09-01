"""
telegram_format.py
Telegram мэдэгдлүүдийг цэвэр, албан ёсны хэлбэрт оруулах.
HTML хэлбэржүүлэлт ашигласан.
Label-ууд: Доогуур зураас + Тод (Underline + Bold) -> дулаахан, тод харагдац.
Утгууд: Энгийн текст (хар / theme-ийн өнгө).
"""

import re


# ---------- Орчуулгын толь ----------
_TRANSLATIONS = {
    "Symbol": "Бэлгэ тэмдэг / Symbol",
    "Strategy": "Стратеги / Strategy",
    "Signal": "Дохио / Signal",
    "Side": "Чиглэл / Side",
    "Entry": "Нээсэн үнэ / Entry",
    "Mark": "Одоогийн үнэ / Mark",
    "PnL": "Ашиг/Алдагдал / PnL",
    "Qty": "Хэмжээ / Qty",
    "Price": "Үнэ / Price",
    "Error": "Алдаа / Error",
    "Status": "Төлөв / Status",
    "Details": "Дэлгэрэнгүй / Details",
    "Note": "Анхаар / Note",
    "Target": "Зорилт / Target",
    "Balance": "Үлдэгдэл / Balance",
    "New Balance": "Шинэ үлдэгдэл / New Balance",
    "Margin": "Дансны хувь / Margin",
    "Leverage": "Хөшүүрэг / Leverage",
    "Allocation": "Хуваарилалт / Allocation",
    "Time": "Цаг / Time",
    "Period": "Хугацаа / Period",
    "Open Positions": "Нээлттэй позиц / Open Positions",
    "Active strategies": "Идэвхтэй стратеги / Active strategies",
    "Score": "Оноо / Score",
    "ADX": "ADX",
    "RSI": "RSI",
    "Regime": "Зах зээлийн төлөв / Regime",
    "ADX / RSI": "ADX / RSI",
    "Take Profit": "Ашиг түгжих / Take Profit",
    "Stop Loss": "Алдагдал хязгаар / Stop Loss",
    "Trailing": "Аялгын зогсоолт / Trailing",
    "Activation": "Идэвхжүүлэх үнэ / Activation",
    "Callback": "Буцах хувь / Callback",
    "Trades": "Арилжааны тоо / Trades",
    "Win / Loss": "Хожсон / Алдсан",
    "Win rate": "Хожлын хувь / Win rate",
    "Loss streak": "Дараалсан алдагдал / Loss streak",
    "Unrealized": "Нийт реализаагүй / Unrealized",
    "Session Realized": "Сессийн ашиг / Session PnL",
    "Realized PnL": "Бодит ашиг / Realized PnL",
    "Balance change": "Үлдэгдлийн өөрчлөлт / Balance change",
}


def money(value, decimals=2):
    """Мөнгөний дүнг форматлах"""
    sign = "+" if value >= 0 else ""
    return f"{sign}${value:,.{decimals}f}"


def _clean_text(text):
    """Текстээс emoji-г арилгах"""
    emoji_pattern = re.compile("["
        u"\U0001F600-\U0001F64F"
        u"\U0001F300-\U0001F5FF"
        u"\U0001F680-\U0001F6FF"
        u"\U0001F1E0-\U0001F1FF"
        u"\U00002702-\U000027B0"
        u"\U000024C2-\U0001F251"
        u"\U0001F900-\U0001F9FF"
        u"\U0001FA70-\U0001FAFF"
        u"\U00002600-\U000026FF"
        u"\U00002B50-\U00002B55"
        u"\U000025AA-\U000025FE"
        u"\U00002020-\U000020BF"
        u"\U000023E9-\U000023FA"
        u"\U000025B6-\U000025C0"
        u"\U00002192-\U00002199"
        "]+", flags=re.UNICODE)
    return emoji_pattern.sub(r'', text).strip()


def _translate_label(label):
    """Label-г орчуулах"""
    if "/" in label:
        return label
    return _TRANSLATIONS.get(label, label)


def _format_rows(rows, indent=2):
    """
    rows: (label, value) хосын жагсаалт.
    Label-уудыг HTML-ээр <u><b>...</b></u> (доогуур зураас + тод) болгоно.
    Утгуудыг энгийн текстээр үлдээнэ.
    """
    if not rows:
        return ""

    valid_rows = []
    for label, value in rows:
        if value is None:
            continue
        translated_label = _translate_label(label)
        valid_rows.append((translated_label, value))

    if not valid_rows:
        return ""

    max_label_len = max(len(label) for label, _ in valid_rows)
    lines = []
    for label, value in valid_rows:
        # Label-ыг доогуур зураас + тод болгох (дулаахан, тод харагдац)
        label_html = f"<u><b>{label.ljust(max_label_len)}</b></u>"
        lines.append(f"{' ' * indent}{label_html} : {value}")
    return "\n".join(lines)


def format_block(title, emoji, rows):
    """
    Нэг блок форматлах (зураасгүй)
    """
    clean_title = _clean_text(title)
    header = f"<b>{clean_title}</b>"
    body = _format_rows(rows)
    if body:
        return f"{header}\n{body}"
    else:
        return header


def format_section(title, emoji, sections):
    """
    Олон блоктой мессеж (зураасгүй)
    """
    clean_title = _clean_text(title)
    header = f"<b>{clean_title}</b>"

    parts = []
    for sub_title, rows in sections:
        if not rows:
            continue
        clean_sub = _clean_text(sub_title) if sub_title else ""
        block = ""
        if clean_sub:
            block = f"<b>{clean_sub}</b>"
        body = _format_rows(rows)
        if body:
            if block:
                parts.append(f"{block}\n{body}")
            else:
                parts.append(body)

    body = "\n\n".join(parts)
    return f"{header}\n{body}"
