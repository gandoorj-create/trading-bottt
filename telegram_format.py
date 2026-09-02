"""
telegram_format.py
Telegram мэдэгдлүүдийг ЦЭВЭР ЭНГИН ТЕКСТ хэлбэрт оруулах.
Ямар ч тодруулгагүй (bold, italic, underline, HTML байхгүй).
Зөвхөн тэгшлэсэн текст + хоёр цэг.
Бүх label-ууд Англи / Монгол 2 хэлээр харагдана.
"""

import re


def money(value, decimals=2):
    """Мөнгөний дүнг форматлах"""
    sign = "+" if value >= 0 else ""
    return f"{sign}${value:,.{decimals}f}"


def _clean_text(text):
    """Текстээс emoji-г бүрэн арилгах"""
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
    """Label-ийн Монгол / Англи орчуулга (2 хэл)"""
    translations = {
        # Үндсэн арилжааны мэдээлэл
        "Symbol": "Бэлгэ тэмдэг / Symbol",
        "Strategy": "Стратеги / Strategy",
        "Signal": "Дохио / Signal",
        "Side": "Чиглэл / Side",
        "Entry": "Нээсэн үнэ / Entry",
        "Mark": "Одоогийн үнэ / Mark",
        "PnL": "Ашиг/Алдагдал / PnL",
        "Qty": "Хэмжээ / Qty",
        "Price": "Үнэ / Price",
        "Margin": "Дансны хувь / Margin",
        "Leverage": "Хөшүүрэг / Leverage",
        "Allocation": "Хуваарилалт / Allocation",
        "Balance": "Үлдэгдэл / Balance",
        "New Balance": "Шинэ үлдэгдэл / New Balance",
        "Balance change": "Үлдэгдлийн өөрчлөлт / Balance change",
        "Open Positions": "Нээлттэй позиц / Open Positions",
        
        # Дохионы мэдээлэл
        "Score": "Оноо / Score",
        "ADX": "ADX / ADX",
        "RSI": "RSI / RSI",
        "ADX / RSI": "ADX / RSI",
        "Regime": "Зах зээлийн төлөв / Regime",
        "Active strategies": "Идэвхтэй стратеги / Active strategies",
        
        # Эрсдэлийн мэдээлэл
        "Take Profit": "Ашиг түгжих / Take Profit",
        "Stop Loss": "Алдагдал хязгаар / Stop Loss",
        "Trailing": "Аялгын зогсоолт / Trailing",
        "Activation": "Идэвхжүүлэх үнэ / Activation",
        "Callback": "Буцах хувь / Callback",
        "Emergency SL": "Яаралтай хязгаар / Emergency SL",
        
        # Статистик
        "Trades": "Арилжааны тоо / Trades",
        "Win / Loss": "Хожсон / Алдсан",
        "Win rate": "Хожлын хувь / Win rate",
        "Loss streak": "Дараалсан алдагдал / Loss streak",
        "Total PnL": "Нийт ашиг/алдагдал / Total PnL",
        "Net PnL": "Цэвэр ашиг/алдагдал / Net PnL",
        "Avg Win": "Дундаж хожлын хэмжээ / Avg Win",
        "Avg Loss": "Дундаж алдагдлын хэмжээ / Avg Loss",
        "Max Profit": "Хамгийн их ашиг / Max Profit",
        "Max Drawdown": "Хамгийн их уналт / Max Drawdown",
        "Profit Factor": "Ашгийн хүчин зүйл / Profit Factor",
        "Expectancy/Trade": "Хүлээгдэж буй ашиг/арилжаа / Expectancy/Trade",
        "Fee model": "Хураамжийн загвар / Fee model",
        "Slippage model": "Гулсалтын загвар / Slippage model",
        "Note": "Анхаар / Note",
        
        # Нийт үзүүлэлт
        "Unrealized": "Нийт реализаагүй / Unrealized",
        "Session Realized": "Сессийн ашиг / Session Realized",
        "Realized PnL": "Бодит ашиг / Realized PnL",
        "Target": "Зорилт / Target",
        "Period": "Хугацаа / Period",
        "Time": "Цаг / Time",
        "Status": "Төлөв / Status",
        "Details": "Дэлгэрэнгүй / Details",
        "Error": "Алдаа / Error",
        "Action": "Үйлдэл / Action",
        
        # DCA болон бусад
        "Drawdown": "Уналт / Drawdown",
        "Peak Balance": "Хамгийн өндөр үлдэгдэл / Peak Balance",
        "Current Balance": "Одоогийн үлдэгдэл / Current Balance",
        "Limit": "Хязгаар / Limit",
        "Level": "Түвшин / Level",
        "Added Qty": "Нэмсэн хэмжээ / Added Qty",
        "New Avg Price": "Шинэ дундаж үнэ / New Avg Price",
        "Total Qty": "Нийт хэмжээ / Total Qty",
        "Pause": "Түр зогсолт / Pause",
        
        # Backtest
        "Period": "Хугацаа / Period",
        "Note": "Анхаар / Note",
    }
    
    # Хэрэв label нь "/" тэмдэгт агуулж байвал (жишээ нь "ADX / RSI") 
    # түүнийг бүхэлд нь орчуулах гэж оролдох, эсвэл хэвээр үлдээх
    if "/" in label and label not in translations:
        return label
    
    # Хэрэв key байхгүй бол анхны label-ыг буцаах
    return translations.get(label, label)


def _format_rows(rows, indent=0):
    """
    rows: (label, value) хосын жагсаалт.
    Бүх label-ийг тэгшлээд ' : ' таслалаар холбоно.
    """
    if not rows:
        return ""

    # Label-уудыг Монгол / Англи хэлээр орчуулах
    translated_rows = [(_translate_label(label), value) for label, value in rows if value is not None]
    if not translated_rows:
        return ""

    max_label_len = max(len(label) for label, _ in translated_rows)
    lines = []
    for label, value in translated_rows:
        lines.append(f"{' ' * indent}{label.ljust(max_label_len)} : {value}")
    return "\n".join(lines)


def format_block(title, emoji, rows):
    """
    Нэг блок форматлах (зураас, тодруулгагүй)
    """
    clean_title = _clean_text(title)
    body = _format_rows(rows, indent=0)
    if body:
        return f"{clean_title}\n{body}"
    return clean_title


def format_section(title, emoji, sections):
    """
    Олон блоктой мессеж (зураас, тодруулгагүй)
    """
    clean_title = _clean_text(title)
    parts = []
    for sub_title, rows in sections:
        if not rows:
            continue
        clean_sub = _clean_text(sub_title) if sub_title else ""
        body = _format_rows(rows, indent=0)
        if body:
            if clean_sub:
                parts.append(f"{clean_sub}\n{body}")
            else:
                parts.append(body)
    body = "\n\n".join(parts)
    return f"{clean_title}\n{body}"
