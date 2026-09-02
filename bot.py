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
import io
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import urlencode

# Timezone болон график
try:
    import pytz
except ImportError:
    print("⚠️ pytz not found. Installing...")
    os.system("pip install pytz")
    import pytz

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
except ImportError:
    print("⚠️ matplotlib not found. Installing...")
    os.system("pip install matplotlib")
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

from telegram_format import format_block, format_section, money
from settings import *

# ==========================================================
# 📦 ТОХИРГОО
# ==========================================================
CHOP_PERIOD = getattr(settings, 'CHOP_PERIOD', 14)
SUPERTREND_PERIOD = getattr(settings, 'SUPERTREND_PERIOD', 10)
SUPERTREND_MULTIPLIER = getattr(settings, 'SUPERTREND_MULTIPLIER', 3)
MTF_ENABLED = getattr(settings, 'MTF_ENABLED', True)
VWAP_ENABLED = getattr(settings, 'VWAP_ENABLED', True)
FUNDING_ENABLED = getattr(settings, 'FUNDING_ENABLED', True)
ORDER_BOOK_ENABLED = getattr(settings, 'ORDER_BOOK_ENABLED', True)
ORDER_BOOK_LIMIT = getattr(settings, 'ORDER_BOOK_LIMIT', 20)
CHART_ENABLED = getattr(settings, 'CHART_ENABLED', True)
CHART_SEND_ON_SIGNAL = getattr(settings, 'CHART_SEND_ON_SIGNAL', True)

# ==========================================================
# 📦 STATE PERSISTENCE
# ==========================================================
STRATEGY_STATE_FILE = os.path.join(os.path.dirname(__file__), "strategy_state.json")
BACKTEST_FEE_RATE = 0.0004
BACKTEST_SLIPPAGE_RATE = 0.0005

# ==========================================================
# 🧠 STRATEGY SETTINGS
# ==========================================================

