"""
persistence.py
State файлуудыг унших/бичих (стратегийн статистик, сессийн төлөв).
"""
from pathlib import Path
import json
import time
from telegram_format import format_block
from settings import *
from state import state, STRATEGY_NAMES
import notifications
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def check_state_storage():
    """State директор бичигдэх боломжтой эсэхийг эхлэхэд шалгана.

    STATE_DIR тохируулаагүй бол файлууд контейнерийн түр зуурын дискэнд бичигдэж,
    redeploy болгонд алга болно — drawdown-ы оргил утга тэглэгдэж, нээлттэй
    арилжаанууд стратегиэ алдана. Тиймээс намуухан бүтэлгүйтэхийн оронд
    эхлэхдээ тодорхой хэлнэ.
    """
    try:
        Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
        probe = Path(STATE_DIR) / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as e:
        log.error(f"🚨 STATE DIR бичигдэхгүй байна ({STATE_DIR}): {e}")
        notifications.send_telegram(format_block("STATE STORAGE АЛДАА", "🚨", [
            ("Директор", str(STATE_DIR)),
            ("Error", str(e)[:200]),
            ("Үр дагавар", "Drawdown peak болон арилжааны стратеги хадгалагдахгүй"),
        ]))
        return False

    if STATE_DIR_IS_PERSISTENT:
        log.info(f"💾 State хадгалалт: {STATE_DIR} (persistent volume)")
        return True

    log.warning(f"⚠️ State хадгалалт: {STATE_DIR} — түр зуурын диск!")
    log.info("   Railway дээр volume mount хийж STATE_DIR-ийг заана уу (жишээ нь /data).")
    notifications.send_telegram(format_block("STATE ХАДГАЛАЛТ ТҮР ЗУУРЫН", "⚠️", [
        ("Директор", str(STATE_DIR)),
        ("Эрсдэл", "Redeploy хийхэд drawdown peak тэглэгдэж, стратеги алга болно"),
        ("Шийдэл", "Railway volume mount + STATE_DIR тохируулах"),
    ]))
    return True


def load_strategy_state():
    try:
        path = Path(STRATEGY_STATE_FILE)
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        for strategy in STRATEGY_NAMES:
            saved = data.get(strategy)
            if not isinstance(saved, dict):
                continue
            current = state.strategy_stats[strategy]
            for key in ("trades", "wins", "losses", "total_pnl", "consecutive_losses", "active", "paused_cycles"):
                if key in saved:
                    current[key] = saved[key]
    except Exception as e:
        log.warning(f"⚠️ Strategy state load failed: {e}")


def save_strategy_state():
    try:
        path = Path(STRATEGY_STATE_FILE)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state.strategy_stats, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        log.warning(f"⚠️ Strategy state save failed: {e}")


def load_session_state():
    try:
        path = Path(SESSION_STATE_FILE)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.warning(f"⚠️ Session state load failed: {e}")
        return None


def save_session_state():
    try:
        path = Path(SESSION_STATE_FILE)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "session_start_balance": state.session_start_balance,
            "session_peak_balance": state.session_peak_balance,
            "session_realized_pnl": state.session_realized_pnl,
            # Нээлттэй арилжааны symbol → стратеги холбоос. Үүнийг хадгалснаар
            # restart-ын дараа позицуудыг "RECOVERED" биш, жинхэнэ стратегиэрээ
            # таньж, статистикт нь зөв тооцох боломжтой болно.
            "active_trades": state.active_trade_info,
            "saved_at": int(time.time()),
        }, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        log.warning(f"⚠️ Session state save failed: {e}")


def load_saved_trades(max_age_sec=86400):
    """Өмнөх ажиллагаанаас үлдсэн нээлттэй арилжааны бүртгэлийг уншина.

    Хэт хуучирсан snapshot-д итгэхгүй — тэр хооронд позиц хаагдаад гараар
    шинээр нээгдсэн байж болзошгүй тул стратегийг буруу оноох эрсдэлтэй.
    """
    saved = load_session_state()
    if not saved:
        return {}
    if (time.time() - utils.safe_float(saved.get("saved_at"), 0)) > max_age_sec:
        return {}
    trades = saved.get("active_trades")
    return trades if isinstance(trades, dict) else {}
