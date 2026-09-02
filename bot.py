import os
import sys
import hashlib
import hmac
import time
import requests
import pandas as pd
import numpy as np
import traceback
import math
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import urlencode

from telegram_format import format_block, format_section, money
from settings import *


# ==========================================================
# 📦 STATE PERSISTENCE (Шинэ)
# ==========================================================
STRATEGY_STATE_FILE = os.path.join(os.path.dirname(__file__), "strategy_state.json")
BACKTEST_FEE_RATE = 0.0004           # 0.04% taker fee (Binance futures)
BACKTEST_SLIPPAGE_RATE = 0.0005      # 0.05% slippage

# ==========================================================
# 🧠 STRATEGY SETTINGS
# ==========================================================

STRATEGY_NAMES = [
    "EMA_CROSSOVER",
    "MACD_MOMENTUM",
    "GRID_TRADING",
    "BOLLINGER_MEAN_REVERSION",
    "RSI_STRATEGY",
    "TREND_FOLLOWING"
]

strategy_stats = {
    strategy: {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "consecutive_losses": 0,
        "active": True,
        "paused_cycles": 0
    }
    for strategy in STRATEGY_NAMES
}


def load_strategy_state():
    """Persist adaptive strategy statistics across restarts."""
    global strategy_stats
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
            current = strategy_stats[strategy]
            for key in ("trades", "wins", "losses", "total_pnl", "consecutive_losses", "active", "paused_cycles"):
                if key in saved:
                    current[key] = saved[key]
    except Exception as e:
        print(f"⚠️ Strategy state load failed: {e}")


def save_strategy_state():
    """Atomically save strategy statistics."""
    try:
        path = Path(STRATEGY_STATE_FILE)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(strategy_stats, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"⚠️ Strategy state save failed: {e}")

# ==========================================================
# 💰 SESSION STATE
# ==========================================================

session_start_balance = 0.0
session_realized_pnl = 0.0
cycle_start_balance = 0.0
cycle_start_time = time.time()
last_cycle_balance = 0.0


# ==========================================================
# 💼 ACTIVE POSITIONS
# ==========================================================

active_trade_info = {}


# ==========================================================
# 🧮 DCA STATE
# ==========================================================

dca_info = {}  # {symbol: {"level": 0, "avg_price": entry_price, "base_qty": qty}}


# ==========================================================
# ⚙️ CACHE
# ==========================================================

leverage_cache = {}
_symbol_info_cache = {}
last_telegram_report_time = 0
server_time_offset_ms = 0
position_mode_cache = None
safety_lock = False          # Глобал түгжээ (зөвхөн онцгой тохиолдолд)
unprotected_symbols = set()  # Хамгаалалтгүй үлдсэн симболууд


# ==========================================================
# 🧰 GENERIC HELPERS
# ==========================================================

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))

def round_down(value, decimals):
    factor = 10 ** decimals
    return math.floor(value * factor + 1e-12) / factor

def is_api_error(data):
    if not isinstance(data, dict):
        return False
    try:
        code = int(data.get("code", 0))
        return code < 0
    except Exception:
        return False

def api_error_text(data):
    if isinstance(data, dict):
        return str(data)
    return repr(data)


# ==========================================================
# ⏱️ SERVER TIME
# ==========================================================

def sync_server_time():
    global server_time_offset_ms
    try:
        local_before = int(time.time() * 1000)
        response = requests.get(f"{BASE_URL}/fapi/v1/time", timeout=REQUEST_TIMEOUT)
        local_after = int(time.time() * 1000)
        data = response.json()
        server_time = int(data.get("serverTime", local_after))
        local_mid = (local_before + local_after) // 2
        server_time_offset_ms = server_time - local_mid
        print(f"🕐 Server time offset: {server_time_offset_ms} ms")
        return True
    except Exception as e:
        print(f"⚠️ Server time sync failed: {e}")
        return False

def current_timestamp_ms():
    return int(time.time() * 1000) + server_time_offset_ms


# ==========================================================
# 📱 TELEGRAM (parse_mode нэмсэн)
# ==========================================================

def send_telegram(text, pin=False):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"{TELEGRAM_API_ROOT}/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"   # <-- ЗАССАН
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print("❌ Telegram error:", response.text)
            return False
        result = response.json()
        if pin and result.get("ok"):
            message_id = result["result"]["message_id"]
            pin_url = f"{TELEGRAM_API_ROOT}/bot{BOT_TOKEN}/pinChatMessage"
            requests.post(pin_url, json={"chat_id": CHAT_ID, "message_id": message_id}, timeout=10)
        return True
    except Exception as e:
        print(f"❌ Telegram exception: {e}")
        return False


# ==========================================================
# 🔐 SIGNATURE
# ==========================================================

def get_signature(params_str, secret):
    return hmac.new(secret.encode("utf-8"), params_str.encode("utf-8"), hashlib.sha256).hexdigest()


def send_signed_request(method, endpoint, params=None, retry_on_time_error=True):
    if params is None:
        params = {}
    params = params.copy()
    params["timestamp"] = current_timestamp_ms()
    params["recvWindow"] = 5000
    params = {k: v for k, v in params.items() if v is not None}
    query_str = urlencode(sorted(params.items()), doseq=True)
    signature = get_signature(query_str, API_SECRET)
    url = f"{BASE_URL}{endpoint}?{query_str}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        method = method.upper()
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method == "POST":
            response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method == "DELETE":
            response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        try:
            data = response.json()
        except Exception:
            data = {"code": response.status_code, "msg": response.text}
        if retry_on_time_error and isinstance(data, dict) and safe_float(data.get("code"), 0) == -1021:
            print("⚠️ Timestamp error. Resyncing server time...")
            sync_server_time()
            return send_signed_request(method, endpoint, params, retry_on_time_error=False)
        if response.status_code >= 400:
            print(f"❌ HTTP {response.status_code} {endpoint}: {data}")
        return data
    except Exception as e:
        print(f"❌ API error {endpoint}: {e}")
        return {"code": -9999, "msg": str(e)}


def send_public_request(endpoint, params=None):
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=REQUEST_TIMEOUT)
        return response.json()
    except Exception as e:
        print(f"❌ Public API error {endpoint}: {e}")
        return {"code": -9999, "msg": str(e)}


# ==========================================================
# 📊 EXCHANGE INFO
# ==========================================================

def load_exchange_info():
    if _symbol_info_cache:
        return
    data = send_public_request("/fapi/v1/exchangeInfo")
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
                info["stepSize"] = safe_float(f.get("stepSize"))
                info["minQty"] = safe_float(f.get("minQty"))
            elif filter_type == "PRICE_FILTER":
                info["tickSize"] = safe_float(f.get("tickSize"))
            elif filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
                info["minNotional"] = safe_float(f.get("notional", f.get("minNotional", 0)))
        _symbol_info_cache[symbol] = info

def get_symbol_info(symbol):
    if symbol not in _symbol_info_cache:
        load_exchange_info()
    return _symbol_info_cache.get(symbol)

def decimals_from_step(step):
    if not step or step <= 0:
        return 8
    decimals = 0
    while decimals < 12:
        value = round(step * (10 ** decimals))
        if abs(value - step * (10 ** decimals)) < 1e-8:
            return decimals
        decimals += 1
    return 8

def round_quantity(symbol, quantity):
    info = get_symbol_info(symbol)
    if not info:
        # Тодорхойгүй нарийвчлалыг таамаглахгүй — арилжаа хийхгүй байх нь илүү аюулгүй
        print(f"⚠️ {symbol}: no exchange info found (delisted/renamed?) — skipping")
        return None
    step = info.get("stepSize")
    if not step or step <= 0:
        return round(quantity, int(info.get("quantityPrecision", 3)))
    decimals = decimals_from_step(step)
    return round_down(quantity, decimals)

def round_price(symbol, price):
    info = get_symbol_info(symbol)
    if not info:
        print(f"⚠️ {symbol}: no exchange info found (delisted/renamed?) — skipping")
        return None
    tick = info.get("tickSize")
    if not tick or tick <= 0:
        return round(price, int(info.get("pricePrecision", 2)))
    decimals = decimals_from_step(tick)
    return round_down(price, decimals)


# ==========================================================
# 💰 ACCOUNT
# ==========================================================

def get_usdt_balance():
    data = send_signed_request("GET", "/fapi/v3/balance")
    if not isinstance(data, list):
        return 0.0
    for item in data:
        if item.get("asset") == "USDT":
            return safe_float(item.get("balance"))
    return 0.0

