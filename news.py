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


def get_next_cpi_event():
    if not NEWS_CALENDAR_URL:
        return None
    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=10)
        data = resp.json()
        now = datetime.now(pytz.UTC)
        ny_tz = pytz.timezone('America/New_York')
        for item in data:
            if "CPI" in item.get("title", "") and "USD" in item.get("country", ""):
                raw_dt = datetime.fromisoformat(item["date"])
                # pytz needs localize(); .replace(tzinfo=) yields a wrong LMT offset.
                if raw_dt.tzinfo is None:
                    raw_dt = ny_tz.localize(raw_dt)
                event_time_utc = raw_dt.astimezone(pytz.UTC)
                if event_time_utc > now:
                    return event_time_utc
    except Exception as e:
        log.warning(f"⚠️ News calendar error: {e}")
    return None


def check_news_status():
    # News mode ONLY toggles news_mode_active. The main loop treats that as
    # "monitor open positions, don't open new technical trades" — it must NOT
    # touch safety_lock, because the main loop's safety_lock branch runs
    # safety_recovery() which force-closes every open position.
    if not NEWS_ENABLED:
        return

    now = datetime.now(pytz.UTC)
    stale = (not isinstance(state.last_news_check, datetime)) or (now - state.last_news_check).total_seconds() > 3600
    if not state.next_news_time or stale:
        state.next_news_time = get_next_cpi_event()
        state.last_news_check = now

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
        log.info("📰 News cooldown finished. Executing post-news trade...")
        execute_post_news_trade()
        state.news_trade_done = True
        state.news_mode_active = False
        return

    if diff <= - (NEWS_WAIT_AFTER + 30) and state.news_mode_active:
        state.news_mode_active = False
        log.info("✅ News window closed. Resuming normal trading.")


def execute_post_news_trade():
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
