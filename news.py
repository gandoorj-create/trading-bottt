"""
news.py
Мэдээний цагийн хуваарь ба мэдээний дараах арилжаа.
"""
from datetime import datetime
import pytz
import requests
from telegram_format import format_block
from settings import *
from state import state
import account
import market_data
import notifications
import order_api
import utils
from logging_setup import get_logger

log = get_logger(__name__)

# Календарийг хэр олон удаа шалгах вэ. Эвент нь долоо хоногийн хуваарь тул
# цагт нэг л хангалттай. Алдаа гарвал улам сийрэгжинэ — эс тэгвээс мөчлөг
# тутамд (30 сек) сүлжээ цохиж, log-ыг warning-оор дүүргэнэ.
NEWS_LOOKUP_INTERVAL_SEC = 3600
NEWS_RETRY_INTERVAL_SEC = 900
# Хэдэн удаа дараалан амжилтгүй болоход "зогсоолт ажиллахгүй байна" гэж
# мэдэгдэх вэ. Чимээгүй унтарсан хамгаалалт бол хамгийн аюултай төрөл.
NEWS_FAILURE_ALERT_AT = 3


class NewsCalendarError(RuntimeError):
    """Календарийг уншиж чадсангүй. "Эвент байхгүй"-гээс ялгаатай."""


def get_next_news_event():
    """Хамгийн ойрын ирээдүйн өндөр нөлөөтэй USD эвентийн UTC цаг.

    Календарийн JSON нь цагийн дарааллаар эрэмбэлэгдсэн гэсэн баталгаа байхгүй
    тул анхны таарсныг биш, ИРЭЭДҮЙН таарсан бүхнээс хамгийн эртнийг сонгоно —
    эс тэгвээс 2 хоногийн дараах эвент рүү тэмүүлж, маргаашийнхыг өнгөрөөнө.
    """
    if not NEWS_CALENDAR_URL:
        raise NewsCalendarError("calendar_url тохируулаагүй")

    try:
        # Анхдагч python-requests User-Agent-ыг олон календарийн үйлчилгээ
        # блоклож, JSON-ы оронд HTML сорилтын хуудас буцаадаг — тэр үед
        # .json() нь "Expecting value: line 1 column 1" гэж унана.
        resp = requests.get(
            NEWS_CALENDAR_URL, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)",
                     "Accept": "application/json"},
        )
    except Exception as e:
        raise NewsCalendarError(f"хүсэлт бүтсэнгүй: {e}") from e

    if resp.status_code != 200:
        raise NewsCalendarError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        # Юу ирснийг харуулна — "Expecting value" ганцаараа юу ч хэлдэггүй.
        raise NewsCalendarError(
            f"JSON биш хариу ({resp.headers.get('Content-Type', '?')}): {resp.text[:120]!r}"
        )

    if not isinstance(data, list):
        raise NewsCalendarError(f"жагсаалт хүлээж байсан, {type(data).__name__} ирлээ")

    now = datetime.now(pytz.UTC)
    ny_tz = pytz.timezone('America/New_York')
    upcoming = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        if "USD" not in item.get("country", ""):
            continue
        if not any(keyword.upper() in title.upper() for keyword in NEWS_EVENT_KEYWORDS):
            continue
        try:
            raw_dt = datetime.fromisoformat(item["date"])
        except (KeyError, TypeError, ValueError):
            continue
        # pytz needs localize(); .replace(tzinfo=) yields a wrong LMT offset.
        if raw_dt.tzinfo is None:
            raw_dt = ny_tz.localize(raw_dt)
        event_time_utc = raw_dt.astimezone(pytz.UTC)
        if event_time_utc > now:
            upcoming.append(event_time_utc)

    if not upcoming:
        return None
    return min(upcoming)


def check_news_status():
    # News mode ONLY toggles news_mode_active. The main loop treats that as
    # "monitor open positions, don't open new technical trades" — it must NOT
    # touch safety_lock, because the main loop's safety_lock branch runs
    # safety_recovery() which force-closes every open position.
    if not NEWS_ENABLED:
        return

    now = datetime.now(pytz.UTC)
    refresh_news_schedule(now)

    if not state.next_news_time:
        return

    diff = (state.next_news_time - now).total_seconds() / 60

    if 0 < diff < NEWS_PAUSE_BEFORE:
        state.news_mode_active = True
        state.news_trade_done = False
        log.info(f"📰 News approaching in {diff:.0f} min. Pausing new technical trades.")
        return

    if -NEWS_WAIT_AFTER < diff < 0:
        state.news_mode_active = True
        log.info(f"📰 News just released. Waiting {NEWS_WAIT_AFTER} min for stability...")
        return

    if diff <= -NEWS_WAIT_AFTER and state.news_mode_active and not state.news_trade_done:
        # Зогсоолт ба дараах арилжаа тусдаа флагтай: зогсоолт нь эвентийн spike
        # дээр stop цохиулахаас хамгаалдаг тул дангаараа утгатай, харин
        # хөдөлгөөний араас үсрэх нь батлагдаагүй тул сонголт хэвээр.
        if NEWS_POST_TRADE_ENABLED:
            log.info("📰 News cooldown finished. Executing post-news trade...")
            execute_post_news_trade()
        else:
            log.info("📰 News cooldown finished — post-news арилжаа унтраалттай, техник арилжаа үргэлжилнэ.")
        state.news_trade_done = True
        state.news_mode_active = False
        return

    if diff <= - (NEWS_WAIT_AFTER + 30) and state.news_mode_active:
        state.news_mode_active = False
        log.info("✅ News window closed. Resuming normal trading.")