def get_position_mode():
    global position_mode_cache
    if position_mode_cache is not None:
        return position_mode_cache
    data = send_signed_request("GET", "/fapi/v1/positionSide/dual")
    if is_api_error(data):
        raise RuntimeError(f"Cannot get position mode: {data}")
    position_mode_cache = bool(data.get("dualSidePosition", False))
    print("📌 Position mode:", "HEDGE" if position_mode_cache else "ONE-WAY")
    return position_mode_cache


# ==========================================================
# 📌 POSITIONS
# ==========================================================

def get_positions():
    data = send_signed_request("GET", "/fapi/v2/positionRisk")
    positions = []
    if not isinstance(data, list):
        return positions
    for pos in data:
        amount = safe_float(pos.get("positionAmt"))
        if abs(amount) <= 0:
            continue
        positions.append({
            "symbol": pos.get("symbol"),
            "positionAmt": amount,
            "entryPrice": safe_float(pos.get("entryPrice")),
            "markPrice": safe_float(pos.get("markPrice")),
            "unRealizedProfit": safe_float(pos.get("unRealizedProfit")),
            "positionSide": pos.get("positionSide", "BOTH")
        })
    return positions

def get_total_unrealized():
    positions = get_positions()
    return sum(p["unRealizedProfit"] for p in positions)


# ==========================================================
# 🛒 ORDER FUNCTIONS (clientOrderId нэмсэн)
# ==========================================================

def place_market_order(symbol, side, quantity, reduce_only=False, position_side=None, client_order_id=None):
    if client_order_id is None:
        client_order_id = f"bot_{int(time.time()*1000)}_{symbol[:4]}"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": str(quantity),
        "newOrderRespType": "RESULT",
        "newClientOrderId": client_order_id
    }
    hedge_mode = get_position_mode()
    if hedge_mode:
        if position_side:
            params["positionSide"] = position_side
    else:
        if reduce_only:
            params["reduceOnly"] = "true"
    return send_signed_request("POST", "/fapi/v1/order", params)

def place_algo_order(params):
    # Binance USDS-M Futures-д тусдаа "algo order" endpoint байхгүй.
    # STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET бүгд
    # ердийн /fapi/v1/order endpoint-оор явна.
    params = params.copy()
    hedge_mode = get_position_mode()
    if hedge_mode:
        params.pop("reduceOnly", None)
    return send_signed_request("POST", "/fapi/v1/order", params)

def place_trailing_stop_order(symbol, side, quantity, callback_rate, activation_price=None, position_side=None):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": str(quantity),
        "callbackRate": str(callback_rate),
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT"
    }
    hedge_mode = get_position_mode()
    if hedge_mode:
        if position_side:
            params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    if activation_price is not None:
        params["activatePrice"] = str(activation_price)
    return place_algo_order(params)

def place_stop_loss_order(symbol, side, quantity, stop_price, position_side=None):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "quantity": str(quantity),
        "stopPrice": str(stop_price),
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT"
    }
    hedge_mode = get_position_mode()
    if hedge_mode:
        if position_side:
            params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    return place_algo_order(params)

def place_take_profit_order(symbol, side, quantity, tp_price, position_side=None):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "quantity": str(quantity),
        "stopPrice": str(tp_price),
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT"
    }
    hedge_mode = get_position_mode()
    if hedge_mode:
        if position_side:
            params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    return place_algo_order(params)

def cancel_all_orders(symbol):
    return send_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

def cancel_all_algo_orders(symbol):
    # STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET захиалгууд ердийн
    # order-той адил байрладаг тул тусдаа "algo" cancel endpoint байхгүй.
    # /fapi/v1/allOpenOrders нь тэдгээрийг бас цуцална.
    return cancel_all_orders(symbol)

def cancel_all_symbol_orders(symbol):
    normal = cancel_all_orders(symbol)
    return {"normal": normal, "algo": normal}


# ==========================================================
# 📈 MARKET DATA
# ==========================================================

def get_klines(symbol, interval="1h", limit=200):
    data = send_public_request("/fapi/v1/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
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
    return df


# ==========================================================
# 📊 INDICATORS
# ==========================================================

def calculate_ema(df, period):
    return df["close"].ewm(span=period, adjust=False).mean()

def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)

def calculate_atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calculate_adx(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr)
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    return dx.ewm(alpha=1/period, adjust=False).mean().fillna(0)

def calculate_macd(df):
    ema12 = calculate_ema(df, 12)
    ema26 = calculate_ema(df, 26)
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    histogram = macd - signal
    return macd, signal, histogram

def calculate_bollinger(df, period=20, std_dev=2):
    middle = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = middle + std * std_dev
    lower = middle - std * std_dev
    return upper, middle, lower

def calculate_volume_ratio(df, period=20):
    avg_volume = df["volume"].rolling(period).mean()
    current_volume = df["volume"].iloc[-1]
    average = avg_volume.iloc[-1]
    if average <= 0:
        return 1.0
    return current_volume / average


# ==========================================================
# 🧠 REGIME
# ==========================================================

def determine_regime(adx, ema_slope, atr_pct):
    if adx >= 30 and abs(ema_slope) >= 0.5:
        return "STRONG_TREND"
    if adx >= 25:
        return "TRENDING"
    if adx <= 18:
        if atr_pct >= 0.5:
            return "VOLATILE_RANGE"
        return "RANGE"
    return "TRANSITION"


# ==========================================================
# 🎯 SIGNAL GENERATION
# ==========================================================

def generate_strategy_signal(strategy, df, sentiment, regime):
    close = df["close"].iloc[-1]
    ema20 = calculate_ema(df, 20)
    ema50 = calculate_ema(df, 50)
    ema200 = calculate_ema(df, 200)
    rsi_series = calculate_rsi(df)
    rsi = rsi_series.iloc[-1]
    macd, macd_signal, histogram = calculate_macd(df)
    upper, middle, lower = calculate_bollinger(df)
    adx = calculate_adx(df).iloc[-1]
    ema_slope = (ema50.iloc[-1] - ema50.iloc[-5]) / ema50.iloc[-5] * 100

    if strategy == "EMA_CROSSOVER":
        bullish = (ema20.iloc[-1] > ema50.iloc[-1] and ema20.iloc[-2] <= ema50.iloc[-2])
        bearish = (ema20.iloc[-1] < ema50.iloc[-1] and ema20.iloc[-2] >= ema50.iloc[-2])
        if bullish and sentiment >= -0.4: return "BUY"
        if bearish and sentiment <= 0.4: return "SELL"

    elif strategy == "MACD_MOMENTUM":
        if histogram.iloc[-1] > 0 and histogram.iloc[-1] > histogram.iloc[-2] and rsi < 70: return "BUY"
        if histogram.iloc[-1] < 0 and histogram.iloc[-1] < histogram.iloc[-2] and rsi > 30: return "SELL"

    elif strategy == "GRID_TRADING":
        if regime in ["RANGE", "VOLATILE_RANGE"] and close <= lower.iloc[-1] and rsi < 40: return "BUY"
        if regime in ["RANGE", "VOLATILE_RANGE"] and close >= upper.iloc[-1] and rsi > 60: return "SELL"

    elif strategy == "BOLLINGER_MEAN_REVERSION":
        if regime in ["RANGE", "VOLATILE_RANGE"] and close < lower.iloc[-1] and rsi < 35: return "BUY"
        if regime in ["RANGE", "VOLATILE_RANGE"] and close > upper.iloc[-1] and rsi > 65: return "SELL"

    elif strategy == "RSI_STRATEGY":
        if rsi < 30 and sentiment > -0.6: return "BUY"
        if rsi > 70 and sentiment < 0.6: return "SELL"

    elif strategy == "TREND_FOLLOWING":
        if adx > 30 and ema20.iloc[-1] > ema50.iloc[-1] > ema200.iloc[-1] and ema_slope > 0.5 and sentiment >= -0.3:
            return "BUY"
        if adx > 30 and ema20.iloc[-1] < ema50.iloc[-1] < ema200.iloc[-1] and ema_slope < -0.5 and sentiment <= 0.3:
            return "SELL"
    return "HOLD"


# ==========================================================
# 📊 SCORE
# ==========================================================