STRATEGY_NAMES = [
    "SUPERTREND",
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
session_peak_balance = 0.0
drawdown_lock_active = False
drawdown_halt = False


# ==========================================================
# 💼 ACTIVE POSITIONS
# ==========================================================

active_trade_info = {}


# ==========================================================
# 🧮 DCA STATE
# ==========================================================

dca_info = {}


# ==========================================================
# ⚙️ CACHE
# ==========================================================

leverage_cache = {}
_symbol_info_cache = {}
last_telegram_report_time = 0
server_time_offset_ms = 0
position_mode_cache = None
safety_lock = False
unprotected_symbols = set()

# ---- Correlation Cache ----
_correlation_cache = {}
_correlation_cache_time = {}


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
# 📱 TELEGRAM
# ==========================================================

def send_telegram(text, pin=False):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"{TELEGRAM_API_ROOT}/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
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

def send_telegram_photo(photo_bytes, caption=""):
    """Telegram-д зураг (chart) илгээх"""
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"{TELEGRAM_API_ROOT}/bot{BOT_TOKEN}/sendPhoto"
        files = {'photo': ('chart.png', photo_bytes, 'image/png')}
        data = {'chat_id': CHAT_ID, 'caption': caption}
        response = requests.post(url, files=files, data=data, timeout=15)
        if response.status_code == 200:
            return True
        else:
            print(f"❌ Photo send error: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Photo exception: {e}")
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
    step_str = f"{step:.12f}".rstrip('0')
    if '.' in step_str:
        return len(step_str.split('.')[1])
    return 0

def round_quantity(symbol, quantity):
    info = get_symbol_info(symbol)
    if not info:
        print(f"⚠️ {symbol}: no exchange info found — skipping")
        return None
    step = info.get("stepSize")
    if not step or step <= 0:
        return round(quantity, int(info.get("quantityPrecision", 3)))
    decimals = decimals_from_step(step)
    return round_down(quantity, decimals)

def round_price(symbol, price):
    info = get_symbol_info(symbol)
    if not info:
        print(f"⚠️ {symbol}: no exchange info found — skipping")
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
# 🛒 ORDER FUNCTIONS
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
    params = params.copy()
    params["algoType"] = "CONDITIONAL"
    hedge_mode = get_position_mode()
    if hedge_mode:
        params.pop("reduceOnly", None)
    return send_signed_request("POST", "/fapi/v1/algoOrder", params)

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
        "triggerPrice": str(stop_price),
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
        "triggerPrice": str(tp_price),
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
    """ЗӨВ арга: GET /openAlgoOrders → DELETE /algoOrder"""
    try:
        orders = send_signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        if not isinstance(orders, list):
            return {"status": "no_orders", "data": orders}
        
        results = []
        for order in orders:
            algo_id = order.get("algoId")
            if algo_id:
                result = send_signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
                results.append(result)
                time.sleep(0.1)
        return {"status": "cancelled", "count": len(results), "results": results}
    except Exception as e:
        print(f"⚠️ Cancel algo orders error: {e}")
        return {"status": "error", "error": str(e)}

def cancel_all_symbol_orders(symbol):
    normal = cancel_all_orders(symbol)
    time.sleep(0.2)
    algo = cancel_all_algo_orders(symbol)
    return {"normal": normal, "algo": algo}


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
# 📊 ORDER BOOK (DEPTH)
# ==========================================================

def get_order_book(symbol, limit=20):
    try:
        data = send_public_request("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        bids = [[float(b[0]), float(b[1])] for b in data.get("bids", [])]
        asks = [[float(a[0]), float(a[1])] for a in data.get("asks", [])]
        return bids, asks
    except Exception as e:
        print(f"⚠️ Order book error {symbol}: {e}")
        return [], []

def find_strong_levels(symbol, price):
    if not ORDER_BOOK_ENABLED:
        return None, None
    bids, asks = get_order_book(symbol, ORDER_BOOK_LIMIT)
    if not bids or not asks:
        return None, None
    
    total_bid_volume = sum(b[1] for b in bids)
    total_ask_volume = sum(a[1] for a in asks)
    
    strong_bid = max(bids, key=lambda x: x[1]) if bids else None
    strong_ask = max(asks, key=lambda x: x[1]) if asks else None
    
    support = strong_bid[0] if strong_bid else None
    resistance = strong_ask[0] if strong_ask else None
    
    return support, resistance


# ==========================================================
# 📈 TELEGRAM GRAPHIC CHART
# ==========================================================

def send_chart(symbol, df, signal=None, score=None):
    if not CHART_ENABLED:
        return False
    try:
        df_plot = df.tail(100).copy()
        if len(df_plot) < 20:
            return False
        
        fig, ax = plt.subplots(figsize=(10, 6))
        
        ax.plot(df_plot.index, df_plot["close"], color='blue', linewidth=1.5, label='Close')
        
        ema20 = calculate_ema(df_plot, 20)
        ema50 = calculate_ema(df_plot, 50)
        ax.plot(df_plot.index, ema20, color='orange', linestyle='--', linewidth=1, label='EMA 20')
        ax.plot(df_plot.index, ema50, color='red', linestyle='--', linewidth=1, label='EMA 50')
        
        upper, middle, lower = calculate_bollinger(df_plot)
        ax.fill_between(df_plot.index, upper, lower, alpha=0.1, color='gray')
        ax.plot(df_plot.index, upper, color='gray', linestyle=':', linewidth=0.8)
        ax.plot(df_plot.index, lower, color='gray', linestyle=':', linewidth=0.8)
        
        if signal:
            last_price = df_plot["close"].iloc[-1]
            if signal == "BUY":
                ax.scatter(df_plot.index[-1], last_price, color='green', s=100, marker='^', label='BUY')
            elif signal == "SELL":
                ax.scatter(df_plot.index[-1], last_price, color='red', s=100, marker='v', label='SELL')
        
        title = f"{symbol} | {signal if signal else 'No Signal'}"
        if score:
            title += f" | Score: {score:.2f}"
        ax.set_title(title, fontsize=12)
        ax.legend(loc='upper left')
        ax.grid(True, alpha=0.3)
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)
        
        caption = f"📊 {symbol} | Signal: {signal}" if signal else f"📊 {symbol}"
        return send_telegram_photo(buf.getvalue(), caption)
    except Exception as e:
        print(f"❌ Chart generation error: {e}")
        return False


# ==========================================================
# 📊 ШИНЭ ҮЗҮҮЛЭЛТҮҮД
# ==========================================================

def calculate_chop(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    atr_sum = (high - low).rolling(period).sum()
    max_high = high.rolling(period).max()
    min_low = low.rolling(period).min()
    
    denominator = (max_high - min_low)
    denominator = denominator.replace(0, np.nan)
    
    chop = 100 * np.log10(atr_sum / denominator) / np.log10(period)
    return chop.fillna(50)

def calculate_supertrend(df, period=10, multiplier=3):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    atr = calculate_atr(df, period)
    hl2 = (high + low) / 2
    upper_band = hl2 + (multiplier * atr)
    lower_band = hl2 - (multiplier * atr)
    
    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)
    
    for i in range(1, len(df)):
        if pd.isna(close.iloc[i]) or pd.isna(upper_band.iloc[i]) or pd.isna(lower_band.iloc[i]):
            direction.iloc[i] = direction.iloc[i-1] if i>1 else 1
            continue
            
        if close.iloc[i] > upper_band.iloc[i-1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < lower_band.iloc[i-1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i-1]
            if direction.iloc[i] == 1:
                lower_band.iloc[i] = max(lower_band.iloc[i], lower_band.iloc[i-1])
            else:
                upper_band.iloc[i] = min(upper_band.iloc[i], upper_band.iloc[i-1])
        
        supertrend.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]
    
    direction.iloc[0] = 1
    supertrend.iloc[0] = lower_band.iloc[0]
    return supertrend, direction

def calculate_vwap(df):
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
    return vwap

def get_funding_rate(symbol):
    if not FUNDING_ENABLED:
        return 0.0
    try:
        data = send_public_request("/fapi/v1/premiumIndex", {"symbol": symbol})
        return safe_float(data.get("lastFundingRate", 0))
    except Exception as e:
        print(f"⚠️ Funding rate error {symbol}: {e}")
        return 0.0

def get_mtf_signal(symbol):
    if not MTF_ENABLED:
        return "NEUTRAL"
    try:
        df_4h = get_klines(symbol, "4h", 50)
        df_1h = get_klines(symbol, "1h", 50)
        
        if len(df_4h) < 20 or len(df_1h) < 20:
            return "NEUTRAL"
            
        ema_4h = calculate_ema(df_4h, 50).iloc[-1]
        close_4h = df_4h["close"].iloc[-1]
        ema_1h = calculate_ema(df_1h, 50).iloc[-1]
        close_1h = df_1h["close"].iloc[-1]
        
        trend_4h = "BUY" if close_4h > ema_4h else "SELL"
        trend_1h = "BUY" if close_1h > ema_1h else "SELL"
        
        if trend_4h == "BUY" and trend_1h == "BUY": return "BULLISH"
        if trend_4h == "SELL" and trend_1h == "SELL": return "BEARISH"
        return "NEUTRAL"
    except Exception as e:
        print(f"⚠️ MTF error {symbol}: {e}")
        return "NEUTRAL"


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
# 🧠 REGIME (CHOP-д суурилсан)
# ==========================================================

def determine_regime(chop, adx, ema_slope, atr_pct):
    if pd.isna(chop):
        if adx >= 30 and abs(ema_slope) >= 0.5:
            return "STRONG_TREND"
        if adx >= 25:
            return "TRENDING"
        if adx <= 18 and atr_pct >= 0.5:
            return "VOLATILE_RANGE"
        if adx <= 18:
            return "RANGE"
        return "TRANSITION"
    
    if chop < 38.2:
        if abs(ema_slope) >= 1.0:
            return "STRONG_TREND"
        return "TRENDING"
    elif chop > 61.8:
        if atr_pct >= 0.5:
            return "VOLATILE_RANGE"
        return "RANGE"
    else:
        return "TRANSITION"


# ==========================================================
# 🎯 SIGNAL GENERATION
# ==========================================================

def generate_strategy_signal(strategy, df, sentiment, regime, chop=None):
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
    
    vwap = calculate_vwap(df).iloc[-1] if VWAP_ENABLED else close

    if strategy == "SUPERTREND":
        st, direction = calculate_supertrend(df, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
        if len(direction) < 2:
            return "HOLD"
        if direction.iloc[-1] == 1 and direction.iloc[-2] == -1:
            if sentiment >= -0.4 and regime in ["TRENDING", "STRONG_TREND"]:
                return "BUY"
        if direction.iloc[-1] == -1 and direction.iloc[-2] == 1:
            if sentiment <= 0.4 and regime in ["TRENDING", "STRONG_TREND"]:
                return "SELL"
        return "HOLD"

    elif strategy == "MACD_MOMENTUM":
        if histogram.iloc[-1] > 0 and histogram.iloc[-1] > histogram.iloc[-2] and rsi < 70: return "BUY"
        if histogram.iloc[-1] < 0 and histogram.iloc[-1] < histogram.iloc[-2] and rsi > 30: return "SELL"

    elif strategy == "GRID_TRADING":
        if regime in ["RANGE", "VOLATILE_RANGE"] and close <= lower.iloc[-1] and rsi < 40: return "BUY"
        if regime in ["RANGE", "VOLATILE_RANGE"] and close >= upper.iloc[-1] and rsi > 60: return "SELL"

    elif strategy == "BOLLINGER_MEAN_REVERSION":
        if regime in ["RANGE", "VOLATILE_RANGE"]:
            vwap_deviation = (close - vwap) / vwap * 100
            if close < lower.iloc[-1] and rsi < 35 and vwap_deviation < -1.0:
                return "BUY"
            if close > upper.iloc[-1] and rsi > 65 and vwap_deviation > 1.0:
                return "SELL"
        return "HOLD"

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

def calculate_strategy_score(strategy, adx, rsi, atr_pct, volume_ratio, ema_slope, sentiment, regime, chop, mtf_signal):
    score = 0.0
    
    mtf_penalty = 0
    if mtf_signal == "NEUTRAL":
        mtf_penalty = -5
    
    chop_score = 0
    if regime in ["STRONG_TREND", "TRENDING"]:
        chop_score = 5
    elif regime in ["RANGE", "VOLATILE_RANGE"]:
        chop_score = 3
    
    if strategy == "SUPERTREND":
        if regime in ["TRENDING", "STRONG_TREND"]: score += 8
        score += min(adx, 50) * 0.3 + abs(ema_slope) * 2 + min(volume_ratio, 3) + sentiment * 2
        score += mtf_penalty

    elif strategy == "MACD_MOMENTUM":
        if regime in ["TRENDING", "TRANSITION"]: score += 5
        score += max(0, 35 - abs(rsi - 50)) * 0.15 + min(adx, 35) * 0.25 + min(atr_pct, 5) * 2 + min(volume_ratio, 3)
        score += mtf_penalty * 0.5

    elif strategy == "GRID_TRADING":
        if regime in ["RANGE", "VOLATILE_RANGE"]: score += 8
        score += max(0, 25 - adx) * 0.4 + atr_pct * 3 + chop_score

    elif strategy == "BOLLINGER_MEAN_REVERSION":
        if regime in ["RANGE", "VOLATILE_RANGE"]: score += 7
        score += max(0, 25 - adx) * 0.35 + abs(rsi - 50) * 0.15 + atr_pct * 2 + chop_score

    elif strategy == "RSI_STRATEGY":
        if rsi <= 35 or rsi >= 65: score += 8
        score += abs(rsi - 50) * 0.4 + min(atr_pct, 5)

    elif strategy == "TREND_FOLLOWING":
        if regime == "STRONG_TREND": score += 10
        elif regime == "TRENDING": score += 7
        score += min(adx, 50) * 0.35 + abs(ema_slope) * 3 + min(volume_ratio, 3) + sentiment * 2
        score += mtf_penalty

    return max(0, score)


# ==========================================================
# 📊 CORRELATION (Кэштэй)
# ==========================================================

def calculate_correlation_cached(symbol1, symbol2, lookback=50):
    global _correlation_cache, _correlation_cache_time
    key = f"{symbol1}_{symbol2}"
    now = time.time()
    if key in _correlation_cache and (now - _correlation_cache_time.get(key, 0)) < CORRELATION_CACHE_TTL:
        return _correlation_cache[key]
    
    corr = calculate_correlation(symbol1, symbol2, lookback)
    _correlation_cache[key] = corr
    _correlation_cache_time[key] = now
    return corr

def calculate_correlation(symbol1, symbol2, lookback=50):
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
# 🛡️ MIN NOTIONAL CHECK
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
# 🔍 ANALYZE COIN
# ==========================================================

def analyze_coin(symbol, check_correlation=True, active_symbols=None):
    try:
        df = get_klines(symbol, "1h", 200)
        if len(df) < 100:
            return None

        mtf_signal = get_mtf_signal(symbol)
        if MTF_ENABLED and mtf_signal == "NEUTRAL":
            print(f"⏸️ SKIPPED {symbol}: MTF Neutral")
            return None

        if CORRELATION_ENABLED and check_correlation and active_symbols:
            for sym in active_symbols:
                if sym == symbol:
                    continue
                corr = calculate_correlation_cached(symbol, sym, CORRELATION_LOOKBACK)
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
        
        chop = calculate_chop(df, CHOP_PERIOD).iloc[-1]
        vwap = calculate_vwap(df).iloc[-1]
        funding_rate = get_funding_rate(symbol)
        
        # Order Book-оос хүчтэй түвшингүүд
        support, resistance = find_strong_levels(symbol, close)
        if support and resistance:
            print(f"🔹 {symbol} Support: {support:.2f} | Resistance: {resistance:.2f}")
        
        sentiment = 0.0
        if funding_rate > 0.01:
            sentiment -= 0.5
        elif funding_rate < -0.01:
            sentiment += 0.5
        
        regime = determine_regime(chop, adx, ema_slope, atr_pct)

        strategy_results = {}
        for strategy in STRATEGY_NAMES:
            if not strategy_stats[strategy]["active"]:
                continue
            score = calculate_strategy_score(
                strategy, adx, rsi, atr_pct, volume_ratio, 
                ema_slope, sentiment, regime, chop, mtf_signal
            )
            signal = generate_strategy_signal(strategy, df, sentiment, regime, chop)
            
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
                "sentiment": sentiment,
                "chop": chop,
                "vwap": vwap,
                "funding": funding_rate,
                "mtf": mtf_signal
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
            "chop": chop,
            "vwap": vwap,
            "funding": funding_rate,
            "mtf": mtf_signal,
            "strategies": strategy_results
        }
    except Exception as e:
        print(f"❌ analyze_coin {symbol}: {e}")
        return None


# ==========================================================
# 🏆 SCREENING
# ==========================================================

def screen_coins():
    print("\n" + "=" * 70)
    print(f"🔍 MARKET SCREENING {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 70)

    skipped_reasons = []

    current_positions = get_positions()
    active_symbols = {p["symbol"] for p in current_positions}
    analyses = []
    for symbol in SYMBOLS_POOL:
        result = analyze_coin(symbol, check_correlation=True, active_symbols=active_symbols)
        if result:
            analyses.append(result)

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

    by_symbol = defaultdict(list)
    for candidate in strategy_candidates:
        by_symbol[candidate["symbol"]].append(candidate)
    unique_candidates = []
    for symbol, candidates in by_symbol.items():
        winner = max(candidates, key=lambda x: x["score"])
        unique_candidates.append(winner)
        if len(candidates) > 1:
            print(f"🔄 DUPLICATE {symbol}: WINNER {winner['strategy']}")

    if CORRELATION_ENABLED:
        final_selected = []
        removed_by_correlation = []
        for i, coin in enumerate(unique_candidates):
            ok = True
            for j in range(i):
                if abs(calculate_correlation_cached(coin["symbol"], unique_candidates[j]["symbol"], CORRELATION_LOOKBACK)) > CORRELATION_THRESHOLD:
                    ok = False
                    removed_by_correlation.append(coin["symbol"])
                    print(f"🔴 REMOVED {coin['symbol']}: high correlation with {unique_candidates[j]['symbol']}")
                    break
            if ok:
                final_selected.append(coin)
            if len(final_selected) >= MAX_SELECTIONS:
                break
        selected = final_selected[:MAX_SELECTIONS]
        if removed_by_correlation:
            skipped_reasons.append(f"🔗 Корреляциас хасагдсан: {', '.join(removed_by_correlation)}")
    else:
        selected = unique_candidates[:MAX_SELECTIONS]

    total_balance = get_usdt_balance()
    positions = get_positions()
    current_margin_used = 0.0
    for pos in positions:
        actual_lev = get_actual_leverage(pos["symbol"])
        current_margin_used += abs(pos["positionAmt"]) * pos["entryPrice"] / actual_lev
    max_margin = total_balance * MAX_TOTAL_MARGIN_USAGE
    if current_margin_used >= max_margin * 0.95:
        skipped_reasons.append(f"💳 Маржин хязгаарт хүрсэн (ашигласан: {current_margin_used:.2f} / хязгаар: {max_margin:.2f} USDT)")

    inactive_strategies = [s for s, stats in strategy_stats.items() if not stats["active"]]
    if inactive_strategies:
        skipped_reasons.append(f"⏸️ Идэвхгүй стратеги: {', '.join(inactive_strategies)}")

    low_score_signals = []
    for coin in analyses:
        for strategy, result in coin["strategies"].items():
            if result["signal"] in ["BUY", "SELL"] and result["score"] < MIN_SIGNAL_SCORE:
                low_score_signals.append(f"{result['symbol']} ({strategy}): {result['score']:.1f}")
    if low_score_signals:
        skipped_reasons.append(f"📉 Оноо хэт бага (MIN_SIGNAL_SCORE={MIN_SIGNAL_SCORE}): {', '.join(low_score_signals[:5])}")

    print("\n🏆 FINAL SELECTION:")
    for i, coin in enumerate(selected, 1):
        print(f"{i}. {coin['symbol']} | {coin['strategy']} | {coin['signal']} | Score={coin['score']:.2f}")

    # Дохио гарсан coin-д график илгээх
    if CHART_SEND_ON_SIGNAL:
        for coin in selected:
            symbol = coin['symbol']
            df = get_klines(symbol, "1h", 200)
            send_chart(symbol, df, coin['signal'], coin['score'])

    send_selection_report(selected, strategy_candidates, skipped_reasons)
    return selected


# ==========================================================
# 📱 SELECTION REPORT
# ==========================================================

def send_selection_report(selected, all_candidates=None, skipped_reasons=None):
    msg = ""

    if selected:
        msg += "🏆 ШИНЭ TOP SIGNALS\n━━━━━━━━━━━━━━━━━\n"
        for i, coin in enumerate(selected, 1):
            msg += f"{i}. {coin['symbol']}\n"
            msg += f"Стратеги / Strategy: {coin['strategy']}\n"
            msg += f"Дохио / Signal: {coin['signal']}\n"
            msg += f"Оноо / Score: {coin['score']:.2f}\n"
            msg += f"ADX / RSI: {coin['adx']:.1f} / {coin['rsi']:.1f}\n"
            msg += f"Regime: {coin['regime']} | CHOP: {coin.get('chop', 50):.1f}\n"
            msg += f"MTF: {coin.get('mtf', 'NEUTRAL')} | Funding: {coin.get('funding', 0)*100:.3f}%\n"
            msg += "─────────────────\n"
    else:
        msg += "⚠️ SIGNAL ОЛДСОНГҮЙ\n━━━━━━━━━━━━━━━━━\n"

    if skipped_reasons:
        msg += "\n\n📋 АРИЛЖАА НЭЭГДЭЭГҮЙ ШАЛТГААНУУД / WHY TRADES NOT OPENED:\n"
        msg += "━━━━━━━━━━━━━━━━━\n"
        for reason in skipped_reasons:
            msg += f"• {reason}\n"

    if all_candidates:
        msg += "\n\n📊 БҮХ СТРАТЕГИЙН ШИЛДЭГ ДОХИОНУУД / TOP SIGNALS PER STRATEGY:\n"
        msg += "━━━━━━━━━━━━━━━━━\n"
        for cand in all_candidates[:10]:
            msg += f"• {cand['strategy']:<20} → {cand['symbol']:<8} {cand['signal']:<4} Score={cand['score']:.1f}\n"

    send_telegram(msg)


# ==========================================================
# 🧪 BACKTESTING
# ==========================================================

def run_backtest(symbol, strategy, days=30, interval="1h"):
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
            chop = calculate_chop(window, CHOP_PERIOD).iloc[-1]
            regime = determine_regime(chop, adx, ema_slope, atr_pct)
            signal = generate_strategy_signal(strategy, window, sentiment, regime, chop)

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

        rows = [
            ("Symbol", symbol),
            ("Strategy", strategy),
            ("Period", f"сүүлийн {days} өдөр ({interval})"),
            ("Trades", str(len(trades))),
            ("Win Rate", f"{win_rate:.1f}%"),
            ("Net PnL", f"{net.sum():+.2f}%"),
            ("Avg Win", f"{avg_win:+.2f}%"),
            ("Avg Loss", f"{avg_loss:+.2f}%"),
            ("Profit Factor", f"{profit_factor:.2f}"),
            ("Expectancy/Trade", f"{expectancy:+.3f}%"),
            ("Max Drawdown", f"{max_dd:.2f}%"),
            ("Fee model", f"{BACKTEST_FEE_RATE * 100:.03f}%/side"),
            ("Slippage model", f"{BACKTEST_SLIPPAGE_RATE * 100:.03f}%/side"),
            ("Note", "энэ нь simulation; funding, liquidation болон exact exchange fills бүрэн моделдоогүй."),
        ]
        return format_block("🧪 BACKTEST REPORT", "🧪", rows)

    except Exception as e:
        return f"❌ Backtest error ({symbol}, {strategy}): {e}"


# ==========================================================
# 🔄 RECOVER EXISTING POSITIONS
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

        algo_orders = send_signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        has_protection = False
        if isinstance(algo_orders, list):
            for order in algo_orders:
                if order.get("symbol") == symbol and order.get("orderType") in ("TRAILING_STOP_MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"):
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
# ⚙️ LEVERAGE
# ==========================================================

def get_actual_leverage(symbol):
    if symbol in leverage_cache:
        return leverage_cache[symbol]
    result = send_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if is_api_error(result) or not isinstance(result, list) or not result:
        return LEVERAGE
    lev = int(safe_float(result[0].get("leverage", LEVERAGE), LEVERAGE))
    leverage_cache[symbol] = lev
    return lev

def ensure_leverage(symbol, leverage=LEVERAGE):
    if leverage_cache.get(symbol) == leverage:
        return True
    result = send_signed_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol,
        "leverage": leverage
    })
    if is_api_error(result):
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
# 🛡️ REBUILD PROTECTION ORDERS
# ==========================================================