def refresh_news_schedule(now):
    """Дараагийн эвентийн цагийг хэрэгтэй үед нь шинэчилнэ.

    Өмнө нь нөхцөл нь `not state.next_news_time or stale` байсан тул хайлт
    бүтэлгүйтэхэд next_news_time нь None хэвээр үлдэж, мөчлөг тутамд (30 сек)
    дахин оролддог байв. Одоо оролдлогын хугацаагаар л шийднэ.
    """
    last = state.last_news_check if isinstance(state.last_news_check, datetime) else None
    if last is not None:
        if state.news_lookup_failures:
            interval = min(NEWS_RETRY_INTERVAL_SEC * state.news_lookup_failures, NEWS_LOOKUP_INTERVAL_SEC)
        else:
            interval = NEWS_LOOKUP_INTERVAL_SEC
        if (now - last).total_seconds() < interval:
            return

    state.last_news_check = now
    try:
        state.next_news_time = get_next_news_event()
    except NewsCalendarError as e:
        state.news_lookup_failures += 1
        log.warning(f"⚠️ News calendar уншигдсангүй ({state.news_lookup_failures} дахь удаа): {e}")
        if state.news_lookup_failures == NEWS_FAILURE_ALERT_AT:
            notifications.send_telegram(format_block("МЭДЭЭНИЙ ЗОГСООЛТ АЖИЛЛАХГҮЙ БАЙНА", "⚠️", [
                ("Шалтгаан", str(e)[:200]),
                ("Үр дагавар", "CPI/FOMC-ийн өмнөх түр зогсоолт хийгдэхгүй"),
                ("Техник арилжаа", "хэвийн үргэлжилнэ"),
                ("Шийдэл", "config.json → news_trading.calendar_url шалгах"),
            ]))
        return

    if state.news_lookup_failures:
        log.info("✅ News calendar дахин уншигдлаа")
    state.news_lookup_failures = 0


def execute_post_news_trade():
    if not NEWS_POST_TRADE_ENABLED:
        return
    if state.news_trade_done:
        return

    for symbol in NEWS_SYMBOLS:
        df = market_data.get_klines(symbol, interval="15m", limit=10)
        if len(df) < 5:
            continue

        first_close = df.iloc[0]["close"]
        last_close = df.iloc[-1]["close"]
        move_pct = (last_close - first_close) / first_close * 100

        if abs(move_pct) < NEWS_MIN_MOVE:
            log.info(f"⏸️ {symbol} move {move_pct:.2f}% < {NEWS_MIN_MOVE}%, skipping.")
            continue

        side = "BUY" if move_pct > 0 else "SELL"
        close_side = "SELL" if side == "BUY" else "BUY"
        position_side = "LONG" if side == "BUY" else "SHORT"

        balance = account.get_usdt_balance()
        allocation = balance * NEWS_ALLOCATION
        notional = allocation * NEWS_LEVERAGE
        price = last_close
        quantity = notional / price
        quantity = market_data.round_quantity(symbol, quantity)
        if quantity is None or quantity <= 0:
            continue

        order = order_api.place_market_order(symbol, side, quantity, reduce_only=False, position_side=position_side)
        if utils.is_api_error(order):
            notifications.send_telegram(f"❌ News trade order failed for {symbol}: {order}")
            continue

        entry_price = utils.safe_float(order.get("avgPrice"), price)
        if entry_price <= 0:
            entry_price = price

        if side == "BUY":
            sl_price = market_data.round_price(symbol, entry_price * (1 - NEWS_SL_PCT / 100))
            tp_price = market_data.round_price(symbol, entry_price * (1 + NEWS_TP_PCT / 100))
        else:
            sl_price = market_data.round_price(symbol, entry_price * (1 + NEWS_SL_PCT / 100))
            tp_price = market_data.round_price(symbol, entry_price * (1 - NEWS_TP_PCT / 100))

        order_api.place_stop_loss_order(symbol, close_side, quantity, sl_price, position_side=position_side)
        order_api.place_take_profit_order(symbol, close_side, quantity, tp_price, position_side=position_side)

        notifications.send_telegram(
            format_block(
                "📰 POST-NEWS TRADE EXECUTED",
                "🚀",
                [
                    ("Symbol", symbol),
                    ("Side", side),
                    ("Entry", f"${entry_price:.2f}"),
                    ("SL", f"${sl_price:.2f} ({NEWS_SL_PCT}%)"),
                    ("TP", f"${tp_price:.2f} ({NEWS_TP_PCT}%)"),
                    ("Leverage", f"{NEWS_LEVERAGE}x"),
                    ("Allocation", f"{NEWS_ALLOCATION*100:.1f}%"),
                ]
            )
        )
        break