def calculate_strategy_score(strategy, adx, rsi, atr_pct, volume_ratio, ema_slope, sentiment, regime):
    score = 0.0
    if strategy == "EMA_CROSSOVER":
        if regime in ["TRENDING", "STRONG_TREND"]: score += 7
        score += min(adx, 40) * 0.35 + abs(ema_slope) * 2 + min(volume_ratio, 3) * 1.5 + sentiment * 2

    elif strategy == "MACD_MOMENTUM":
        if regime in ["TRENDING", "TRANSITION"]: score += 5
        score += max(0, 35 - abs(rsi - 50)) * 0.15 + min(adx, 35) * 0.25 + min(atr_pct, 5) * 2 + min(volume_ratio, 3)

    elif strategy == "GRID_TRADING":
        if regime in ["RANGE", "VOLATILE_RANGE"]: score += 8
        score += max(0, 20 - adx) * 0.4 + atr_pct * 3

    elif strategy == "BOLLINGER_MEAN_REVERSION":
        if regime in ["RANGE", "VOLATILE_RANGE"]: score += 7
        score += max(0, 20 - adx) * 0.35 + abs(rsi - 50) * 0.15 + atr_pct * 2

    elif strategy == "RSI_STRATEGY":
        if rsi <= 35 or rsi >= 65: score += 8
        score += abs(rsi - 50) * 0.4 + min(atr_pct, 5)

    elif strategy == "TREND_FOLLOWING":
        if regime == "STRONG_TREND": score += 10
        elif regime == "TRENDING": score += 7
        score += min(adx, 50) * 0.35 + abs(ema_slope) * 3 + min(volume_ratio, 3) + sentiment * 2

    return max(0, score)


# ==========================================================
# 📊 CORRELATION
# ==========================================================

def calculate_correlation(symbol1, symbol2, lookback=50):
    """Pearson correlation between two symbols."""
    try:
        df1 = get_klines(symbol1, interval="1h", limit=lookback + 10)
        df2 = get_klines(symbol2, interval="1h", limit=lookback + 10)
        if len(df1) < lookback or len(df2) < lookback:
            return 0.0
        close1 = df1["close"].iloc[-lookback:]
        close2 = df2["close"].iloc[-lookback:]
        returns1 = close1.pct_change().dropna()
        returns2 = close2.pct_change().dropna()
        if len(returns1) < 10 or len(returns2) < 10:
            return 0.0
        valid_idx = returns1.index.intersection(returns2.index)
        if len(valid_idx) < 10:
            return 0.0
        corr = returns1.loc[valid_idx].corr(returns2.loc[valid_idx])
        return corr if not np.isnan(corr) else 0.0
    except Exception as e:
        print(f"⚠️ Correlation error {symbol1}-{symbol2}: {e}")
        return 0.0


# ==========================================================
# 🛡️ MIN NOTIONAL CHECK (Шинэ)
# ==========================================================

def check_min_notional(symbol, price, quantity):
    info = get_symbol_info(symbol)
    if info and info.get("minNotional"):
        min_notional = safe_float(info["minNotional"])
        if min_notional > 0 and price * quantity < min_notional:
            print(f"⚠️ Notional {price*quantity:.2f} < minNotional {min_notional}")
            return False
    return True


# ==========================================================
# 🔍 ANALYZE COIN (Корреляци шалгалттай)
# ==========================================================

def analyze_coin(symbol, check_correlation=True, active_symbols=None):
    try:
        df = get_klines(symbol, "1h", 200)
        if len(df) < 100:
            return None

        if CORRELATION_ENABLED and check_correlation and active_symbols:
            for sym in active_symbols:
                if sym == symbol:
                    continue
                corr = calculate_correlation(symbol, sym, CORRELATION_LOOKBACK)
                if abs(corr) > CORRELATION_THRESHOLD:
                    print(f"🔴 SKIPPED {symbol}: Correlation with {sym} = {corr:.2f}")
                    return None

        close = df["close"].iloc[-1]
        adx = calculate_adx(df).iloc[-1]
        rsi = calculate_rsi(df).iloc[-1]
        atr = calculate_atr(df).iloc[-1]
        atr_pct = atr / close * 100
        ema20 = calculate_ema(df, 20)
        ema50 = calculate_ema(df, 50)
        ema200 = calculate_ema(df, 200)
        ema_slope = (ema50.iloc[-1] - ema50.iloc[-5]) / ema50.iloc[-5] * 100
        volume_ratio = calculate_volume_ratio(df)
        sentiment = 0.0
        regime = determine_regime(adx, ema_slope, atr_pct)

        strategy_results = {}
        for strategy in STRATEGY_NAMES:
            if not strategy_stats[strategy]["active"]:
                continue
            score = calculate_strategy_score(strategy, adx, rsi, atr_pct, volume_ratio, ema_slope, sentiment, regime)
            signal = generate_strategy_signal(strategy, df, sentiment, regime)
            if signal == "BUY" and strategy == "TREND_FOLLOWING" and ema20.iloc[-1] < ema50.iloc[-1]:
                signal = "HOLD"
            if signal == "SELL" and strategy == "TREND_FOLLOWING" and ema20.iloc[-1] > ema50.iloc[-1]:
                signal = "HOLD"
            if score < MIN_SIGNAL_SCORE:
                signal = "HOLD"
            strategy_results[strategy] = {
                "strategy": strategy,
                "symbol": symbol,
                "price": close,
                "score": score,
                "signal": signal,
                "adx": adx,
                "rsi": rsi,
                "atr_pct": atr_pct,
                "volume_ratio": volume_ratio,
                "ema_slope": ema_slope,
                "regime": regime,
                "sentiment": sentiment
            }
        return {
            "symbol": symbol,
            "price": close,
            "adx": adx,
            "rsi": rsi,
            "atr_pct": atr_pct,
            "volume_ratio": volume_ratio,
            "ema_slope": ema_slope,
            "regime": regime,
            "strategies": strategy_results
        }
    except Exception as e:
        print(f"❌ analyze_coin {symbol}: {e}")
        return None


# ==========================================================
# 🏆 SCREENING (Корреляци шүүлтүүртэй, candidate хооронд шалгах)
# ==========================================================

