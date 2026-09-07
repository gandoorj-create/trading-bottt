"""
backtest.py
Портфелийн симуляц: ботын ЖИНХЭНЭ шийдвэрийн кодыг түүхэн лаан дээр ажиллуулна.

Яагаад дахин бичсэн бэ:
Өмнөх хувилбар нь signal эсрэгээрээ эргэтэл барьдаг байсан — SL, TP, trailing,
хэсэгчилсэн TP, breakeven, хугацааны stop, ATR хэмжээ, MIN_SIGNAL_SCORE, MTF,
funding алийг нь ч загварчлаагүй. Өөрөөр хэлбэл бот ажиллуулдаггүй огт өөр
стратегийн тоо гаргаж, гарцын бүтцийг батлах чадваргүй байв.

Энэ хувилбарын гол зарчим: **дуурайхгүй, ботын кодыг өөрийг нь дуудна.**
  screening.analyze_frame  — signal, оноо, шүүлтүүрүүд
  screening.pick_candidates — нэр дэвшигч сонголт, корреляци
  risk.exit_levels / position_margin — ATR гарц ба хэмжээ
  risk.update_strategy_performance / check_drawdown_circuit_breaker — cooldown, halt
Симуляц нь зөвхөн БИРЖИЙН үүргийг гүйцэтгэнэ: захиалга биелүүлэх, шимтгэл,
funding, цаг хугацаа.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import sys

import numpy as np
import pandas as pd

from settings import *
from state import state, STRATEGY_NAMES
import account
import binance_client
import indicators
import market_data
import notifications
import persistence
import risk
import screening
import strategies
from logging_setup import get_logger, setup_logging

log = get_logger(__name__)

HOUR_MS = 3_600_000
# Live нь analyze_coin дотор 600 хаагдсан лаа дамжуулдаг. Backtest ижил
# өргөнтэй цонх өгөхгүй бол EMA-ийн утга зөрж, өөр signal гарна.
SIGNAL_WINDOW = 600


# ----------------------------------------------------------------
# Симуляцын горим: гадаад ертөнцийг таслах
# ----------------------------------------------------------------

@contextmanager
def market_data_source(url):
    """Түүхэн өгөгдлийг production эндпойнтоос уншина.

    Бот demo дансан дээр ажилладаг ч demo-гийн лаа/funding түүх бодит бус,
    эсвэл богино байдаг. Эдгээр нь нээлттэй эндпойнт тул түлхүүр хэрэггүй,
    арилжааны зам нь BASE_URL дээрээ хэвээр үлдэнэ.
    """
    if not url or url == binance_client.BASE_URL:
        yield
        return
    saved = binance_client.BASE_URL
    binance_client.BASE_URL = url
    log.info(f"📡 Түүхэн өгөгдөл: {url}")
    try:
        yield
    finally:
        binance_client.BASE_URL = saved


@contextmanager
def simulation_mode(equity_getter, halt_on_drawdown=True):
    """Telegram, диск, баланс ба runtime state-ийг симуляц руу чиглүүлнэ.

    Ингэснээр risk.py-ийн жинхэнэ cooldown ба drawdown код backtest дотор
    хэвээрээ ажиллана — дахин бичихгүй тул хэзээ ч салж холбогдохгүй.

    State-ийг заавал тусгаарлана: симуляц нь state.reset() дуудаж, стратегийн
    статистикийг өөрийнхөөрөө дүүргэдэг. Амьд процесс дотор (bot.py эхлэхэд)
    үүнийг хийвэл sync_existing_positions-ийн барьсан нээлттэй арилжааны
    бүртгэл, drawdown-ы оргил утга бүгд устана — өөрөөр хэлбэл backtest нь
    ажиллаж буй ботоо сүйтгэнэ. Тиймээс бүх атрибутыг хуулж аваад буцаана.
    """
    state_snapshot = dict(state.__dict__)
    # Breaker-ыг унтраахдаа хязгаарыг нь 0 болгоно — check_drawdown_circuit_breaker
    # тэр үед эрт буцдаг тул "хязгааргүй тохируулсан бот" яг ижил зан гаргана.
    saved_drawdown_limit = risk.MAX_SESSION_DRAWDOWN_PCT
    saved = (
        notifications.send_telegram,
        persistence.save_strategy_state,
        persistence.save_session_state,
        account.get_usdt_balance,
    )
    notifications.send_telegram = lambda text, pin=False: None
    persistence.save_strategy_state = lambda: None
    persistence.save_session_state = lambda: None
    account.get_usdt_balance = equity_getter
    if not halt_on_drawdown:
        risk.MAX_SESSION_DRAWDOWN_PCT = 0.0
    try:
        yield
    finally:
        (notifications.send_telegram, persistence.save_strategy_state,
         persistence.save_session_state, account.get_usdt_balance) = saved
        risk.MAX_SESSION_DRAWDOWN_PCT = saved_drawdown_limit
        state.__dict__.clear()
        state.__dict__.update(state_snapshot)


# ----------------------------------------------------------------
# Өгөгдөл
# ----------------------------------------------------------------

def exec_bars_agree(signal_df, exec_df, samples=40, tolerance=0.02):
    """Нарийн лаа нь 1h лаатай ижил үнийн цувааг илэрхийлж байгаа эсэх.

    Хоёр давтамжийн өгөгдөл зөрөх нь (тухайн symbol-ийн 15м түүх хожуу
    эхэлсэн, өөр хос татагдсан, эсвэл эх сурвалж эвдэрсэн) чимээгүй боловч
    гамшигтай: entry нь 1h барын нээлтээр тогтоогдож, гарц нь огт өөр үнэ
    дээр шийдэгдэн арилжаа бүр цоорхойгоор stop цохисон мэт харагдана.
    Ийм үед 1h лаа руу буцах нь бүдүүлэг ч ҮНЭН үр дүн өгнө.
    """
    if exec_df is None or len(exec_df) == 0:
        return False
    exec_open = dict(zip(exec_df["time"] // HOUR_MS * HOUR_MS, exec_df["open"]))
    checked = mismatched = 0
    for ts, open_price in zip(signal_df["time"].iloc[-samples:], signal_df["open"].iloc[-samples:]):
        other = exec_open.get(int(ts) // HOUR_MS * HOUR_MS)
        if other is None or open_price <= 0:
            continue
        checked += 1
        if abs(other - open_price) / open_price > tolerance:
            mismatched += 1
    if checked < samples // 4:
        return False
    return mismatched <= checked * 0.1


def load_history(symbols, days, exec_interval="15m", progress=True, data_url=None, offset_days=0):
    """Signal-ийн 1h лаа, гарц шийдэх нарийн лаа, funding түүх.

    Гарцыг 1h лаан дээр шийдэх нь хамгийн том худал эх сурвалж: нэг лаанд
    SL ба TP хоёул хүрсэн бол алийг нь эхэлж цохисныг мэдэх аргагүй. Илүү
    нарийн (15m) лаа ашиглах нь тэр тодорхойгүй байдлыг 4 дахин багасгана.

    offset_days: цонхны төгсгөлийг өнөөдрөөс хэдэн хоногоор ухраах вэ. Ижил
    тохиргоог хэд хэдэн ӨӨР хугацаанд шалгах цорын ганц арга — үүнгүйгээр
    бүх ажиллагаа ижил сүүлийн үеийг хэмжиж, шилдэг хувилбар үнэхээр
    ажилладаг уу эсвэл тэр хугацаанд таарсан уу гэдгийг ялгах боломжгүй.
    """
    end_ms = binance_client.current_timestamp_ms() - int(offset_days) * 24 * HOUR_MS
    # Warmup: signal цонх (600 лаа) + туршилтын хугацаа
    start_ms = end_ms - (days * 24 + SIGNAL_WINDOW + 24) * HOUR_MS
    data = {}

    with market_data_source(data_url if data_url is not None else BACKTEST_DATA_URL):
        for n, symbol in enumerate(symbols, 1):
            if progress:
                log.info(f"📥 [{n}/{len(symbols)}] {symbol} өгөгдөл татаж байна...")
            signal_df = market_data.get_klines_range(symbol, "1h", start_ms, end_ms)
            if len(signal_df) < SIGNAL_WINDOW + 48:
                log.warning(f"⚠️ {symbol}: хангалттай түүх алга ({len(signal_df)} лаа) — алгаслаа")
                continue
            exec_df = market_data.get_klines_range(symbol, exec_interval, start_ms, end_ms)
            used_interval = exec_interval
            if not exec_bars_agree(signal_df, exec_df):
                log.warning(f"⚠️ {symbol}: {exec_interval} лаа 1h-тэй таарахгүй — гарцыг 1h дээр шийднэ")
                exec_df, used_interval = signal_df, "1h"
            funding = market_data.get_funding_history(symbol, start_ms, end_ms) if FUNDING_ENABLED else []
            data[symbol] = {
                "signal": signal_df,
                "exec": exec_df,
                "exec_interval": used_interval,
                "funding": funding,
            }
    return data


# ----------------------------------------------------------------
# Биржийн дуураймал: биелэлт
# ----------------------------------------------------------------

def _down_fill(bar_open, bar_low, level):
    """Доош хөдлөх үед level биелэх үнэ (цоорхойг тооцно)."""
    if level is None:
        return None
    if bar_open <= level:
        return bar_open          # лаа аль хэдийн доогуур нээгдсэн
    if bar_low <= level:
        return level
    return None


def _up_fill(bar_open, bar_high, level):
    if level is None:
        return None
    if bar_open >= level:
        return bar_open
    if bar_high >= level:
        return level
    return None


def _adverse_fill(pos, bar, level):
    if pos["side"] == "BUY":
        return _down_fill(bar["open"], bar["low"], level)
    return _up_fill(bar["open"], bar["high"], level)


def _favourable_fill(pos, bar, level):
    if pos["side"] == "BUY":
        return _up_fill(bar["open"], bar["high"], level)
    return _down_fill(bar["open"], bar["low"], level)


def _slipped(pos, price, entering=False):
    """Market биелэлт үргэлж бидний эсрэг гулсана."""
    slip = BACKTEST_SLIPPAGE_RATE
    buying = (pos["side"] == "BUY") if entering else (pos["side"] == "SELL")
    return price * (1 + slip) if buying else price * (1 - slip)


def _leg_pnl(pos, price, qty):
    direction = 1 if pos["side"] == "BUY" else -1
    return (price - pos["entry"]) * qty * direction


def _close_leg(pos, raw_price, qty, reason, ts, sim):
    """Позицын хэсэг эсвэл бүхлийг хааж, зардлыг нь бүртгэнэ."""
    price = _slipped(pos, raw_price)
    gross = _leg_pnl(pos, price, qty)
    fee = price * qty * BACKTEST_FEE_RATE
    pos["gross"] += gross
    pos["fees"] += fee
    pos["qty"] -= qty
    sim["realized"] += gross - fee
    sim["total_gross"] += gross
    sim["total_fees"] += fee
    pos.setdefault("legs", []).append({"reason": reason, "price": price, "qty": qty, "ts": ts})
    if pos["qty"] <= 1e-12:
        _finalize(pos, reason, ts, sim)
        return True
    return False


def _finalize(pos, reason, ts, sim):
    net = pos["gross"] - pos["fees"] - pos["funding"]
    sim["trades"].append({
        "symbol": pos["symbol"],
        "strategy": pos["strategy"],
        "side": pos["side"],
        "score": pos["score"],
        "regime": pos["regime"],
        "atr_pct": pos["atr_pct"],
        "sl_pct": pos["levels"]["sl"],
        "entry": pos["entry"],
        "margin": pos["margin"],
        "opened_ms": pos["opened_ms"],
        "closed_ms": ts,
        "hold_hours": (ts - pos["opened_ms"]) / HOUR_MS,
        "gross": pos["gross"],
        "fees": pos["fees"],
        "funding": pos["funding"],
        "net": net,
        "net_pct_margin": net / pos["margin"] * 100 if pos["margin"] else 0.0,
        "exit_reason": reason,
        "partial_hit": pos["partial_done"],
    })
    # Жинхэнэ cooldown/статистикийн код — дахин бичихгүй
    risk.update_strategy_performance(pos["strategy"], net)
    sim["open"].pop(pos["symbol"], None)


def advance_position(pos, bar, sim):
    """Нэг дэд лаан дээрх гарцуудыг шийднэ.

    Дарааллын дүрэм (лаан доторх замыг мэдэхгүй тул консерватив):
      1) Эсрэг чиглэлийн түвшин (SL / breakeven / trailing) эхэлж биелнэ.
         Хэд хэдэн нь цохисон бол хөдөлгөөний чиглэлд хамгийн ойрхон нь
         эхэлнэ — энэ нь таамаг биш, физик.
      2) Дараа нь ашгийн түвшин: partial эхлээд, дараа нь бүтэн TP.
      3) Trailing-ийн оргилыг лааны ТӨГСГӨЛД шинэчилнэ — тухайн лаан дотор
         арматлаад тэр дороо ашигтай биелэх боломж өгөхгүй.
      4) Breakeven stop нь ДАРААГИЙН лаанаас хүчинтэй — амьд бот partial
         биелэлтийг мониторингийн дараагийн мөчлөгт л хардаг.
    """
    ts = int(bar["time"])

    if pos.get("pending_breakeven") is not None:
        pos["stop_price"] = pos.pop("pending_breakeven")
        pos["breakeven_done"] = True

    # --- 1) эсрэг чиглэл ---
    hits = []
    fill = _adverse_fill(pos, bar, pos["stop_price"])
    if fill is not None:
        hits.append((pos["stop_price"], fill, "BREAKEVEN" if pos["breakeven_done"] else "STOP_LOSS"))
    if pos["trail_armed"]:
        callback = TRAILING_CALLBACK_RATE / 100
        trail_stop = (pos["trail_extreme"] * (1 - callback) if pos["side"] == "BUY"
                      else pos["trail_extreme"] * (1 + callback))
        fill = _adverse_fill(pos, bar, trail_stop)
        if fill is not None:
            hits.append((trail_stop, fill, "TRAILING"))
    if hits:
        # Урвуу хөдөлгөөнд хамгийн эхэнд тааралдах = long-д хамгийн ӨНДӨР түвшин
        chosen = max(hits, key=lambda x: x[0]) if pos["side"] == "BUY" else min(hits, key=lambda x: x[0])
        _close_leg(pos, chosen[1], pos["qty"], chosen[2], ts, sim)
        return

    # --- 2) ашгийн түвшин ---
    if not pos["partial_done"] and pos["partial_price"] is not None:
        fill = _favourable_fill(pos, bar, pos["partial_price"])
        if fill is not None:
            partial_qty = min(pos["qty"] * PARTIAL_TP_RATIO, pos["qty"])
            closed = _close_leg(pos, fill, partial_qty, "PARTIAL_TP", ts, sim)
            pos["partial_done"] = True
            if closed:
                return
            offset = BREAKEVEN_OFFSET_PCT / 100
            pos["pending_breakeven"] = (pos["entry"] * (1 + offset) if pos["side"] == "BUY"
                                        else pos["entry"] * (1 - offset))

    fill = _favourable_fill(pos, bar, pos["tp_price"])
    if fill is not None:
        _close_leg(pos, fill, pos["qty"], "TAKE_PROFIT", ts, sim)
        return

    # --- 3) trailing-ийн оргилыг лааны төгсгөлд ---
    if pos["side"] == "BUY":
        pos["trail_extreme"] = max(pos["trail_extreme"], bar["high"])
        if not pos["trail_armed"] and bar["high"] >= pos["trail_activation"]:
            pos["trail_armed"] = True
    else:
        pos["trail_extreme"] = min(pos["trail_extreme"], bar["low"])
        if not pos["trail_armed"] and bar["low"] <= pos["trail_activation"]:
            pos["trail_armed"] = True


# ----------------------------------------------------------------
# Портфелийн гогцоо
# ----------------------------------------------------------------

def _mtf_for(df):
    """Live-ийн get_mtf_signal-ийн backtest дэх хувилбар.

    Live нь биржээс 4h лаа татдаг, энд 1h-ээс нэгтгэнэ. Шийдвэрийн логик нь
    strategies.mtf_from_frames дотор ганц хувилбартай.
    """
    df_4h = indicators.resample_ohlcv(df, 4)
    if df_4h is None:
        return "NEUTRAL"
    return strategies.mtf_from_frames(df_4h.iloc[-60:], df.iloc[-60:])


def _funding_at(funding, ts):
    """ts мөчид хүчинтэй хамгийн сүүлийн funding хувь (sentiment-д)."""
    rate = 0.0
    for ftime, frate in funding:
        if ftime > ts:
            break
        rate = frate
    return rate


def _correlation_builder(data):
    """Түүхэн өгөгдлөөс корреляци — live-ийн кэштэй сүлжээний дуудлагыг орлоно.

    fn.time-ыг мөчлөг тутамд шинэчилнэ; кэш нь live-ийн 4 цагийн TTL-ийг дуурайна.
    """
    cache = {}
    index_by_time = {s: dict(zip(d["signal"]["time"], range(len(d["signal"]))))
                     for s, d in data.items()}

    def fn(symbol1, symbol2, lookback=50):
        ts = fn.time
        key = (tuple(sorted((symbol1, symbol2))), ts // (4 * HOUR_MS))
        if key in cache:
            return cache[key]
        corr = 0.0
        try:
            i1 = index_by_time[symbol1].get(ts)
            i2 = index_by_time[symbol2].get(ts)
            if i1 is not None and i2 is not None:
                c1 = data[symbol1]["signal"]["close"].iloc[max(0, i1 - lookback):i1 + 1]
                c2 = data[symbol2]["signal"]["close"].iloc[max(0, i2 - lookback):i2 + 1]
                r1, r2 = c1.pct_change().dropna(), c2.pct_change().dropna()
                if len(r1) >= 10 and len(r2) >= 10:
                    n = min(len(r1), len(r2))
                    value = float(np.corrcoef(r1.iloc[-n:], r2.iloc[-n:])[0, 1])
                    corr = 0.0 if np.isnan(value) else value
        except Exception:
            corr = 0.0
        cache[key] = corr
        return corr

    fn.time = 0
    return fn


def _open_position(sim, coin, entry_bar, equity, margin_used):
    """execute_trades-ийн шийдвэрийг давтана (жинхэнэ risk.* функцүүдээр)."""
    symbol = coin["symbol"]
    levels = risk.exit_levels(coin.get("atr_pct"))
    margin = risk.position_margin(equity, levels["sl"])
    if margin_used + margin > equity * MAX_TOTAL_MARGIN_USAGE:
        return None
    if equity < MIN_BALANCE_USDT:
        return None

    side = coin["signal"]
    entry = _slipped({"side": side}, float(entry_bar["open"]), entering=True)
    if entry <= 0:
        return None
    qty = margin * LEVERAGE / entry
    fee = entry * qty * BACKTEST_FEE_RATE
    sim["realized"] -= fee
    sim["total_fees"] += fee

    sign = 1 if side == "BUY" else -1
    pos = {
        "symbol": symbol, "strategy": coin["strategy"], "side": side,
        "entry": entry, "qty": qty, "margin": margin,
        "levels": levels, "score": coin["score"], "regime": coin["regime"],
        "atr_pct": coin.get("atr_pct", 0.0),
        "stop_price": entry * (1 - sign * levels["sl"] / 100),
        "tp_price": entry * (1 + sign * levels["tp"] / 100),
        "partial_price": entry * (1 + sign * levels["partial"] / 100) if PARTIAL_TP_ENABLED else None,
        "trail_activation": entry * (1 + sign * levels["trail"] / 100),
        "partial_done": False, "breakeven_done": False,
        "trail_armed": False, "trail_extreme": entry,
        "pending_breakeven": None,
        "opened_ms": int(entry_bar["time"]),
        "gross": 0.0, "fees": fee, "funding": 0.0,
    }
    sim["open"][symbol] = pos
    return pos


def _unrealized(sim, closes):
    total = 0.0
    for symbol, pos in sim["open"].items():
        price = closes.get(symbol)
        if price:
            total += _leg_pnl(pos, price, pos["qty"])
    return total


def _row_at(df, index):
    if index < 0 or index >= len(df):
        return None
    return df.iloc[index]


def _analyses_at(data, symbols, index_by_time, t):
    """Тухайн мөчид бүх symbol-ийн шинжилгээ. Ботын жинхэнэ analyze_frame."""
    analyses = []
    for symbol in symbols:
        i = index_by_time[symbol].get(t)
        if i is None or i + 1 < SIGNAL_WINDOW:
            continue
        df = data[symbol]["signal"].iloc[max(0, i + 1 - SIGNAL_WINDOW):i + 1]
        if len(df) < 210:
            continue
        result = screening.analyze_frame(
            symbol, df, _mtf_for(df), _funding_at(data[symbol]["funding"], t)
        )
        if result:
            analyses.append(result)
    return analyses


@contextmanager
def neutral_screening():
    """Шинжилгээг тохиргооноос ХАМААРАЛГҮЙ болгоно.

    analyze_frame нь MIN_SIGNAL_SCORE-оос доош онооны signal-ыг HOLD болгодог,
    мөн идэвхгүй стратегиудыг алгасдаг. Кэшийг бүх тохиргоонд тохирохоор
    барихын тулд босгыг 0, бүх стратегийг идэвхтэй болгоно — жинхэнэ шүүлт нь
    pick_candidates дотор хийгддэг тул үр дүн ижил хэвээр.
    """
    saved = screening.MIN_SIGNAL_SCORE
    saved_active = {name: state.strategy_stats[name]["active"] for name in STRATEGY_NAMES}
    screening.MIN_SIGNAL_SCORE = 0.0
    for name in STRATEGY_NAMES:
        state.strategy_stats[name]["active"] = True
    try:
        yield
    finally:
        screening.MIN_SIGNAL_SCORE = saved
        for name, value in saved_active.items():
            state.strategy_stats[name]["active"] = value


def build_analysis_cache(data, progress=True):
    """Бүх мөчлөгийн шинжилгээг НЭГ удаа тооцоолж кэшилнэ.

    Энэ нь симуляцын хугацааны ~85%-ыг эзэлдэг бөгөөд тохиргооноос
    хамаардаггүй тул хэд хэдэн хувилбар турших үед дахин тооцоолох нь цэвэр
    үрэлгэн байдал: 5 хувилбар × 11 мин биш, 11 мин + 5 × 2 мин.
    """
    symbols = list(data)
    master = sorted(set().union(*[set(d["signal"]["time"]) for d in data.values()]))
    index_by_time = {sym: dict(zip(d["signal"]["time"], range(len(d["signal"]))))
                     for sym, d in data.items()}
    selection_every = max(1, int(SELECTION_INTERVAL_MINUTES // 60))
    first, last = SIGNAL_WINDOW, len(master) - 2

    cache = {}
    with neutral_screening():
        for n, mi in enumerate(range(first, last + 1, selection_every)):
            t = master[mi]
            cache[t] = _analyses_at(data, symbols, index_by_time, t)
            if progress and n and n % 50 == 0:
                done = (mi - first) / max(1, last - first) * 100
                log.info(f"   шинжилгээ {done:5.1f}%")
    return cache


def _group_exec_bars(df, interval):
    """Гарц шийдэх лааг цагаар нь бүлэглэнэ (1h бар → дэд лаанууд)."""
    grouped = {}
    for row in df.to_dict("records"):
        hour = int(row["time"]) // HOUR_MS * HOUR_MS
        grouped.setdefault(hour, []).append(row)
    return grouped


def run_portfolio_backtest(data, start_balance, progress=True, disabled=None,
                           halt_on_drawdown=True, analysis_cache=None):
    """Ботын бүх мөчлөгийг түүхэн өгөгдөл дээр давтана.

    disabled: тухайн ажиллагаанд оролцуулахгүй стратегиудын нэр. `paused_cycles`
    нь 0 хэвээр тул update_strategy_cooldowns тэднийг дахин асаахгүй.
    analysis_cache: build_analysis_cache-ийн үр дүн. Байвал лаа дахин
    шинжлэхгүй — олон хувилбар харьцуулахад л ялгаа гаргана.
    halt_on_drawdown: False үед circuit breaker унтарна. Одоогийн тохиргоогоор
    бот эхний долоо хоногт хязгаартаа хүрч зогсдог тул урт хугацааны зан төлөв
    хэмжигдэхгүй байсан — ямар ч `--days` утга ижил долоо хоногийг л хэмждэг.
    """
    symbols = list(data)
    if not symbols:
        return None

    master = sorted(set().union(*[set(d["signal"]["time"]) for d in data.values()]))
    index_by_time = {s: dict(zip(d["signal"]["time"], range(len(d["signal"]))))
                     for s, d in data.items()}
    exec_bars = {s: _group_exec_bars(d["exec"], d["exec_interval"]) for s, d in data.items()}
    corr_fn = _correlation_builder(data)

    sim = {
        "open": {}, "trades": [], "realized": 0.0,
        "total_gross": 0.0, "total_fees": 0.0, "total_funding": 0.0,
        "equity_curve": [], "pending": [],
    }
    equity = lambda: start_balance + sim["realized"]

    selection_every = max(1, int(SELECTION_INTERVAL_MINUTES // 60))
    first = SIGNAL_WINDOW
    last = len(master) - 2
    halted_reason = None
    cycles = 0
    # Симуляц эрт зогсож болно (drawdown halt). Тайлан бүх өгөгдлийн хугацаагаар
    # хуваавал "сард X%" ба BTC-тэй харьцуулалт хоёулаа гуйвна — арилжаа
    # хийгээгүй өдрүүдийг тоолно гэсэн үг. Тиймээс үнэхээр боловсруулсан
    # сүүлийн барыг хөтөлж, тайланг түүгээр хэмжинэ.
    last_processed_ms = master[first]

    with simulation_mode(equity, halt_on_drawdown=halt_on_drawdown):
        # Snapshot аль хэдийн авагдсаны ДАРАА цэвэрлэнэ — эс тэгвээс амьд
        # ботын state-ийг устгачихаад устсан хувилбарыг нь буцаана.
        state.reset()
        state.session_start_balance = start_balance
        state.session_peak_balance = start_balance
        for name in (disabled or []):
            state.strategy_stats[name]["active"] = False

        for mi in range(first, last + 1):
            t = master[mi]
            next_t = master[mi + 1]

            # (1) өмнөх барын шийдвэрээр хаах (lookahead-гүй: дараагийн нээлтээр)
            for symbol, reason in sim["pending"]:
                pos = sim["open"].get(symbol)
                row = _row_at(data[symbol]["signal"], index_by_time[symbol].get(next_t, -1))
                if pos is not None and row is not None:
                    _close_leg(pos, float(row["open"]), pos["qty"], reason, int(row["time"]), sim)
            sim["pending"] = []

            if halted_reason:
                break

            # (2) screening — амьд бот шиг SELECTION_INTERVAL тутамд
            if (mi - first) % selection_every == 0 and not state.safety_lock:
                cycles += 1
                risk.update_strategy_cooldowns()
                if analysis_cache is not None:
                    analyses = analysis_cache.get(t, [])
                else:
                    analyses = _analyses_at(data, symbols, index_by_time, t)

                corr_fn.time = t
                selected, _, _ = screening.pick_candidates(analyses, corr_fn)
                margin_used = sum(p["margin"] for p in sim["open"].values())
                for coin in selected:
                    if coin["symbol"] in sim["open"]:
                        continue
                    if len(sim["open"]) >= MAX_SELECTIONS:
                        break
                    row = _row_at(data[coin["symbol"]]["signal"], index_by_time[coin["symbol"]].get(next_t, -1))
                    if row is None:
                        continue
                    pos = _open_position(sim, coin, row, equity(), margin_used)
                    if pos:
                        margin_used += pos["margin"]

            # (3) дараагийн барын дэд лаанууд дээр гарцуудыг шийднэ
            for symbol in list(sim["open"]):
                bars = exec_bars[symbol].get(next_t)
                if not bars:
                    row = _row_at(data[symbol]["signal"], index_by_time[symbol].get(next_t, -1))
                    bars = [row.to_dict()] if row is not None else []
                for bar in bars:
                    pos = sim["open"].get(symbol)
                    if pos is None:
                        break
                    advance_position(pos, bar, sim)

            # (4) funding
            closes = {}
            for symbol in symbols:
                row = _row_at(data[symbol]["signal"], index_by_time[symbol].get(next_t, -1))
                if row is not None:
                    closes[symbol] = float(row["close"])
            for symbol, pos in sim["open"].items():
                price = closes.get(symbol, pos["entry"])
                for ftime, frate in data[symbol]["funding"]:
                    if next_t <= ftime < next_t + HOUR_MS:
                        # Эерэг funding-ийг long төлж, short хүлээж авна
                        cost = pos["qty"] * price * frate * (1 if pos["side"] == "BUY" else -1)
                        pos["funding"] += cost
                        sim["realized"] -= cost
                        sim["total_funding"] += cost

            # (5) барын хаалт: хугацааны stop, зорилт, drawdown
            bar_close_ms = next_t + HOUR_MS
            last_processed_ms = bar_close_ms
            for symbol, pos in list(sim["open"].items()):
                if MAX_HOLD_HOURS <= 0:
                    break
                if (bar_close_ms - pos["opened_ms"]) / HOUR_MS < MAX_HOLD_HOURS:
                    continue
                price = closes.get(symbol)
                if not price:
                    continue
                move = (price - pos["entry"]) / pos["entry"] * 100
                if pos["side"] == "SELL":
                    move = -move
                if abs(move) <= TIME_STOP_FLAT_PCT:
                    sim["pending"].append((symbol, "TIME_STOP"))

            unreal = _unrealized(sim, closes)
            # Хоёр утга: mark-to-market (жинхэнэ drawdown) ба realized (амьд
            # breaker нь walletBalance хардаг тул түүнтэй ижил суурь)
            sim["equity_curve"].append((next_t, equity() + unreal, equity()))

            if sim["open"] and unreal >= TARGET_PROFIT:
                for symbol in sim["open"]:
                    sim["pending"].append((symbol, "TARGET"))

            risk.check_drawdown_circuit_breaker()
            if state.drawdown_halt:
                halted_reason = "DRAWDOWN_HALT"
                for symbol in list(sim["open"]):
                    pos = sim["open"][symbol]
                    _close_leg(pos, closes.get(symbol, pos["entry"]), pos["qty"], "DRAWDOWN_HALT", next_t, sim)

            if progress and cycles and (mi - first) % (selection_every * 100) == 0:
                done = (mi - first) / max(1, last - first) * 100
                log.info(f"   ... {done:5.1f}% | equity ${equity() + unreal:,.0f} | арилжаа {len(sim['trades'])}")

        # Үлдсэн позицуудыг сүүлийн үнээр хаана. Заавал simulation_mode
        # ДОТОР — _close_leg нь risk.update_strategy_performance дуудаж
        # стратегийн статистикт бичдэг тул гадна нь бол амьд ботын
        # тоонуудыг симуляцын арилжаагаар бохирдуулна.
        final_t = master[-1]
        for symbol in list(sim["open"]):
            pos = sim["open"][symbol]
            row = _row_at(data[symbol]["signal"], index_by_time[symbol].get(final_t, -1))
            price = float(row["close"]) if row is not None else pos["entry"]
            _close_leg(pos, price, pos["qty"], "END_OF_TEST", final_t, sim)

    sim["start_balance"] = start_balance
    sim["final_balance"] = equity()
    sim["halted"] = halted_reason
    sim["cycles"] = cycles
    sim["from_ms"] = master[first]
    sim["to_ms"] = last_processed_ms
    # Өгөгдөл хаана дуусахыг тусад нь хадгална: эрт зогссоныг харуулахад хэрэгтэй
    sim["data_to_ms"] = master[-1] + HOUR_MS
    sim["symbols"] = symbols
    sim["disabled"] = sorted(disabled or [])
    sim["halt_enabled"] = halt_on_drawdown
    sim["breach_ms"] = _drawdown_breach_ms(sim["equity_curve"], MAX_SESSION_DRAWDOWN_PCT)
    return sim


# ----------------------------------------------------------------
# Хувилбар харьцуулах (sweep)
# ----------------------------------------------------------------

@contextmanager
def config_overrides(min_score=None, regimes=None, partial_tp=None):
    """Тухайн хувилбарын тохиргоог түр хүчинтэй болгоно.

    Тогтмолууд `from settings import *`-аар модуль тус бүрд хуулбарлагддаг тул
    уншиж буй модульд нь шууд тавина, дараа нь буцаана.
    """
    saved = (screening.MIN_SIGNAL_SCORE, screening.ALLOWED_REGIMES, globals()["PARTIAL_TP_ENABLED"])
    if min_score is not None:
        screening.MIN_SIGNAL_SCORE = min_score
    if regimes is not None:
        screening.ALLOWED_REGIMES = regimes
    if partial_tp is not None:
        globals()["PARTIAL_TP_ENABLED"] = partial_tp
    try:
        yield
    finally:
        screening.MIN_SIGNAL_SCORE, screening.ALLOWED_REGIMES = saved[0], saved[1]
        globals()["PARTIAL_TP_ENABLED"] = saved[2]


DEFAULT_SWEEP = [
    {"name": "жишиг (одоогийн)"},
    {"name": "зөвхөн STRONG_TREND", "regimes": ["STRONG_TREND"]},
    {"name": "trend горимууд", "regimes": ["STRONG_TREND", "TRENDING"]},
    {"name": "partial TP унтраасан", "partial_tp": False},
    {"name": "min_score 24", "min_score": 24.0},
    {"name": "STRONG_TREND + partial TP off", "regimes": ["STRONG_TREND"], "partial_tp": False},
]


def run_sweep(data, start_balance, configs=None, disabled=None,
              halt_on_drawdown=False, progress=True):
    """Хэд хэдэн тохиргоог НЭГ шинжилгээний дамжлагаар харьцуулна.

    Лаа шинжлэх нь симуляцын хугацааны ~85%-ыг эзэлдэг ба тохиргооноос
    хамаардаггүй. Тиймээс нэг удаа тооцоолоод бүх хувилбарт хуваалцвал
    6 хувилбар нь 6 дахин биш, ~1.9 дахин удаан болно.
    """
    configs = configs or DEFAULT_SWEEP
    if progress:
        log.info(f"🔬 {len(configs)} хувилбар | шинжилгээг нэг удаа тооцоолж хуваалцана")
    cache = build_analysis_cache(data, progress=progress)

    results = []
    for n, cfg in enumerate(configs, 1):
        if progress:
            log.info(f"   [{n}/{len(configs)}] {cfg['name']}")
        with config_overrides(cfg.get("min_score"), cfg.get("regimes"), cfg.get("partial_tp")):
            sim = run_portfolio_backtest(
                data, start_balance, progress=False,
                disabled=cfg.get("disabled", disabled),
                halt_on_drawdown=cfg.get("halt", halt_on_drawdown),
                analysis_cache=cache,
            )
        sim["name"] = cfg["name"]
        results.append(sim)
    return results


def format_sweep_report(results, data=None):
    lines = []
    add = lines.append

    def when(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    first = results[0]
    days = (first["to_ms"] - first["from_ms"]) / (24 * HOUR_MS)
    add("=" * 78)
    add(f"ХУВИЛБАРУУДЫН ХАРЬЦУУЛАЛТ   {when(first['from_ms'])} → {when(first['to_ms'])}")
    add(f"{len(first['symbols'])} coin | {MAX_SELECTIONS} слот | {LEVERAGE}x | ${first['start_balance']:,.0f}")
    if first.get("disabled"):
        add(f"Унтраасан стратеги: {', '.join(first['disabled'])}")
    add("=" * 78)
    add("")
    add(f"  {'хувилбар':<32}{'n':>5}{'win%':>7}{'өгөөж':>9}{'expect$':>9}{'DD%':>7}{'PF':>6}")
    add("  " + "-" * 74)

    for sim in results:
        trades = sim["trades"]
        ret = (sim["final_balance"] - sim["start_balance"]) / sim["start_balance"] * 100
        if not trades:
            add(f"  {sim['name']:<32}{0:>5}{'—':>7}{ret:>8.2f}%{'—':>9}{'—':>7}{'—':>6}")
            continue
        nets = np.array([t["net"] for t in trades], dtype=float)
        wins, losses = nets[nets > 0], nets[nets < 0]
        pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() else float("inf")
        add(f"  {sim['name']:<32}{len(trades):>5}{(nets > 0).mean() * 100:>6.0f}%"
            f"{ret:>8.2f}%{nets.mean():>9.2f}{_max_drawdown(sim['equity_curve']):>6.1f}%{pf:>6.2f}")

    add("")
    add("─ ЖИНХЭНЭ БОТ ХЭЗЭЭ ЗОГСОХ БАЙСАН (15% breaker) ─────────────────────────────")
    for sim in results:
        breach = sim.get("breach_ms")
        if breach:
            day = (breach - sim["from_ms"]) / (24 * HOUR_MS)
            add(f"  {sim['name']:<32}{when(breach)}  ({day:.0f} дахь өдөр)")
        else:
            add(f"  {sim['name']:<32}зогсохгүй")

    add("")
    add("─ ЗАРДЛЫН ЭЗЛЭХ ХУВЬ ───────────────────────────────────────────────────────")
    add(f"  {'хувилбар':<32}{'gross$':>10}{'шимтгэл$':>11}{'funding$':>10}{'net$':>10}")
    for sim in results:
        add(f"  {sim['name']:<32}{sim['total_gross']:>10.0f}{-sim['total_fees']:>11.0f}"
            f"{-sim['total_funding']:>10.0f}{sim['final_balance'] - sim['start_balance']:>10.0f}")

    if data:
        bh = _buy_and_hold(data, first["from_ms"], first["to_ms"])
        if bh is not None:
            add("")
            add(f"ЖИШИГ: BTC зүгээр барьсан бол {bh:+.2f}% ({days:.0f} хоног)")

    best = max(results, key=lambda s: s["final_balance"])
    add("")
    add(f"🥇 Хамгийн сайн: {best['name']} "
        f"({(best['final_balance'] - best['start_balance']) / best['start_balance'] * 100:+.2f}%)")
    add("")
    add("⚠️ Эдгээрийг НЭГ хугацаанаас сонгож байгаа тул хамгийн сайн нь зүгээр л")
    add("   тэр хугацаанд таарсан байж болно. Ялагчийг ӨӨР хугацаан дээр заавал")
    add("   шалгах ёстой — эс тэгвээс түүхийг цээжилсэн үр дүн авна.")
    return "\n".join(lines)


# ----------------------------------------------------------------
# Тайлан
# ----------------------------------------------------------------

def _bucket_stats(trades, key_fn, order=None):
    groups = {}
    for trade in trades:
        groups.setdefault(key_fn(trade), []).append(trade)
    rows = []
    for key in (order or sorted(groups)):
        items = groups.get(key)
        if not items:
            continue
        nets = np.array([t["net"] for t in items], dtype=float)
        rows.append({
            "key": key, "n": len(items),
            "win_rate": float((nets > 0).mean() * 100),
            "net": float(nets.sum()),
            "expectancy": float(nets.mean()),
        })
    return rows


def _max_drawdown(curve, column=1):
    if not curve:
        return 0.0
    peak, worst = curve[0][column], 0.0
    for point in curve:
        value = point[column]
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


def _drawdown_breach_ms(curve, limit_pct):
    """Амьд breaker анх хэзээ буух байсан бэ (realized баланс дээр).

    Halt унтраалттай ажиллуулахад "жинхэнэ бот энэ өдөр зогсох байсан" гэдгийг
    харуулж, нэг ажиллагаанаас хоёр хариу авна: бүтэн хугацааны зан төлөв, мөн
    одоогийн хязгаар хаана таслах вэ.
    """
    if not curve or not limit_pct or limit_pct <= 0:
        return None
    peak = curve[0][2]
    for point in curve:
        value = point[2]
        peak = max(peak, value)
        if peak > 0 and (peak - value) / peak * 100 >= limit_pct:
            return point[0]
    return None


def _buy_and_hold(data, from_ms, to_ms, symbol="BTCUSDT"):
    if symbol not in data:
        return None
    df = data[symbol]["signal"]
    window = df[(df["time"] >= from_ms) & (df["time"] <= to_ms)]
    if len(window) < 2:
        return None
    first, last = float(window["close"].iloc[0]), float(window["close"].iloc[-1])
    return (last - first) / first * 100 if first else None


def format_report(sim, data=None):
    trades = sim["trades"]
    start, final = sim["start_balance"], sim["final_balance"]
    days = (sim["to_ms"] - sim["from_ms"]) / (24 * HOUR_MS)
    lines = []
    add = lines.append

    def when(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    add("=" * 72)
    add(f"ПОРТФЕЛИЙН BACKTEST   {when(sim['from_ms'])} → {when(sim['to_ms'])}  ({days:.0f} өдөр)")
    add(f"{len(sim['symbols'])} coin | {MAX_SELECTIONS} слот | {LEVERAGE}x | min_score={MIN_SIGNAL_SCORE}")
    if sim.get("disabled"):
        add(f"Унтраасан стратеги: {', '.join(sim['disabled'])}")
    data_days = (sim.get("data_to_ms", sim["to_ms"]) - sim["from_ms"]) / (24 * HOUR_MS)
    if data_days - days > 1:
        add(f"⚠️ {data_days:.0f} хоногийн өгөгдлөөс {days:.0f} дахь өдөр дээр ЗОГССОН — "
            f"доорх бүх тоо тэр {days:.0f} хоногийнх")
    add("=" * 72)

    if not trades:
        add("Арилжаа огт гүйцэтгэгдээгүй — босго хэт өндөр эсвэл өгөгдөл дутуу.")
        return "\n".join(lines)

    nets = np.array([t["net"] for t in trades], dtype=float)
    wins, losses = nets[nets > 0], nets[nets < 0]
    total_return = (final - start) / start * 100 if start else 0.0
    monthly = total_return / days * 30 if days else 0.0

    add("")
    add(f"Баланс         ${start:,.0f} → ${final:,.0f}   ({total_return:+.2f}%, сард {monthly:+.2f}%)")
    add(f"Арилжаа        {len(trades)}  ({len(trades) / days * 30:.0f}/сар)")
    add(f"Win rate       {(nets > 0).mean() * 100:.1f}%")
    add(f"Expectancy     ${nets.mean():+.2f}/арилжаа   (маржины {np.mean([t['net_pct_margin'] for t in trades]):+.2f}%)")
    add(f"Дундаж ашиг    ${wins.mean():+.2f}" if len(wins) else "Дундаж ашиг    —")
    add(f"Дундаж алдагдал ${losses.mean():+.2f}" if len(losses) else "Дундаж алдагдал —")
    profit_factor = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() else float("inf")
    add(f"Profit factor  {profit_factor:.2f}")
    add(f"Max drawdown   {_max_drawdown(sim['equity_curve']):.2f}%")
    add(f"Дундаж барилт  {np.mean([t['hold_hours'] for t in trades]):.1f} цаг")
    if sim.get("halted"):
        add(f"⚠️ ЗОГССОН: {sim['halted']} ({when(sim['to_ms'])})")
    if sim.get("halt_enabled") is False:
        add("")
        add(f"ℹ️ Drawdown breaker УНТРААЛТТАЙ ажилласан ({MAX_SESSION_DRAWDOWN_PCT:.0f}% хязгаар).")
        breach = sim.get("breach_ms")
        if breach:
            reached = (breach - sim["from_ms"]) / (24 * HOUR_MS)
            add(f"   Жинхэнэ бот {when(breach)}-нд ({reached:.0f} дахь өдөр) зогсох байсан;")
            add("   түүнээс хойшхи тоонууд нь 'хязгааргүй байсан бол' гэсэн таамаг.")
        else:
            add("   Хязгаарт хүрээгүй — бүтэн хугацаанд арилжаалсан.")

    add("")
    add("─ ЗАРДЛЫН ЗАДАРГАА ─────────────────────────────────────────────────")
    gross, fees, funding = sim["total_gross"], sim["total_fees"], sim["total_funding"]
    add(f"  Gross PnL          {gross:+10,.2f}")
    add(f"  Шимтгэл+slippage   {-fees:+10,.2f}   ({fees / start * 100 / days * 30:.2f}%/сар)")
    add(f"  Funding            {-funding:+10,.2f}   ({funding / start * 100 / days * 30:.2f}%/сар)")
    add(f"  {'-' * 34}")
    add(f"  Net PnL            {gross - fees - funding:+10,.2f}")
    if gross > 0:
        add(f"  → Зардал нь бохир ашгийн {(fees + funding) / gross * 100:.0f}%-ийг иджээ")

    add("")
    add("─ СТРАТЕГИ ТУС БҮР ─────────────────────────────────────────────────")
    add(f"  {'стратеги':<26}{'n':>5}{'win%':>8}{'net$':>11}{'expect$':>10}")
    for row in sorted(_bucket_stats(trades, lambda t: t["strategy"]), key=lambda r: -r["net"]):
        add(f"  {row['key']:<26}{row['n']:>5}{row['win_rate']:>7.0f}%{row['net']:>11,.0f}{row['expectancy']:>10.2f}")

    add("")
    add("─ ОНООНЫ БҮЛЭГ (min_signal_score зөв эсэх) ──────────────────────────")
    edges = [(0, 16), (16, 20), (20, 24), (24, 999)]

    def score_bucket(trade):
        for lo, hi in edges:
            if lo <= trade["score"] < hi:
                return f"{lo}–{hi if hi < 999 else '∞'}"
        return "?"

    add(f"  {'оноо':<26}{'n':>5}{'win%':>8}{'net$':>11}{'expect$':>10}")
    for row in _bucket_stats(trades, score_bucket):
        add(f"  {row['key']:<26}{row['n']:>5}{row['win_rate']:>7.0f}%{row['net']:>11,.0f}{row['expectancy']:>10.2f}")

    add("")
    add("─ ГАРЦЫН ШАЛТГААН ──────────────────────────────────────────────────")
    add(f"  {'шалтгаан':<26}{'n':>5}{'win%':>8}{'net$':>11}{'expect$':>10}")
    for row in sorted(_bucket_stats(trades, lambda t: t["exit_reason"]), key=lambda r: -r["n"]):
        add(f"  {row['key']:<26}{row['n']:>5}{row['win_rate']:>7.0f}%{row['net']:>11,.0f}{row['expectancy']:>10.2f}")

    add("")
    add("─ ЗАХ ЗЭЭЛИЙН ГОРИМ ────────────────────────────────────────────────")
    add(f"  {'regime':<26}{'n':>5}{'win%':>8}{'net$':>11}{'expect$':>10}")
    for row in sorted(_bucket_stats(trades, lambda t: t["regime"]), key=lambda r: -r["net"]):
        add(f"  {row['key']:<26}{row['n']:>5}{row['win_rate']:>7.0f}%{row['net']:>11,.0f}{row['expectancy']:>10.2f}")

    if data:
        bh = _buy_and_hold(data, sim["from_ms"], sim["to_ms"])
        if bh is not None:
            add("")
            add(f"ЖИШИГ: BTC зүгээр барьсан бол {bh:+.2f}%   |   бот {total_return:+.2f}%")

    add("")
    add("Тэмдэглэл: лаан доторх замыг мэдэхгүй тул stop/trailing нь ашгийн")
    add("түвшнээс ӨМНӨ биелсэн гэж үзнэ (консерватив). Liquidation, захиалгын")
    add("хэсэгчилсэн биелэлт, exchange info-гийн тоймлолт загварчлаагүй.")
    return "\n".join(lines)


# ----------------------------------------------------------------
# CLI
# ----------------------------------------------------------------

def run(days=90, start_balance=None, symbols=None, exec_interval="15m", progress=True,
        data_url=None, disabled=None, halt_on_drawdown=True, offset_days=0):
    symbols = symbols or SYMBOLS_POOL
    data = load_history(symbols, days, exec_interval=exec_interval, progress=progress,
                        data_url=data_url, offset_days=offset_days)
    if not data:
        return None, "❌ Өгөгдөл татагдсангүй."
    if start_balance is None:
        try:
            start_balance = account.get_usdt_balance() or 10_000.0
        except Exception:
            start_balance = 10_000.0
    log.info(f"🧪 Симуляц эхэллээ: {len(data)} coin, ${start_balance:,.0f}")
    sim = run_portfolio_backtest(data, start_balance, progress=progress, disabled=disabled,
                                 halt_on_drawdown=halt_on_drawdown)
    if not sim:
        return None, "❌ Симуляц ажиллаагүй."
    return sim, format_report(sim, data)


TELEGRAM_CHUNK = 3900


def send_report_to_telegram(report):
    """Тайланг Telegram-ын 4096 тэмдэгтийн хязгаарт багтаан хэсэглэж илгээнэ.

    send_telegram нь parse_mode тавьдаггүй тул <pre> гэх мэт таг нь бичвэр
    хэвээрээ харагдана — зүгээр л цэвэр текстээр илгээнэ. Хэсэглэхгүй бол
    тайлангийн сүүл (жишиг харьцуулалт) таслагдана.
    """
    chunks, current = [], ""
    for line in report.split("\n"):
        if len(current) + len(line) + 1 > TELEGRAM_CHUNK:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    for n, chunk in enumerate(chunks, 1):
        header = f"🧪 BACKTEST {n}/{len(chunks)}\n" if len(chunks) > 1 else ""
        notifications.send_telegram(header + chunk)
    return len(chunks)


def run_sweep_cli(days=90, start_balance=None, symbols=None, exec_interval="15m",
                  data_url=None, disabled=None, halt_on_drawdown=False, progress=True,
                  offset_days=0):
    """Sweep-ийн өгөгдөл ачаалах + ажиллуулах + тайлагнах."""
    symbols = symbols or SYMBOLS_POOL
    data = load_history(symbols, days, exec_interval=exec_interval, progress=progress,
                        data_url=data_url, offset_days=offset_days)
    if not data:
        return None, "❌ Өгөгдөл татагдсангүй."
    if start_balance is None:
        try:
            start_balance = account.get_usdt_balance() or 10_000.0
        except Exception:
            start_balance = 10_000.0
    results = run_sweep(data, start_balance, disabled=disabled,
                        halt_on_drawdown=halt_on_drawdown, progress=progress)
    return results, format_sweep_report(results, data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ботын портфелийн backtest")
    parser.add_argument("--days", type=int, default=90, help="туршилтын хугацаа (өдөр)")
    parser.add_argument("--balance", type=float, default=None, help="эхлэх баланс (анхдагч: жинхэнэ данс)")
    parser.add_argument("--symbols", type=str, default=None, help="таслалаар тусгаарласан symbol-ууд")
    parser.add_argument("--exec-interval", type=str, default="15m",
                        help="гарц шийдэх лааны давтамж (15m/5m). 1h нь хамгийн бүдүүлэг.")
    parser.add_argument("--offset-days", type=int, default=0,
                        help="цонхны төгсгөлийг өнөөдрөөс хэдэн хоногоор ухраах "
                             "(ж: --days 91 --offset-days 91 = өмнөх улирал)")
    parser.add_argument("--sweep", action="store_true",
                        help="хэд хэдэн тохиргоог нэг ажиллагаагаар харьцуулна")
    parser.add_argument("--no-halt", action="store_true",
                        help="drawdown circuit breaker-ыг унтраан бүтэн хугацааг хэмжинэ")
    parser.add_argument("--disable", type=str, default=None,
                        help="оролцуулахгүй стратегиуд, таслалаар (ж: MACD_MOMENTUM,BREAKOUT)")
    parser.add_argument("--data-url", type=str, default=None,
                        help=f"түүхэн өгөгдлийн эндпойнт (анхдагч: {BACKTEST_DATA_URL})")
    parser.add_argument("--csv", type=str, default=None, help="арилжаа бүрийг CSV-д бичих зам")
    parser.add_argument("--telegram", action="store_true", help="тайланг Telegram руу илгээх")
    args = parser.parse_args(argv)
    if args.offset_days < 0:
        parser.error("--offset-days сөрөг байж болохгүй")

    setup_logging(STATE_DIR, STATE_DIR_IS_PERSISTENT)
    binance_client.sync_server_time()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    disabled = None
    if args.disable:
        disabled = [name.strip().upper() for name in args.disable.split(",") if name.strip()]
        unknown = [name for name in disabled if name not in STRATEGY_NAMES]
        if unknown:
            parser.error(f"Ийм стратеги алга: {', '.join(unknown)}. "
                         f"Боломжтой: {', '.join(STRATEGY_NAMES)}")

    if args.sweep:
        # Sweep нь breaker-ыг ҮРГЭЛЖ унтраана: хувилбарууд өөр өөр өдөр зогсвол
        # тус бүр нь өөр хугацаа хэмжиж, харьцуулалт утгагүй болно. Аль нь
        # хэзээ зогсох байсныг тайлан тус тусад нь хэлнэ.
        results, report = run_sweep_cli(
            days=args.days, start_balance=args.balance, symbols=symbols,
            exec_interval=args.exec_interval, data_url=args.data_url,
            disabled=disabled, halt_on_drawdown=False, offset_days=args.offset_days,
        )
        print(report)
        if results and args.telegram:
            send_report_to_telegram(report)
        return 0 if results else 1

    sim, report = run(days=args.days, start_balance=args.balance, symbols=symbols,
                      exec_interval=args.exec_interval, data_url=args.data_url,
                      disabled=disabled, halt_on_drawdown=not args.no_halt,
                      offset_days=args.offset_days)
    print(report)

    if sim and args.csv:
        pd.DataFrame(sim["trades"]).to_csv(args.csv, index=False)
        print(f"\n💾 {len(sim['trades'])} арилжаа → {args.csv}")
    if sim and args.telegram:
        send_report_to_telegram(report)
    return 0 if sim else 1


if __name__ == "__main__":
    sys.exit(main())