def rebuild_protection_orders(symbol, side, quantity, entry_price, position_side):
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
        return False, None, None

    activation_price = calculate_trailing_activation(symbol, side, entry_price)
    if activation_price is None:
        return False, tp_price, None

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
        return False, tp_price, activation_price

    return True, tp_price, activation_price


# ==========================================================
# 🚀 EXECUTE TRADES
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

        dca_info[symbol] = {
            "level": 0,
            "avg_price": entry_price,
            "base_qty": actual_quantity,
            "total_qty": actual_quantity
        }

        success, tp_price, activation_price = rebuild_protection_orders(symbol, signal, actual_quantity, entry_price, actual_position_side)
        if not success:
            send_telegram(format_block("PROTECTION FAILED", "🚨", [("Symbol", symbol), ("Action", "Closing position")]))
            close_result = place_market_order(symbol, close_side, actual_quantity, reduce_only=True, position_side=actual_position_side)
            if is_api_error(close_result):
                safety_lock = True
                send_telegram(format_block("CRITICAL CLOSE FAILED", "🚨", [("Symbol", symbol)]))
                continue
            existing_symbols.discard(symbol)
            if symbol in dca_info:
                del dca_info[symbol]
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
# 📡 DCA MANAGEMENT (УНТРААСАН)
# ==========================================================