def screen_coins():
    print("\n" + "=" * 70)
    print(f"🔍 MARKET SCREENING {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 70)

    # Эхлээд бүх койныг шинжлэх (одоо байгаа позицтой корреляци шалгах)
    current_positions = get_positions()
    active_symbols = {p["symbol"] for p in current_positions}
    analyses = []
    for symbol in SYMBOLS_POOL:
        result = analyze_coin(symbol, check_correlation=True, active_symbols=active_symbols)
        if result:
            analyses.append(result)

    # Strategy тус бүрээр хамгийн сайн candidate сонгох
    strategy_candidates = []
    for strategy in STRATEGY_NAMES:
        if not strategy_stats[strategy]["active"]:
            continue
        candidates = []
        for coin in analyses:
            result = coin["strategies"].get(strategy)
            if not result:
                continue
            if result["signal"] not in ["BUY", "SELL"]:
                continue
            if result["score"] < MIN_SIGNAL_SCORE:
                continue
            candidates.append(result)
        if not candidates:
            continue
        candidates.sort(key=lambda x: x["score"], reverse=True)
        best = candidates[0]
        strategy_candidates.append(best)
        print(f"🎯 {strategy:<30} → {best['symbol']:<10} {best['signal']:<4} Score={best['score']:.2f}")

    # Давхардсан symbol-ыг шийдвэрлэх
    by_symbol = defaultdict(list)
    for candidate in strategy_candidates:
        by_symbol[candidate["symbol"]].append(candidate)
    unique_candidates = []
    for symbol, candidates in by_symbol.items():
        winner = max(candidates, key=lambda x: x["score"])
        unique_candidates.append(winner)
        if len(candidates) > 1:
            print(f"🔄 DUPLICATE {symbol}: WINNER {winner['strategy']}")

    # Одоо candidate-ууд хоорондоо корреляци шалгах
    if CORRELATION_ENABLED:
        final_selected = []
        for i, coin in enumerate(unique_candidates):
            ok = True
            for j in range(i):
                if abs(calculate_correlation(coin["symbol"], unique_candidates[j]["symbol"], CORRELATION_LOOKBACK)) > CORRELATION_THRESHOLD:
                    ok = False
                    print(f"🔴 REMOVED {coin['symbol']}: high correlation with {unique_candidates[j]['symbol']}")
                    break
            if ok:
                final_selected.append(coin)
            if len(final_selected) >= MAX_SELECTIONS:
                break
        selected = final_selected[:MAX_SELECTIONS]
    else:
        selected = unique_candidates[:MAX_SELECTIONS]

    print("\n🏆 FINAL SELECTION:")
    for i, coin in enumerate(selected, 1):
        print(f"{i}. {coin['symbol']} | {coin['strategy']} | {coin['signal']} | Score={coin['score']:.2f}")
    return selected


# ==========================================================
# 📱 SELECTION REPORT
# ==========================================================

def send_selection_report(selected):
    if not selected:
        send_telegram("⚠️ *SIGNAL ОЛДСОНГҮЙ*\n━━━━━━━━━━━━━━━━━\nЭнэ cycle-д trade нээхгүй.")
        return
    sections = []
    for i, coin in enumerate(selected, 1):
        sections.append((
            f"{i}. {coin['symbol']}",
            [
                ("Strategy", coin["strategy"]),
                ("Signal", coin["signal"]),
                ("Score", f"{coin['score']:.2f}"),
                ("ADX / RSI", f"{coin['adx']:.1f} / {coin['rsi']:.1f}"),
                ("Regime", coin["regime"]),
            ]
        ))
    send_telegram(format_section("ШИНЭ TOP SIGNALS", "🏆", sections), pin=True)


# ==========================================================
# 🧪 BACKTESTING (fee/slippage моделтой)
# ==========================================================

def run_backtest(symbol, strategy, days=30, interval="1h"):
    """Directional backtest with next-bar execution, fees, slippage and drawdown."""
    print(f"\n🧪 Backtesting {strategy} on {symbol} for {days} days ({interval})")
    try:
        limit = days * 24 if interval == "1h" else days * 24 * 4 if interval == "15m" else days * 6
        df = get_klines(symbol, interval=interval, limit=min(limit, 1500))
        if len(df) < 220:
            return f"⚠️ {symbol}: Хангалттай өгөгдөл байхгүй ({len(df)} лаа)"

        trades = []
        position = None
        equity = 0.0
        peak_equity = 0.0
        max_dd = 0.0

        for i in range(200, len(df) - 1):
            window = df.iloc[:i + 1]
            close = float(window["close"].iloc[-1])
            adx = float(calculate_adx(window).iloc[-1])
            rsi = float(calculate_rsi(window).iloc[-1])
            atr = float(calculate_atr(window).iloc[-1])
            atr_pct = atr / close * 100 if close else 0.0
            ema50 = calculate_ema(window, 50)
            ema_slope = ((ema50.iloc[-1] - ema50.iloc[-5]) / ema50.iloc[-5] * 100) if ema50.iloc[-5] else 0.0
            volume_ratio = calculate_volume_ratio(window)
            sentiment = 0.0
            regime = determine_regime(adx, ema_slope, atr_pct)
            signal = generate_strategy_signal(strategy, window, sentiment, regime)

            next_open = float(df["open"].iloc[i + 1])
            fee = BACKTEST_FEE_RATE
            slip = BACKTEST_SLIPPAGE_RATE

            if position is None and signal in ("BUY", "SELL"):
                entry = next_open * (1 + slip if signal == "BUY" else 1 - slip)
                position = {"side": "LONG" if signal == "BUY" else "SHORT", "entry": entry, "entry_i": i + 1}
                continue

            if position is not None:
                reverse_signal = (position["side"] == "LONG" and signal == "SELL") or (position["side"] == "SHORT" and signal == "BUY")
                if reverse_signal:
                    exit_price = next_open * (1 - slip if position["side"] == "LONG" else 1 + slip)
                    if position["side"] == "LONG":
                        gross_pct = (exit_price - position["entry"]) / position["entry"] * 100
                    else:
                        gross_pct = (position["entry"] - exit_price) / position["entry"] * 100
                    net_pct = gross_pct - fee * 2 * 100
                    trades.append({
                        "entry_time": int(df.iloc[position["entry_i"]]["time"]),
                        "exit_time": int(df.iloc[i + 1]["time"]),
                        "side": position["side"],
                        "entry_price": position["entry"],
                        "exit_price": exit_price,
                        "gross_pct": gross_pct,
                        "net_pct": net_pct,
                    })
                    equity += net_pct
                    peak_equity = max(peak_equity, equity)
                    max_dd = max(max_dd, peak_equity - equity)
                    position = None

        if position is not None:
            exit_price = float(df["close"].iloc[-1]) * (1 - BACKTEST_SLIPPAGE_RATE if position["side"] == "LONG" else 1 + BACKTEST_SLIPPAGE_RATE)
            if position["side"] == "LONG":
                gross_pct = (exit_price - position["entry"]) / position["entry"] * 100
            else:
                gross_pct = (position["entry"] - exit_price) / position["entry"] * 100
            net_pct = gross_pct - BACKTEST_FEE_RATE * 2 * 100
            trades.append({
                "entry_time": int(df.iloc[position["entry_i"]]["time"]),
                "exit_time": int(df.iloc[-1]["time"]),
                "side": position["side"],
                "entry_price": position["entry"],
                "exit_price": exit_price,
                "gross_pct": gross_pct,
                "net_pct": net_pct,
            })
            equity += net_pct
            peak_equity = max(peak_equity, equity)
            max_dd = max(max_dd, peak_equity - equity)

        if not trades:
            return f"⚠️ {symbol} ({strategy}): Арилжаа гүйцэтгэгдээгүй"

        net = np.array([t["net_pct"] for t in trades], dtype=float)
        wins = net[net > 0]
        losses = net[net < 0]
        win_rate = float((net > 0).mean() * 100)
        profit_factor = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
        expectancy = float(net.mean())
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0

        return (
            f"🧪 BACKTEST REPORT\n"
            f"Symbol: {symbol}\n"
            f"Strategy: {strategy}\n"
            f"Period: сүүлийн {days} өдөр ({interval})\n"
            f"Trades: {len(trades)}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"Net PnL: {net.sum():+.2f}%\n"
            f"Avg Win: {avg_win:+.2f}%\n"
            f"Avg Loss: {avg_loss:+.2f}%\n"
            f"Profit Factor: {profit_factor:.2f}\n"
            f"Expectancy/Trade: {expectancy:+.3f}%\n"
            f"Max Drawdown: {max_dd:.2f}%\n"
            f"Fee model: {BACKTEST_FEE_RATE * 100:.03f}%/side\n"
            f"Slippage model: {BACKTEST_SLIPPAGE_RATE * 100:.03f}%/side\n"
            f"Note: энэ нь simulation; funding, liquidation болон exact exchange fills бүрэн моделдоогүй."
        )
    except Exception as e:
        return f"❌ Backtest error ({symbol}, {strategy}): {e}"


# ==========================================================
# 🔄 RECOVER EXISTING POSITIONS (хамгаалалт шалгах + сэргээх)
# ==========================================================

def sync_existing_positions():
    global dca_info, unprotected_symbols
    positions = get_positions()
    if not positions:
        return

    for pos in positions:
        symbol = pos["symbol"]
        if symbol in active_trade_info:
            continue
        amount = pos["positionAmt"]
        side = "BUY" if amount > 0 else "SELL"
        position_side = pos.get("positionSide", "BOTH")
        qty = abs(amount)
        entry = pos["entryPrice"]

        # Хамгаалалт байгаа эсэхийг шалгах (энгийн орд болон conditional
        # захиалгууд бүгд /fapi/v1/openOrders дотор ижил байрладаг)
        open_orders = send_signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol})
        has_protection = False
        if isinstance(open_orders, list):
            for order in open_orders:
                if order.get("symbol") == symbol and order.get("type") in ("TRAILING_STOP_MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"):
                    has_protection = True
                    break

        active_trade_info[symbol] = {
            "strategy": "RECOVERED",
            "side": side,
            "entry_price": entry,
            "quantity": qty,
            "position_side": position_side,
            "opened_at": time.time(),
            "opened_at_ms": int(time.time() * 1000),
            "entry_order_id": None,
            "sl_order_id": None,
            "tp_order_id": None,
            "recovered": True
        }

        dca_info[symbol] = {
            "level": 0,
            "avg_price": entry,
            "base_qty": qty,
            "total_qty": qty
        }

        if not has_protection:
            print(f"🔄 RECOVERED {symbol} WITHOUT protection – rebuilding...")
            success, _, _ = rebuild_protection_orders(symbol, side, qty, entry, position_side)
            if not success:
                unprotected_symbols.add(symbol)
                send_telegram(format_block("RECOVERED POSITION WITHOUT PROTECTION", "🚨", [("Symbol", symbol)]))
        else:
            print(f"🔄 RECOVERED POSITION: {symbol} (protected)")


# ==========================================================
# 💰 REALIZED PNL
# ==========================================================

