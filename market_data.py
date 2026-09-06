"""
market_data.py
Зах зээлийн өгөгдөл ба symbol-ийн нарийвчлал (klines, exchange info, тоймлолт).
"""
import pandas as pd
from settings import *
from state import state
import binance_client
import utils
from logging_setup import get_logger

log = get_logger(__name__)


def load_exchange_info():
    if state.symbol_info_cache:
        return
    data = binance_client.send_public_request("/fapi/v1/exchangeInfo")
    if not isinstance(data, dict):
        return
    for item in data.get("symbols", []):
        symbol = item.get("symbol")
        if not symbol:
            continue
        info = {
            "quantityPrecision": item.get("quantityPrecision", 3),
            "pricePrecision": item.get("pricePrecision", 2),
            "stepSize": None,
            "tickSize": None,
            "minQty": None,
            "minNotional": None
        }
        for f in item.get("filters", []):
            filter_type = f.get("filterType")
            if filter_type == "LOT_SIZE":
                info["stepSize"] = utils.safe_float(f.get("stepSize"))
                info["minQty"] = utils.safe_float(f.get("minQty"))
            elif filter_type == "PRICE_FILTER":
                info["tickSize"] = utils.safe_float(f.get("tickSize"))
            elif filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
                info["minNotional"] = utils.safe_float(f.get("notional", f.get("minNotional", 0)))
        state.symbol_info_cache[symbol] = info


def get_symbol_info(symbol):
    if symbol not in state.symbol_info_cache:
        load_exchange_info()
    return state.symbol_info_cache.get(symbol)


def decimals_from_step(step):
    if not step or step <= 0:
        return 8
    step_str = f"{step:.12f}".rstrip('0')
    if '.' in step_str:
        return len(step_str.split('.')[1])
    return 0


def round_quantity(symbol, quantity):
    info = get_symbol_info(symbol)
    if not info:
        log.warning(f"⚠️ {symbol}: no exchange info found — skipping")
        return None
    step = info.get("stepSize")
    if not step or step <= 0:
        return round(quantity, int(info.get("quantityPrecision", 3)))
    decimals = decimals_from_step(step)
    return utils.round_down(quantity, decimals)


def round_price(symbol, price):
    info = get_symbol_info(symbol)
    if not info:
        log.warning(f"⚠️ {symbol}: no exchange info found — skipping")
        return None
    tick = info.get("tickSize")
    if not tick or tick <= 0:
        return round(price, int(info.get("pricePrecision", 2)))
    decimals = decimals_from_step(tick)
    return utils.round_down(price, decimals)


def format_qty(symbol, quantity):
    """Fixed-point string for API params. str(float) can emit '1e-05' for small
    step sizes, which Binance rejects."""
    info = get_symbol_info(symbol)
    decimals = 8
    if info:
        step = info.get("stepSize")
        if step and step > 0:
            decimals = decimals_from_step(step)
        else:
            decimals = int(info.get("quantityPrecision", 3))
    return f"{float(quantity):.{decimals}f}"


def format_price(symbol, price):
    info = get_symbol_info(symbol)
    decimals = 8
    if info:
        tick = info.get("tickSize")
        if tick and tick > 0:
            decimals = decimals_from_step(tick)
        else:
            decimals = int(info.get("pricePrecision", 2))
    return f"{float(price):.{decimals}f}"


def get_klines(symbol, interval="1h", limit=200, drop_unclosed=True):
    # Binance returns the still-forming candle as the last element. Deriving
    # signals from a candle that is still moving causes repainting (a signal
    # shows mid-candle then vanishes) and makes live trading diverge from the
    # backtest. Fetch one extra bar and drop the last row so callers only ever
    # see closed candles.
    req_limit = min(limit + 1, 1500) if drop_unclosed else limit
    data = binance_client.send_public_request("/fapi/v1/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": req_limit
    })
    if not isinstance(data, list):
        raise ValueError(f"Kline error: {data}")
    columns = [
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
    ]
    df = pd.DataFrame(data, columns=columns)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    if drop_unclosed and len(df) > 1:
        df = df.iloc[:-1].reset_index(drop=True)
    return df


INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}