def manage_dca():
    if not DCA_ENABLED:
        return


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
# 🚨 MAX DRAWDOWN CIRCUIT BREAKER
# ==========================================================

def check_drawdown_circuit_breaker():
    global safety_lock, session_peak_balance, drawdown_lock_active, drawdown_halt

    if not MAX_SESSION_DRAWDOWN_PCT or MAX_SESSION_DRAWDOWN_PCT <= 0:
        return

    balance = get_usdt_balance()
    if balance <= 0:
        return

    if balance > session_peak_balance:
        session_peak_balance = balance
        if drawdown_lock_active:
            drawdown_lock_active = False
        return

    if session_peak_balance <= 0:
        return

    drawdown_pct = (session_peak_balance - balance) / session_peak_balance * 100
    if drawdown_pct >= MAX_SESSION_DRAWDOWN_PCT and not safety_lock:
        safety_lock = True
        drawdown_lock_active = True
        drawdown_halt = True
        print(f"🚨 MAX DRAWDOWN HIT: {drawdown_pct:.2f}% (limit {MAX_SESSION_DRAWDOWN_PCT}%) — HARD STOP")
        send_telegram(
            format_block(
                "MAX DRAWDOWN CIRCUIT BREAKER",
                "🚨",
                [
                    ("Peak Balance", f"${session_peak_balance:,.2f}"),
                    ("Current Balance", f"${balance:,.2f}"),
                    ("Drawdown", f"{drawdown_pct:.2f}% (limit {MAX_SESSION_DRAWDOWN_PCT:.1f}%)"),
                    ("", ""),
                    ("Статус", "БОТ БҮРМӨСӨН ЗОГСЛОО"),
                    ("Дараагийн алхам", "Бүх позиц хаагдана. Гараар restart хийтэл автоматаар үргэлжлэхгүй"),
                ]
            )
        )