def get_trade_realized_pnl(symbol, opened_at_ms):
    try:
        start_time = max(0, int(opened_at_ms) - 5000)
        trades = send_signed_request("GET", "/fapi/v1/userTrades", {
            "symbol": symbol,
            "startTime": start_time,
            "limit": PNL_LOOKBACK_LIMIT
        })
        if not isinstance(trades, list):
            return 0.0
        pnl = 0.0
        for trade in trades:
            trade_time = safe_float(trade.get("time"), 0)
            if trade_time < start_time:
                continue
            pnl += safe_float(trade.get("realizedPnl", 0))
        return pnl
    except Exception as e:
        print(f"❌ PnL error {symbol}: {e}")
        return 0.0


# ==========================================================
# 📈 STRATEGY PERFORMANCE
# ==========================================================

def update_strategy_performance(strategy, pnl):
    global session_realized_pnl
    if strategy not in strategy_stats:
        return
    stats = strategy_stats[strategy]
    stats["trades"] += 1
    stats["total_pnl"] += pnl
    session_realized_pnl += pnl
    if pnl > 0:
        stats["wins"] += 1
        stats["consecutive_losses"] = 0
    else:
        stats["losses"] += 1
        stats["consecutive_losses"] += 1
        if ADAPTIVE_STRATEGY and stats["consecutive_losses"] >= CONSECUTIVE_LOSS_LIMIT:
            stats["active"] = False
            stats["paused_cycles"] = STRATEGY_COOLDOWN_CYCLES
            send_telegram(
                format_block(
                    "STRATEGY PAUSED",
                    "⚠️",
                    [
                        ("Strategy", strategy),
                        ("Loss streak", stats["consecutive_losses"]),
                        ("Pause", f"{STRATEGY_COOLDOWN_CYCLES} cycles"),
                    ]
                )
            )
    save_strategy_state()

def finalize_trade(symbol, trade_data):
    global dca_info
    strategy = trade_data.get("strategy", "UNKNOWN")
    if strategy == "RECOVERED":
        if symbol in dca_info:
            del dca_info[symbol]
        return 0.0
    opened_at_ms = trade_data.get("opened_at_ms", int(trade_data.get("opened_at", time.time()) * 1000))
    pnl = get_trade_realized_pnl(symbol, opened_at_ms)
    update_strategy_performance(strategy, pnl)
    print(f"🔴 CLOSED {symbol} | Strategy={strategy} | PnL=${pnl:.2f}")
    send_telegram(
        format_block(
            "ПОЗИЦ ХААГДЛАА",
            "🟢" if pnl > 0 else "🔴",
            [
                ("Symbol", symbol),
                ("Strategy", strategy),
                ("PnL", money(pnl)),
            ]
        )
    )
    if symbol in dca_info:
        del dca_info[symbol]
    return pnl


# ==========================================================
# ⚙️ LEVERAGE (бодит хөшүүргийг авах)
# ==========================================================

def get_actual_leverage(symbol):
    """Get the current leverage for a symbol (cached)."""
    if symbol in leverage_cache:
        return leverage_cache[symbol]
    result = send_signed_request("GET", "/fapi/v1/leverage", {"symbol": symbol})
    if is_api_error(result):
        return LEVERAGE
    lev = int(result.get("leverage", LEVERAGE))
    leverage_cache[symbol] = lev
    return lev

def ensure_leverage(symbol, leverage=LEVERAGE):
    # Хэрэв аль хэдийн тохируулсан бол шалгах
    if leverage_cache.get(symbol) == leverage:
        return True
    result = send_signed_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol,
        "leverage": leverage
    })
    if is_api_error(result):
        # 4141 = leverage already set
        if safe_float(result.get("code"), 0) == -4141:
            leverage_cache[symbol] = leverage
            return True
        print(f"❌ {symbol}: leverage error {result}")
        return False
    leverage_cache[symbol] = leverage
    return True


# ==========================================================
# 🛡️ ACTIVATE PRICE
# ==========================================================

def calculate_trailing_activation(symbol, signal, entry_price):
    positions = get_positions()
    mark_price = entry_price
    for pos in positions:
        if pos["symbol"] == symbol:
            if pos["markPrice"] > 0:
                mark_price = pos["markPrice"]
            break
    if signal == "BUY":
        activation = max(entry_price * (1 + TRAILING_ACTIVATION_PCT / 100), mark_price * 1.001)
    else:
        activation = min(entry_price * (1 - TRAILING_ACTIVATION_PCT / 100), mark_price * 0.999)
    return round_price(symbol, activation)


# ==========================================================
# 🛡️ REBUILD PROTECTION ORDERS (цэвэр атомар)
# ==========================================================

def rebuild_protection_orders(symbol, side, quantity, entry_price, position_side):
    """Rebuild TP + trailing + fallback SL atomically from the bot's perspective.
    Returns (success: bool, tp_price, activation_price)."""
    close_side = "SELL" if side == "BUY" else "BUY"
    if quantity <= 0 or entry_price <= 0:
        return False, None, None

    if side == "BUY":
        tp_price = round_price(symbol, entry_price * (1 + TAKE_PROFIT_PCT / 100))
        emergency_sl_price = round_price(symbol, entry_price * (1 - EMERGENCY_SL_PCT / 100))
    else:
        tp_price = round_price(symbol, entry_price * (1 - TAKE_PROFIT_PCT / 100))
        emergency_sl_price = round_price(symbol, entry_price * (1 + EMERGENCY_SL_PCT / 100))

    if tp_price is None or emergency_sl_price is None:
        # Symbol info олдсонгүй — тодорхойгүй нарийвчлалаар order оруулахгүй
        return False, None, None

    activation_price = calculate_trailing_activation(symbol, side, entry_price)
    if activation_price is None:
        return False, tp_price, None

    # Cancel stale conditional orders
    cancel_all_algo_orders(symbol)
    trailing = place_trailing_stop_order(symbol, close_side, quantity, TRAILING_CALLBACK_RATE, activation_price, position_side)
    trailing_ok = not is_api_error(trailing)

    if trailing_ok:
        sl = None
    else:
        sl = place_stop_loss_order(symbol, close_side, quantity, emergency_sl_price, position_side)
        if is_api_error(sl):
            return False, tp_price, activation_price

    tp = place_take_profit_order(symbol, close_side, quantity, tp_price, position_side)
    if is_api_error(tp):
        # Do not leave the position without a hard stop. Keep the successful stop/trailing in place.
        return False, tp_price, activation_price

    return True, tp_price, activation_price


# ==========================================================
# 🚀 EXECUTE TRADES (rebuild_protection_orders ашиглах)
# ==========================================================