def _klines_page(symbol, interval, start_ms, end_ms, limit=1500):
    return binance_client.send_public_request("/fapi/v1/klines", {
        "symbol": symbol, "interval": interval,
        "startTime": int(start_ms), "endTime": int(end_ms), "limit": limit,
    })


def get_klines_range(symbol, interval, start_ms, end_ms, max_bars=100_000):
    """Түүхэн лааг хуудаслаж татна.

    /fapi/v1/klines нэг удаад 1500 мөр өгдөг тул backtest-д хэрэгтэй хэдэн
    мянган лааг хэсэгчлэн авч холбоно. Хаагдаагүй сүүлийн лааг ХАСНА —
    get_klines-тай ижил зарчим, эс тэгвээс backtest хөдөлж байгаа лаанаас
    signal гаргаж repainting хийнэ.
    """
    step = INTERVAL_MS.get(interval)
    if not step:
        raise ValueError(f"Тодорхойгүй interval: {interval}")

    rows = []
    cursor = int(start_ms)
    end_ms = int(end_ms)
    while cursor < end_ms and len(rows) < max_bars:
        page = _klines_page(symbol, interval, cursor, end_ms)
        if not isinstance(page, list):
            raise ValueError(f"Kline range error {symbol} {interval}: {page}")
        if not page:
            break
        rows.extend(page)
        last_open = int(page[-1][0])
        if last_open + step <= cursor:
            break
        cursor = last_open + step
        if len(page) < 1500:
            break

    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    columns = [
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore",
    ]
    df = pd.DataFrame(rows, columns=columns)
    df["time"] = df["time"].astype("int64")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    # Хаагдсан лаа л үлдээнэ
    now_ms = binance_client.current_timestamp_ms()
    df = df[df["time"] + step <= now_ms].reset_index(drop=True)
    return df[["time", "open", "high", "low", "close", "volume"]]


def get_funding_history(symbol, start_ms, end_ms):
    """8 цаг тутмын бодит funding хувь (time_ms, rate) жагсаалт.

    Backtest-д зайлшгүй: 6 позиц 5x-ээр барихад funding сард балансын ~2%
    иддэг бөгөөд энэ нь ашигтай/алдагдалтай эсэхийг шийдэх хэмжээний дүн.
    """
    out = []
    cursor = int(start_ms)
    end_ms = int(end_ms)
    while cursor < end_ms:
        page = binance_client.send_public_request("/fapi/v1/fundingRate", {
            "symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000,
        })
        if not isinstance(page, list) or not page:
            break
        for item in page:
            out.append((int(item["fundingTime"]), utils.safe_float(item.get("fundingRate"), 0.0)))
        last = int(page[-1]["fundingTime"])
        if last <= cursor:
            break
        cursor = last + 1
        if len(page) < 1000:
            break
    return sorted(set(out))


def find_strong_levels(df, lookback=100):
    """Сүүлийн `lookback` лааны swing доод/дээд түвшин.

    Өмнө нь энэ нь order book-ийн хамгийн том 20 захиалгаас авдаг байсан тул
    зөвхөн spread-ийн эргэн тойрны утга гардаг байв (жишээ нь ETH дээр
    2453.47 / 2454.18 буюу 0.03% зөрүү) — дэмжлэг/эсэргүүцэл гэж нэрлэх
    боломжгүй. Одоо үнийн түүхээс тооцно, нэмэлт API дуудлага ч шаардахгүй.
    """
    if df is None or len(df) < 2:
        return None, None
    window = df.iloc[-lookback:]
    support = float(window["low"].min())
    resistance = float(window["high"].max())
    if support <= 0 or resistance <= 0:
        return None, None
    return support, resistance


def get_funding_rate(symbol):
    if not FUNDING_ENABLED:
        return 0.0
    try:
        data = binance_client.send_public_request("/fapi/v1/premiumIndex", {"symbol": symbol})
        return utils.safe_float(data.get("lastFundingRate", 0))
    except Exception as e:
        log.warning(f"⚠️ Funding rate error {symbol}: {e}")
        return 0.0


def check_min_notional(symbol, price, quantity):
    info = get_symbol_info(symbol)
    if info and info.get("minNotional"):
        min_notional = utils.safe_float(info["minNotional"])
        if min_notional > 0 and price * quantity < min_notional:
            log.warning(f"⚠️ Notional {price*quantity:.2f} < minNotional {min_notional}")
            return False
    return True