# ==========================================================
# 🌐 NEWS TRADING
# ==========================================================

def get_next_cpi_event():
    if not NEWS_CALENDAR_URL:
        return None
    try:
        resp = requests.get(NEWS_CALENDAR_URL, timeout=10)
        data = resp.json()
        now = datetime.now(pytz.UTC)
        for item in data:
            if "CPI" in item.get("title", "") and "USD" in item.get("country", ""):
                event_time = datetime.fromisoformat(item["date"]).replace(tzinfo=pytz.timezone('America/New_York'))
                event_time_utc = event_time.astimezone(pytz.UTC)
                if event_time_utc > now:
                    return event_time_utc
    except Exception as e:
        print(f"⚠️ News calendar error: {e}")
    return None

news_mode_active = False
news_trade_done = False
last_news_check = 0
next_news_time = None

def check_news_status():
    global news_mode_active, news_trade_done, next_news_time, safety_lock
    if not NEWS_ENABLED:
        return

    now = datetime.now(pytz.UTC)
    if not next_news_time or (now - last_news_check).seconds > 3600:
        next_news_time = get_next_cpi_event()
        last_news_check = now

    if not next_news_time:
        return

    diff = (next_news_time - now).total_seconds() / 60

    if 0 < diff < NEWS_PAUSE_BEFORE:
        news_mode_active = True
        news_trade_done = False
        print(f"📰 News approaching in {diff:.0f} min. Pausing new technical trades.")
        safety_lock = True
        return

    if -NEWS_WAIT_AFTER < diff < 0:
        news_mode_active = True
        print(f"📰 News just released. Waiting {NEWS_WAIT_AFTER} min for stability...")
        safety_lock = True
        return

    if diff <= -NEWS_WAIT_AFTER and news_mode_active and not news_trade_done:
        print("📰 News cooldown finished. Executing post-news trade...")
        execute_post_news_trade()
        news_trade_done = True
        news_mode_active = False
        safety_lock = False
        return

    if diff <= - (NEWS_WAIT_AFTER + 30) and news_mode_active:
        news_mode_active = False
        safety_lock = False
        print("✅ News window closed. Resuming normal trading.")