def execute_trades(selected_coins, total_balance):
    global safety_lock, dca_info, unprotected_symbols
    if safety_lock:
        print("🔒 SAFETY LOCK: new trades disabled")
        return
    if not selected_coins:
        return

    positions = get_positions()
    existing_symbols = {p["symbol"] for p in positions}
    current_margin_used = 0.0
    for pos in positions:
        actual_lev = get_actual_leverage(pos["symbol"])
        current_margin_used += abs(pos["positionAmt"]) * pos["entryPrice"] / actual_lev
    max_margin = total_balance * MAX_TOTAL_MARGIN_USAGE

    for coin in selected_coins:
        if safety_lock:
            return
        symbol = coin["symbol"]
        strategy = coin["strategy"]
        signal = coin["signal"]
        if signal not in ["BUY", "SELL"]:
            continue
        if symbol in existing_symbols:
            print(f"⏸️ {symbol}: already has position")
            continue
        if total_balance < MIN_BALANCE_USDT:
            send_telegram(format_block("БАЛАНС БАГА", "⚠️", [("Balance", f"${total_balance:.2f}")]))
            return

        margin = total_balance * TRADE_ALLOCATION
        # Хэрэв symbol нь unprotected гэж тэмдэглэгдсэн бол алгасах
        if symbol in unprotected_symbols:
            print(f"⏸️ {symbol}: unprotected, skip new trade")
            continue

        if current_margin_used + margin > max_margin:
            print(f"⏸️ {symbol}: portfolio margin limit")
            continue

        if not ensure_leverage(symbol, LEVERAGE):
            continue

        price = coin["price"]
        notional = margin * LEVERAGE
        raw_quantity = notional / price
        quantity = round_quantity(symbol, raw_quantity)
        if quantity is None:
            continue
        info = get_symbol_info(symbol)
        if info and info.get("minQty"):
            min_qty = safe_float(info.get("minQty"))
            if min_qty > 0 and quantity < min_qty:
                print(f"⏸️ {symbol}: quantity below minQty")
                continue
        if not check_min_notional(symbol, price, quantity):
            continue
        if quantity <= 0:
            continue

        try:
            cancel_all_symbol_orders(symbol)
        except Exception as e:
            print(f"⚠️ Order cleanup {symbol}: {e}")

        order_side = "BUY" if signal == "BUY" else "SELL"
        close_side = "SELL" if signal == "BUY" else "BUY"
        position_side = "LONG" if signal == "BUY" else "SHORT"
        print(f"\n🚀 OPEN {symbol}\nStrategy={strategy}\nSignal={signal}\nQty={quantity}")

        # Market order (clientOrderId ашигласан)
        order = place_market_order(symbol, order_side, quantity, reduce_only=False, position_side=position_side)
        if is_api_error(order):
            send_telegram(format_block("ORDER FAILED", "❌", [("Symbol", symbol), ("Error", str(order)[:300])]))
            continue

        time.sleep(0.5)
        current_positions = get_positions()
        actual_position = None
        for p in current_positions:
            if p["symbol"] == symbol:
                actual_position = p
                break

        if actual_position:
            entry_price = actual_position["entryPrice"]
            actual_quantity = abs(actual_position["positionAmt"])
            actual_position_side = actual_position.get("positionSide", "BOTH")
        else:
            entry_price = safe_float(order.get("avgPrice"), price)
            actual_quantity = quantity
            actual_position_side = "BOTH" if not get_position_mode() else position_side
        if entry_price <= 0:
            entry_price = price
        opened_at_ms = current_timestamp_ms()

        # DCA info эхлүүлэх
        dca_info[symbol] = {
            "level": 0,
            "avg_price": entry_price,
            "base_qty": actual_quantity,
            "total_qty": actual_quantity
        }

        # Хамгаалалтын захиалгуудыг байршуулах (нэгдсэн функц)
        success, tp_price, activation_price = rebuild_protection_orders(symbol, signal, actual_quantity, entry_price, actual_position_side)
        if not success:
            # Хамгаалалт бүтэлгүйтсэн → position-ийг хаах
            send_telegram(format_block("PROTECTION FAILED", "🚨", [("Symbol", symbol), ("Action", "Closing position")]))
            close_result = place_market_order(symbol, close_side, actual_quantity, reduce_only=True, position_side=actual_position_side)
            if is_api_error(close_result):
                safety_lock = True
                send_telegram(format_block("CRITICAL CLOSE FAILED", "🚨", [("Symbol", symbol)]))
                continue
            existing_symbols.discard(symbol)
            if symbol in dca_info:
                del dca_info[symbol]
            # Emergency close-ийн PnL бүртгэх
            pnl = get_trade_realized_pnl(symbol, opened_at_ms)
            update_strategy_performance(strategy, pnl)
            send_telegram(format_block("EMERGENCY CLOSED", "⚠️", [("Symbol", symbol), ("PnL", money(pnl))]))
            continue

        active_trade_info[symbol] = {
            "strategy": strategy,
            "side": signal,
            "entry_price": entry_price,
            "quantity": actual_quantity,
            "position_side": actual_position_side,
            "opened_at": time.time(),
            "opened_at_ms": opened_at_ms,
            "entry_order_id": order.get("orderId"),
            "sl_order_id": None,
            "tp_order_id": None,
            "recovered": False
        }
        existing_symbols.add(symbol)
        current_margin_used += margin

        send_telegram(
            format_block(
                "ШИНЭ ПОЗИЦ НЭЭГДЛЭЭ",
                "🚀",
                [
                    ("Symbol", symbol),
                    ("Signal", f"{signal} ({position_side})"),
                    ("Strategy", strategy),
                    ("", ""),
                    ("Entry", f"${entry_price:,.6f}"),
                    ("Qty", actual_quantity),
                    ("Margin", f"${margin:.2f} ({LEVERAGE}x)"),
                    ("", ""),
                    ("Take Profit", f"${tp_price:,.6f}"),
                    ("Trailing", f"{TRAILING_CALLBACK_RATE}% @ ${activation_price:,.6f}"),
                    ("", ""),
                    ("Score", f"{coin['score']:.2f}"),
                    ("ADX / RSI", f"{coin['adx']:.1f} / {coin['rsi']:.1f}"),
                    ("Regime", coin["regime"]),
                ]
            )
        )
        time.sleep(0.5)


# ==========================================================
# 📡 DCA MANAGEMENT (rebuild_protection ашиглах)
# ==========================================================

def manage_dca():
    if not DCA_ENABLED:
        return

    positions = get_positions()
    if not positions:
        return

    balance = get_usdt_balance()
    max_margin = balance * MAX_TOTAL_MARGIN_USAGE

    for pos in positions:
        symbol = pos["symbol"]
        current_price = safe_float(pos.get("markPrice"))
        amount = safe_float(pos.get("positionAmt"))
        if current_price <= 0 or amount == 0:
            continue
        if symbol not in dca_info:
            continue

        side = "BUY" if amount > 0 else "SELL"
        position_side = pos.get("positionSide", "BOTH")
        info = dca_info[symbol]
        avg_price = safe_float(info.get("avg_price"))
        current_level = int(info.get("level", 0))
        if avg_price <= 0 or current_level >= DCA_LEVELS:
            continue

        move_against = ((avg_price - current_price) / avg_price * 100) if side == "BUY" else ((current_price - avg_price) / avg_price * 100)
        trigger_pct = DCA_TRIGGER_PCT * (current_level + 1)
        if move_against < trigger_pct:
            continue

        base_qty = safe_float(info.get("base_qty"))
        add_qty = round_quantity(symbol, base_qty * DCA_MULTIPLIER)
        if add_qty is None or add_qty <= 0:
            continue

        # Portfolio margin guard
        current_margin_used = 0.0
        for p in positions:
            actual_lev = get_actual_leverage(p["symbol"])
            current_margin_used += abs(p["positionAmt"]) * p["entryPrice"] / actual_lev
        additional_margin = add_qty * current_price / get_actual_leverage(symbol)
        if current_margin_used + additional_margin > max_margin:
            print(f"⏸️ DCA blocked {symbol}: portfolio margin limit")
            continue

        # Order
        order = place_market_order(symbol, side, add_qty, reduce_only=False, position_side=position_side if position_side != "BOTH" else None)
        if is_api_error(order):
            send_telegram(format_block("DCA FAILED", "⚠️", [("Symbol", symbol), ("Error", str(order)[:300])]))
            continue

        time.sleep(0.5)
        refreshed = next((p for p in get_positions() if p["symbol"] == symbol), None)
        if not refreshed:
            send_telegram(format_block("DCA VERIFY FAILED", "🚨", [("Symbol", symbol), ("Status", "Skipping further DCA")]))
            continue

        actual_qty = abs(safe_float(refreshed.get("positionAmt")))
        fill_price = safe_float(order.get("avgPrice"), current_price)
        if fill_price <= 0:
            fill_price = current_price

        old_qty = safe_float(info.get("total_qty"), abs(amount))
        new_avg = ((old_qty * avg_price) + (add_qty * fill_price)) / (old_qty + add_qty)
        new_level = current_level + 1

        # Rebuild protection
        dca_success, _, _ = rebuild_protection_orders(symbol, side, actual_qty, new_avg, refreshed.get("positionSide", position_side))
        if not dca_success:
            send_telegram(format_block("DCA PROTECTION FAILED", "⚠️", [("Symbol", symbol), ("Status", "DCA executed without protection")]))
            # Do not stop bot, but mark symbol as unprotected to avoid further trades
            unprotected_symbols.add(symbol)
            continue

        info["level"] = new_level
        info["avg_price"] = new_avg
        info["total_qty"] = actual_qty

        send_telegram(
            format_block(
                f"DCA EXECUTED for {symbol}",
                "📊",
                [
                    ("Level", f"{new_level}/{DCA_LEVELS}"),
                    ("Added Qty", add_qty),
                    ("Side", side),
                    ("New Avg Price", f"${new_avg:.4f}"),
                    ("Total Qty", actual_qty),
                ]
            )
        )


# ==========================================================
# 🔒 CLOSE ONE POSITION
# ==========================================================

