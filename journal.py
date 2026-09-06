"""
journal.py
Хаагдсан арилжаа бүрийг орох үеийн нөхцөлтэй нь CSV-д бүртгэнэ.

Яагаад хэрэгтэй вэ: тохиргоог (min_signal_score, TP/SL, ATR үржүүлэгч) одоо
таамгаар л тааруулж байна. 30-50 арилжааны дараа "аль score-оос дээш нь
үнэхээр ашигтай вэ", "аль regime-д алдаж байна вэ", "аль стратеги ажиллаж
байна вэ" гэдгийг энэ файлаас тоогоор хардаг болно.

Бүртгэл нь арилжааны гогцоог хэзээ ч зогсоох ёсгүй — бүх алдааг залгина.
"""
import csv
import os
import time
from settings import *
import utils
from logging_setup import get_logger

log = get_logger(__name__)

# Орох үед хадгалагдаж, хаагдахад бичигдэх нөхцөлүүд. Дараалал нь CSV баганы
# дараалал — шинэ багана нэмэхдээ ТӨГСГӨЛД нь нэмнэ, эс тэгвээс хуучин мөрүүд
# буруу баганад унана.
ENTRY_FIELDS = (
    "score", "adx", "rsi", "atr_pct", "volume_ratio", "ema_slope",
    "regime", "chop", "sentiment", "funding", "mtf",
)

FIELDNAMES = (
    "closed_at", "symbol", "strategy", "side", "pnl", "hold_hours",
    "entry_price", "quantity", "exit_reason", "partial_tp_hit",
    "sl_pct", "tp_pct",
) + ENTRY_FIELDS


def entry_context(coin):
    """Screening-ийн үр дүнгээс бүртгэх талбаруудыг ялгаж авна."""
    return {field: coin.get(field) for field in ENTRY_FIELDS}


def _row(symbol, trade, pnl):
    context = trade.get("entry_context") or {}
    opened_at = utils.safe_float(trade.get("opened_at"), 0.0)
    hold_hours = (time.time() - opened_at) / 3600 if opened_at > 0 else None
    levels = trade.get("levels") or {}

    row = {
        "closed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "symbol": symbol,
        "strategy": trade.get("strategy", "UNKNOWN"),
        "side": trade.get("side", ""),
        "pnl": round(pnl, 4),
        "hold_hours": round(hold_hours, 2) if hold_hours is not None else "",
        "entry_price": trade.get("entry_price", ""),
        "quantity": trade.get("quantity", ""),
        # Захиалга дуусгасан шалтгаан. Биржийн SL/TP/trailing аль нь цохисныг
        # ялгах боломжгүй (algo захиалгын түүх уншигдахгүй) тул нэг ангилалд.
        "exit_reason": trade.get("exit_reason", "SL_TP_TRAILING"),
        "partial_tp_hit": bool(trade.get("breakeven_done")),
        "sl_pct": round(utils.safe_float(levels.get("sl"), EMERGENCY_SL_PCT), 3),
        "tp_pct": round(utils.safe_float(levels.get("tp"), TAKE_PROFIT_PCT), 3),
    }
    for field in ENTRY_FIELDS:
        value = context.get(field)
        row[field] = round(value, 4) if isinstance(value, float) else (value if value is not None else "")
    return row


def record_close(symbol, trade, pnl):
    """Хаагдсан арилжааг нэг мөр болгон нэмнэ (толгойг нэг удаа бичнэ)."""
    if not JOURNAL_ENABLED:
        return
    try:
        exists = os.path.exists(JOURNAL_FILE) and os.path.getsize(JOURNAL_FILE) > 0
        with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(_row(symbol, trade, pnl))
    except Exception as e:
        log.warning(f"⚠️ Journal бичигдсэнгүй ({symbol}): {e}")