def execute_post_news_trade():
    global news_trade_done
    if news_trade_done:
        return

    for symbol in NEWS_SYMBOLS:
        df = get_klines(symbol, interval="15m", limit=10)
        if len(df) < 5:
            continue

        first_close = df.iloc[0]["close"]
        last_close = df.iloc[-1]["close"]
        move_pct = (last_close - first_close) / first_close * 100

        if abs(move_pct) < NEWS_MIN_MOVE:
            print(f"⏸️ {symbol} move {move_pct:.2f}% < {NEWS_MIN_MOVE}%, skipping.")
            continue

        side = "BUY" if move_pct > 0 else "SELL"
        close_side = "SELL" if side == "BUY" else "BUY"
        position_side = "LONG" if side == "BUY" else "SHORT"

        balance = get_usdt_balance()
        allocation = balance * NEWS_ALLOCATION
        notional = allocation * NEWS_LEVERAGE
        price = last_close
        quantity = notional / price
        quantity = round_quantity(symbol, quantity)
        if quantity is None or quantity <= 0:
            continue

        order = place_market_order(symbol, side, quantity, reduce_only=False, position_side=position_side)
        if is_api_error(order):
            send_telegram(f"❌ News trade order failed for {symbol}: {order}")
            continue

        entry_price = safe_float(order.get("avgPrice"), price)
        if entry_price <= 0:
            entry_price = price

        if side == "BUY":
            sl_price = round_price(symbol, entry_price * (1 - NEWS_SL_PCT / 100))
            tp_price = round_price(symbol, entry_price * (1 + NEWS_TP_PCT / 100))
        else:
            sl_price = round_price(symbol, entry_price * (1 + NEWS_SL_PCT / 100))
            tp_price = round_price(symbol, entry_price * (1 - NEWS_TP_PCT / 100))

        place_stop_loss_order(symbol, close_side, quantity, sl_price, position_side=position_side)
        place_take_profit_order(symbol, close_side, quantity, tp_price, position_side=position_side)

        send_telegram(
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


# ==========================================================
# 📡 MONITOR
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
    global session_start_balance, cycle_start_balance, last_cycle_balance, cycle_start_time, safety_lock, session_peak_balance, drawdown_halt

    print("=" * 70)
    print("🤖 SMART BOT V2 (SUPERTREND + CHOP + MTF + VWAP + FUNDING + ORDERBOOK + CHART)")
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

    load_strategy_state()

    try:
        sync_existing_positions()
    except Exception as e:
        print(f"❌ Position sync: {e}")

    try:
        session_start_balance = get_usdt_balance()
        cycle_start_balance = session_start_balance
        last_cycle_balance = session_start_balance
        session_peak_balance = session_start_balance
    except Exception:
        session_start_balance = 0.0
        cycle_start_balance = 0.0
        last_cycle_balance = 0.0
        session_peak_balance = 0.0
    cycle_start_time = time.time()

    send_telegram(
        format_block(
            "SMART BOT V2 АСЛАА! (ШИНЭ ҮЗҮҮЛЭЛТҮҮД)",
            "🤖",
            [
                ("Strategies", "6 (SUPERTREND, MACD, GRID, BOLLINGER, RSI, TREND)"),
                ("Regime", "CHOP Index (38.2/61.8)"),
                ("Trend Signal", "Supertrend (EMA-г орлосон)"),
                ("Filters", "MTF (4h/1h) + VWAP + Funding Rate"),
                ("Order Book", "Strong levels detection"),
                ("Chart", "Telegram chart on signal"),
                ("Leverage", f"{LEVERAGE}x"),
                ("Allocation", f"{TRADE_ALLOCATION * 100:.0f}%"),
                ("Target", f"${TARGET_PROFIT:.2f}"),
                ("Max Drawdown", f"{MAX_SESSION_DRAWDOWN_PCT:.1f}%" if MAX_SESSION_DRAWDOWN_PCT else "OFF"),
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

            if drawdown_halt:
                try:
                    remaining = get_positions()
                    if remaining:
                        close_all_positions_and_verify()
                except Exception as e:
                    print(f"❌ Drawdown halt cleanup: {e}")
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            if safety_lock:
                safety_recovery()
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            try:
                check_news_status()
            except Exception as e:
                print(f"⚠️ News check error: {e}")

            if news_mode_active:
                try:
                    monitor_positions()
                except Exception as e:
                    print(f"❌ Monitor error during news: {e}")
                time.sleep(MONITOR_INTERVAL_SEC)
                continue

            try:
                check_drawdown_circuit_breaker()
            except Exception as e:
                print(f"❌ Drawdown check: {e}")
            if safety_lock:
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
                    send_telegram("⚠️ Скрининг хийхэд алдаа гарлаа. Дараагийн циклд дахин оролдоно.")

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