def close_one_position(pos):
    symbol = pos["symbol"]
    amount = safe_float(pos["positionAmt"])
    if abs(amount) <= 0:
        return True
    close_side = "SELL" if amount > 0 else "BUY"
    quantity = round_quantity(symbol, abs(amount))
    if quantity is None:
        # Position хаах ажиллагааг ХЭЗЭЭ Ч алгасахгүй — эсхүл symbol info
        # байхгүй байсан ч, Binance дээр аль хэдийн байгаа position хэмжээ
        # хүчинтэй тул яг тэр хэмжээгээрээ хаана.
        quantity = abs(amount)
        print(f"⚠️ {symbol}: no exchange info — closing with raw position size {quantity}")
    position_side = pos.get("positionSide", "BOTH")
    print(f"🔒 CLOSE {symbol} | {close_side} | {quantity} | PositionSide={position_side}")
    result = place_market_order(symbol, close_side, quantity, reduce_only=True, position_side=position_side)
    if is_api_error(result):
        print(f"❌ CLOSE FAILED {symbol}: {result}")
        return False
    print(f"✅ CLOSE ORDER SENT {symbol}")
    return True


# ==========================================================
# 🔒 CLOSE ALL + VERIFY
# ==========================================================

def close_all_positions_and_verify():
    print("\n" + "=" * 70)
    print("🔒 CLOSE ALL POSITIONS")
    print("=" * 70)
    positions = get_positions()
    if not positions:
        print("✅ No open positions.")
        return True
    symbols = {p["symbol"] for p in positions}

    for symbol in symbols:
        try:
            result = cancel_all_symbol_orders(symbol)
            print(f"🧹 Cancel {symbol}: {result}")
        except Exception as e:
            print(f"⚠️ Cancel error {symbol}: {e}")

    time.sleep(1)

    for pos in positions:
        close_one_position(pos)
        time.sleep(0.4)

    for attempt in range(1, CLOSE_VERIFY_ATTEMPTS + 1):
        time.sleep(CLOSE_VERIFY_DELAY_SEC)
        remaining = get_positions()
        if not remaining:
            print("✅ ALL POSITIONS CLOSED")
            for symbol in symbols:
                try:
                    cancel_all_symbol_orders(symbol)
                except Exception:
                    pass
            return True
        print(f"⏳ CLOSE VERIFY {attempt}/{CLOSE_VERIFY_ATTEMPTS} | Remaining={len(remaining)}")
        for pos in remaining:
            close_one_position(pos)
            time.sleep(0.4)

    remaining = get_positions()
    if remaining:
        print("🚨 POSITION CLOSE INCOMPLETE")
        return False
    return True


# ==========================================================
# 🎯 TARGET HANDLER
# ==========================================================

def handle_target_reached(total_unrealized):
    global safety_lock
    safety_lock = True
    balance_before = get_usdt_balance()
    send_telegram(
        format_block(
            "TARGET REACHED!",
            "🎯",
            [
                ("Unrealized PnL", money(total_unrealized)),
                ("Target", f"${TARGET_PROFIT:.2f}"),
                ("", ""),
                ("Статус", "Бүх позицыг хааж байна..."),
            ]
        )
    )
    success = close_all_positions_and_verify()
    if not success:
        send_telegram(
            format_block(
                "CRITICAL: CLOSE INCOMPLETE",
                "🚨",
                [
                    ("Статус", "Бүх позиц бүрэн хаагдсангүй"),
                    ("Дараагийн алхам", "SAFETY LOCK ACTIVE — шинэ trade нээхгүй"),
                ]
            )
        )
        return False

    time.sleep(2)
    balance_after = get_usdt_balance()
    balance_delta = balance_after - balance_before

    target_symbols = list(active_trade_info.keys())
    target_realized = 0.0
    for symbol in target_symbols:
        trade_data = active_trade_info.pop(symbol, None)
        if not trade_data:
            continue
        target_realized += finalize_trade(symbol, trade_data)

    final_positions = get_positions()
    if final_positions:
        safety_lock = True
        send_telegram(
            format_block(
                "FINAL SAFETY CHECK FAILED",
                "🚨",
                [("Статус", "Position үлдсэн — шинэ trade нээхгүй")]
            )
        )
        return False

    send_telegram(
        format_block(
            "TARGET REALIZED!",
            "✅",
            [
                ("Target Unrealized", money(total_unrealized)),
                ("Realized PnL", money(target_realized)),
                ("Balance change", money(balance_delta)),
                ("New Balance", f"${balance_after:,.2f}"),
                ("Open Positions", 0),
                ("", ""),
                ("Дараагийн алхам", "10 минут cooldown → автомат үргэлжлэл"),
            ]
        )
    )
    return True


# ==========================================================
# 😴 10 MIN COOLDOWN
# ==========================================================

def target_cooldown():
    print("\n😴 TARGET COOLDOWN")
    cooldown_end = time.time() + TARGET_COOLDOWN_SEC
    while True:
        remaining = cooldown_end - time.time()
        if remaining <= 0:
            break
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        print(f"\r😴 COOLDOWN {minutes:02d}:{seconds:02d}", end="", flush=True)
        time.sleep(5)
    print("\n")
    send_telegram(
        format_block(
            "10 МИНУТЫН COOLDOWN ДУУСЛАА",
            "🚀",
            [("Статус", "Бот дахин ажиллаж, шинэ screening эхэллээ")]
        )
    )


# ==========================================================
# 📡 MONITOR (DCA дуудах хэсэг нэмэгдсэн)
# ==========================================================

def monitor_positions():
    global last_telegram_report_time
    positions = get_positions()
    current_symbols = {p["symbol"] for p in positions}
    tracked_symbols = set(active_trade_info.keys())

    closed_symbols = tracked_symbols - current_symbols
    for symbol in closed_symbols:
        trade_data = active_trade_info.pop(symbol, None)
        if not trade_data:
            continue
        pnl = finalize_trade(symbol, trade_data)
        try:
            cancel_all_symbol_orders(symbol)
        except Exception:
            pass

    if not positions:
        return

    # ---- DCA MANAGEMENT ----
    manage_dca()

    now = time.time()
    if now - last_telegram_report_time < TELEGRAM_REPORT_INTERVAL_SEC:
        return

    sections = []
    total_unrealized = 0.0
    for pos in positions:
        symbol = pos["symbol"]
        pnl = pos["unRealizedProfit"]
        total_unrealized += pnl
        trade_data = active_trade_info.get(symbol, {})
        strategy = trade_data.get("strategy", "UNKNOWN")
        side = trade_data.get("side", "UNKNOWN")
        dca_level = dca_info.get(symbol, {}).get("level", 0)
        sections.append((
            f"🔹 {symbol} ({side}) [DCA: {dca_level}/{DCA_LEVELS}]",
            [
                ("Strategy", strategy),
                ("Entry", f"${pos['entryPrice']:,.6f}"),
                ("Mark", f"${pos['markPrice']:,.6f}"),
                ("PnL", money(pnl)),
                ("Qty", abs(pos["positionAmt"])),
            ]
        ))

    current_balance = get_usdt_balance()
    sections.append((
        "📊 НИЙТ",
        [
            ("Unrealized", money(total_unrealized)),
            ("Target", f"${TARGET_PROFIT:.2f}"),
            ("Balance", f"${current_balance:,.2f}"),
            ("Session Realized", money(session_realized_pnl)),
        ]
    ))

    send_telegram(format_section("ПОЗИЦЫН МОНИТОР", "📊", sections))
    last_telegram_report_time = now


# ==========================================================
# 🔄 STRATEGY COOLDOWN
# ==========================================================

def update_strategy_cooldowns():
    for strategy, stats in strategy_stats.items():
        if stats["paused_cycles"] <= 0:
            continue
        stats["paused_cycles"] -= 1
        if stats["paused_cycles"] <= 0:
            stats["active"] = True
            stats["consecutive_losses"] = 0
            save_strategy_state()
            send_telegram(
                format_block(
                    "STRATEGY REACTIVATED",
                    "🔄",
                    [("Strategy", strategy)]
                )
            )

def get_active_strategies():
    return [s for s, stats in strategy_stats.items() if stats["active"]]


# ==========================================================
# 📊 PERFORMANCE REPORT
# ==========================================================

def send_performance_report():
    if not STRATEGY_PERFORMANCE_TRACKING:
        return
    total_pnl = 0.0
    sections = []
    for strategy, stats in strategy_stats.items():
        if stats["trades"] == 0:
            continue
        win_rate = stats["wins"] / stats["trades"] * 100
        status = "🟢 ACTIVE" if stats["active"] else "🔴 PAUSED"
        pnl = stats["total_pnl"]
        total_pnl += pnl
        sections.append((
            f"🔹 {strategy} — {status}",
            [
                ("Trades", stats["trades"]),
                ("Win / Loss", f"{stats['wins']} / {stats['losses']}"),
                ("Win rate", f"{win_rate:.1f}%"),
                ("PnL", money(pnl)),
                ("Loss streak", stats["consecutive_losses"]),
            ]
        ))
    sections.append(("📊 НИЙТ", [("Total PnL", money(total_pnl))]))
    send_telegram(format_section("СТРАТЕГИЙН ГҮЙЦЭТГЭЛ", "📊", sections))


