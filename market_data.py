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


def get_order_book(symbol, limit=20):
    try:
        data = binance_client.send_public_request("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        bids = [[float(b[0]), float(b[1])] for b in data.get("bids", [])]
        asks = [[float(a[0]), float(a[1])] for a in data.get("asks", [])]
        return bids, asks
    except Exception as e:
        log.warning(f"⚠️ Order book error {symbol}: {e}")
        return [], []


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