# ==========================================================
# 📆 CYCLE SUMMARY
# ==========================================================

def send_cycle_summary():
    global cycle_start_time, last_cycle_balance
    current_balance = get_usdt_balance()
    balance_change = current_balance - last_cycle_balance
    period = f"{datetime.fromtimestamp(cycle_start_time).strftime('%H:%M:%S')} → {datetime.now().strftime('%H:%M:%S')}"
    send_telegram(
        format_block(
            "6 ЦАГИЙН ЦИКЛ",
            "📆",
            [
                ("Хугацаа", period),
                ("Balance change", money(balance_change)),
                ("Balance", f"${current_balance:.2f}"),
                ("Active strategies", len(get_active_strategies())),
            ]
        ),
        pin=True
    )
    cycle_start_time = time.time()
    last_cycle_balance = current_balance


# ==========================================================
# 🛡️ SAFETY RECOVERY
# ==========================================================

def safety_recovery():
    global safety_lock
    if not safety_lock:
        return True
    positions = get_positions()
    if not positions:
        safety_lock = False
        send_telegram(
            format_block(
                "SAFETY LOCK CLEARED",
                "✅",
                [("Статус", "Position = 0 — trading үргэлжлэхэд бэлэн")]
            )
        )
        return True
    send_telegram(
        format_block(
            "SAFETY LOCK",
            "🔒",
            [("Статус", "Position үлдсэн — хаалтыг дахин оролдож байна")]
        )
    )
    success = close_all_positions_and_verify()
    if success:
        safety_lock = False
        active_trade_info.clear()
        send_telegram(
            format_block(
                "SAFETY RECOVERY SUCCESS",
                "✅",
                [("Статус", "Бүх позиц хаагдлаа — trading үргэлжилнэ")]
            )
        )
        return True
    return False


# ==========================================================
# 🚀 MAIN
# ==========================================================

def main():
    global session_start_balance, cycle_start_balance, last_cycle_balance, cycle_start_time, safety_lock

    print("=" * 70)
    print("🤖 SMART BOT V2 (DCA + Correlation)")
    print("🎯 UNREALIZED $300 → REALIZED")
    print("😴 10 MIN COOLDOWN")
    print("🔄 AUTO RESUME")
    print("=" * 70)

    try:
        validate_config()
    except Exception as e:
        print(f"❌ CONFIG ERROR: {e}")
        return

    sync_server_time()

    try:
        load_exchange_info()
        get_position_mode()
    except Exception as e:
        print(f"⚠️ Exchange setup: {e}")

    # Strategy state-г ачаалах
    load_strategy_state()

    try:
        sync_existing_positions()
    except Exception as e:
        print(f"❌ Position sync: {e}")

    try:
        session_start_balance = get_usdt_balance()
        cycle_start_balance = session_start_balance
        last_cycle_balance = session_start_balance
    except Exception:
        session_start_balance = 0.0
        cycle_start_balance = 0.0
        last_cycle_balance = 0.0
    cycle_start_time = time.time()

    send_telegram(
        format_block(
            "SMART BOT V2 АСЛАА! (DCA + Correlation)",
            "🤖",
            [
                ("Strategies × Coins", f"6 × {len(SYMBOLS_POOL)}"),
                ("Leverage", f"{LEVERAGE}x"),
                ("Allocation", f"{TRADE_ALLOCATION * 100:.0f}%"),
                ("Trailing", f"{TRAILING_CALLBACK_RATE}%"),
                ("Take Profit", f"{TAKE_PROFIT_PCT}%"),
                ("Target (unrealized)", f"${TARGET_PROFIT:.2f}"),
                ("DCA", f"{DCA_LEVELS} levels, {DCA_TRIGGER_PCT}% trigger"),
                ("Correlation", f"{CORRELATION_THRESHOLD} threshold"),
                ("", ""),
                ("Target хүрэхэд", "TP/SL цуцлаад бүх позиц хаана"),
                ("Дараа нь", "10 мин cooldown → автомат үргэлжлэл"),
            ]
        )
    )

    if BACKTEST_ENABLED:
        try:
            print("\n🧪 Running initial backtest for all strategies...")
            test_symbols = SYMBOLS_POOL[:2]
            for strategy in STRATEGY_NAMES:
                if strategy == "GRID_TRADING":
                    continue
                for symbol in test_symbols:
                    report = run_backtest(symbol, strategy, days=BACKTEST_DAYS, interval=BACKTEST_INTERVAL)
                    if report and "error" not in report.lower() and "хангалттай" not in report:
                        send_telegram(report)
                    time.sleep(1)
        except Exception as e:
            print(f"❌ Backtest error: {e}")

    try:
        selected = screen_coins()
        send_selection_report(selected)
        execute_trades(selected, get_usdt_balance())
    except Exception as e:
        error = traceback.format_exc()
        print(f"❌ Initial error:\n{error}")
        send_telegram(format_block("АНХНЫ АЛДАА", "❌", [("Error", str(e)[:400])]))

    last_selection_time = time.time()
    performance_report_time = time.time()
    cycle_count = 0

    while True:
        try:
            current_time = time.time()

            if safety_lock:
                safety_recovery()
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            try:
                monitor_positions()
            except Exception as e:
                print(f"❌ Monitor: {e}")

            try:
                positions = get_positions()
                total_unrealized = sum(p["unRealizedProfit"] for p in positions)
            except Exception as e:
                print(f"❌ Target check: {e}")
                total_unrealized = 0.0

            print(f"📡 {datetime.now().strftime('%H:%M:%S')} | Positions={len(positions) if 'positions' in locals() else 0} | Unrealized=${total_unrealized:.2f} / ${TARGET_PROFIT:.2f}")

            if total_unrealized >= TARGET_PROFIT:
                success = handle_target_reached(total_unrealized)
                if success:
                    target_cooldown()
                    active_trade_info.clear()
                    safety_lock = False
                    cycle_start_time = time.time()
                    cycle_start_balance = get_usdt_balance()
                    last_cycle_balance = cycle_start_balance
                    last_selection_time = time.time()

                    try:
                        selected = screen_coins()
                        send_selection_report(selected)
                        execute_trades(selected, get_usdt_balance())
                    except Exception as e:
                        print(f"❌ Auto-resume screening error: {e}")
                        send_telegram(format_block("AUTO RESUME ERROR", "❌", [("Error", str(e)[:400])]))
                    continue
                else:
                    safety_lock = True
                    time.sleep(MONITOR_INTERVAL_SEC)
                    continue

            if current_time - last_selection_time >= SELECTION_INTERVAL_MINUTES * 60:
                cycle_count += 1
                print("\n" + "=" * 70)
                print(f"🔄 CYCLE #{cycle_count}")
                print(datetime.now())
                print("=" * 70)

                try:
                    send_cycle_summary()
                except Exception as e:
                    print(f"❌ Summary: {e}")

                try:
                    update_strategy_cooldowns()
                except Exception as e:
                    print(f"❌ Cooldown: {e}")

                try:
                    selected = screen_coins()
                except Exception as e:
                    print(f"❌ Screening: {e}")
                    selected = []

                try:
                    send_selection_report(selected)
                except Exception as e:
                    print(f"❌ Selection report: {e}")

                try:
                    execute_trades(selected, get_usdt_balance())
                except Exception as e:
                    print(f"❌ Execute: {e}")
                    send_telegram(format_block("АРИЛЖААНЫ АЛДАА", "❌", [("Error", str(e)[:400])]))

                try:
                    send_performance_report()
                except Exception as e:
                    print(f"❌ Performance: {e}")

                last_selection_time = current_time

            if current_time - performance_report_time >= 86400:
                try:
                    send_performance_report()
                except Exception as e:
                    print(f"❌ Daily report: {e}")
                performance_report_time = current_time

            time.sleep(MONITOR_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("\n🛑 BOT STOPPED")
            send_telegram(format_block("БОТ ЗОГСЛОО", "🛑", [("Учир", "KeyboardInterrupt")]))
            break

        except Exception as e:
            error = traceback.format_exc()
            print(f"❌ MAIN ERROR\n{error}")
            try:
                send_telegram(format_block("ГОЛ АЛДАА", "❌", [("Traceback", error[:500])]))
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
