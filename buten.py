import os
import sys
import hashlib
import hmac
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import traceback
import math
from collections import defaultdict


# ==========================================================
# 🔐 API / TELEGRAM
# ==========================================================
API_KEY = "tyRDudce0UlVVEA9jqLRbiHulMGlCtzIMsBQqduZtrARuxFhHgJJVuoYk7l3TvrG"
API_SECRET = "4NuMPGZhbsMfAerDIQeyBV0vR1v7aOuwSh8tm3RrQUPm1HkUNf1DQB98neXutUKX"
BASE_URL = "https://demo-fapi.binance.com"

BOT_TOKEN = "8786518803:AAG8yVyTdBfOw0pOsieHOynoQnt7Qr7nl94"
CHAT_ID = "6886167068"


# ==========================================================
# 📊 CONFIG
# ==========================================================

SYMBOLS_POOL = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "MATICUSDT", "LINKUSDT",
    "DOTUSDT", "UNIUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT"
]

SELECTION_INTERVAL_MINUTES = 360
MONITOR_INTERVAL_SEC = 60
TELEGRAM_REPORT_INTERVAL_SEC = 300

MAX_SELECTIONS = 6
TRADE_ALLOCATION = 0.15
LEVERAGE = 5

STOP_LOSS_PCT = 2.0
TAKE_PROFIT_PCT = 3.0
MIN_SIGNAL_SCORE = 10.0

MIN_BALANCE_USDT = 10
MAX_TOTAL_MARGIN_USAGE = 0.90

ADAPTIVE_STRATEGY = True
STRATEGY_PERFORMANCE_TRACKING = True

CONSECUTIVE_LOSS_LIMIT = 3
STRATEGY_COOLDOWN_CYCLES = 2

PNL_LOOKBACK_LIMIT = 100
REQUEST_TIMEOUT = 15

# 🎯 ЗОРИЛТЫН АШИГ
TARGET_PROFIT = 300.0


# ==========================================================
# 🧠 STRATEGIES
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


# ==========================================================
# 💰 TOTAL REALIZED PNL
# ==========================================================

total_realized_pnl = 0.0


# ==========================================================
# 💼 ACTIVE POSITIONS
# ==========================================================

active_trade_info = {}


# ==========================================================
# ⚙️ CACHES
# ==========================================================

leverage_cache = {}
_symbol_info_cache = {}

last_telegram_report_time = 0
cycle_start_time = time.time()
last_cycle_balance = 0.0


# ==========================================================
# 🔐 VALIDATE CONFIG
# ==========================================================

def validate_config():
    missing = []
    if not API_KEY:
        missing.append("BINANCE_API_KEY")
    if not API_SECRET:
        missing.append("BINANCE_API_SECRET")
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
        )


# ==========================================================
# GENERIC HELPERS
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
    return math.floor(value * factor) / factor


def is_api_error(data):
    return (
        isinstance(data, dict)
        and safe_float(data.get("code"), 0) < 0
    )


# ==========================================================
# 📱 TELEGRAM
# ==========================================================

def send_telegram(text, pin=False):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Telegram credentials missing")
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print("❌ Telegram error:", response.text)
            return False
        result = response.json()
        if pin and result.get("ok"):
            message_id = result["result"]["message_id"]
            pin_url = f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage"
            requests.post(pin_url, json={
                "chat_id": CHAT_ID,
                "message_id": message_id
            }, timeout=10)
        return True
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False


# ==========================================================
# 🔐 BINANCE SIGNATURE
# ==========================================================

def get_signature(params_str, secret):
    return hmac.new(
        secret.encode("utf-8"),
        params_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def send_signed_request(method, endpoint, params=None):
    if params is None:
        params = {}
    params = params.copy()
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query_str = "&".join(
        f"{key}={params[key]}"
        for key in sorted(params)
    )
    signature = get_signature(query_str, API_SECRET)
    url = f"{BASE_URL}{endpoint}?{query_str}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method.upper() == "POST":
            response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method.upper() == "DELETE":
            response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        return response.json()
    except Exception as e:
        print(f"❌ API error {endpoint}: {e}")
        return {"code": -9999, "msg": str(e)}


def send_public_request(endpoint, params=None):
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=REQUEST_TIMEOUT)
        return response.json()
    except Exception as e:
        print(f"❌ Public API error: {e}")
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
            elif filter_type == "MIN_NOTIONAL":
                info["minNotional"] = safe_float(f.get("notional", 0))
        _symbol_info_cache[symbol] = info


def get_symbol_info(symbol):
    if symbol not in _symbol_info_cache:
        load_exchange_info()
    return _symbol_info_cache.get(symbol)


def round_quantity(symbol, quantity):
    info = get_symbol_info(symbol)
    if not info:
        return round(quantity, 3)
    step = info.get("stepSize")
    if not step or step <= 0:
        return round(quantity, info.get("quantityPrecision", 3))
    decimals = max(0, int(round(-math.log10(step))))
    return round_down(quantity, decimals)


def round_price(symbol, price):
    info = get_symbol_info(symbol)
    if not info:
        return round(price, 2)
    tick = info.get("tickSize")
    if not tick or tick <= 0:
        return round(price, info.get("pricePrecision", 2))
    decimals = max(0, int(round(-math.log10(tick))))
    return round_down(price, decimals)


# ==========================================================
# 💰 BALANCE
# ==========================================================

def get_usdt_balance():
    data = send_signed_request("GET", "/fapi/v2/balance")
    if not isinstance(data, list):
        return 0.0
    for item in data:
        if item.get("asset") == "USDT":
            return safe_float(item.get("balance"))
    return 0.0


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


# ==========================================================
# ⚙️ LEVERAGE
# ==========================================================

def ensure_leverage(symbol, leverage=LEVERAGE):
    if leverage_cache.get(symbol) == leverage:
        return True
    result = send_signed_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol,
        "leverage": leverage
    })
    if is_api_error(result):
        print(f"❌ {symbol}: leverage error {result}")
        return False
    leverage_cache[symbol] = leverage
    return True


# ==========================================================
# 🛒 ORDERS
# ==========================================================

def place_market_order(symbol, side, quantity, reduce_only=False):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return send_signed_request("POST", "/fapi/v1/order", params)


def place_stop_loss_order(symbol, side, quantity, stop_price):
    return send_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "stopPrice": stop_price,
        "quantity": quantity,
        "workingType": "MARK_PRICE",
        "reduceOnly": "true"
    })


def place_take_profit_order(symbol, side, quantity, tp_price):
    return send_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": tp_price,
        "quantity": quantity,
        "workingType": "MARK_PRICE",
        "reduceOnly": "true"
    })


def cancel_all_orders(symbol):
    return send_signed_request("DELETE", "/fapi/v1/allOpenOrders", {
        "symbol": symbol
    })


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
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_asset_volume",
        "taker_buy_quote_asset_volume",
        "ignore"
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
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
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
# 🧠 MARKET REGIME
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
# 🎯 STRATEGY SIGNAL
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
        bullish = (
            ema20.iloc[-1] > ema50.iloc[-1]
            and ema20.iloc[-2] <= ema50.iloc[-2]
        )
        bearish = (
            ema20.iloc[-1] < ema50.iloc[-1]
            and ema20.iloc[-2] >= ema50.iloc[-2]
        )
        if bullish and sentiment >= -0.4:
            return "BUY"
        if bearish and sentiment <= 0.4:
            return "SELL"

    elif strategy == "MACD_MOMENTUM":
        if (
            histogram.iloc[-1] > 0
            and histogram.iloc[-1] > histogram.iloc[-2]
            and rsi < 70
        ):
            return "BUY"
        if (
            histogram.iloc[-1] < 0
            and histogram.iloc[-1] < histogram.iloc[-2]
            and rsi > 30
        ):
            return "SELL"

    elif strategy == "GRID_TRADING":
        if regime in ["RANGE", "VOLATILE_RANGE"] and close <= lower.iloc[-1] and rsi < 40:
            return "BUY"
        if regime in ["RANGE", "VOLATILE_RANGE"] and close >= upper.iloc[-1] and rsi > 60:
            return "SELL"

    elif strategy == "BOLLINGER_MEAN_REVERSION":
        if regime in ["RANGE", "VOLATILE_RANGE"] and close < lower.iloc[-1] and rsi < 35:
            return "BUY"
        if regime in ["RANGE", "VOLATILE_RANGE"] and close > upper.iloc[-1] and rsi > 65:
            return "SELL"

    elif strategy == "RSI_STRATEGY":
        if rsi < 30 and sentiment > -0.6:
            return "BUY"
        if rsi > 70 and sentiment < 0.6:
            return "SELL"

    elif strategy == "TREND_FOLLOWING":
        if (
            adx > 30
            and ema20.iloc[-1] > ema50.iloc[-1] > ema200.iloc[-1]
            and ema_slope > 0.5
            and sentiment >= -0.3
        ):
            return "BUY"
        if (
            adx > 30
            and ema20.iloc[-1] < ema50.iloc[-1] < ema200.iloc[-1]
            and ema_slope < -0.5
            and sentiment <= 0.3
        ):
            return "SELL"
    return "HOLD"


# ==========================================================
# 🧮 STRATEGY SCORE
# ==========================================================

def calculate_strategy_score(
    strategy,
    adx,
    rsi,
    atr_pct,
    volume_ratio,
    ema_slope,
    sentiment,
    regime
):
    score = 0.0

    if strategy == "EMA_CROSSOVER":
        if regime in ["TRENDING", "STRONG_TREND"]:
            score += 7
        score += min(adx, 40) * 0.35
        score += abs(ema_slope) * 2
        score += min(volume_ratio, 3) * 1.5
        score += sentiment * 2

    elif strategy == "MACD_MOMENTUM":
        if regime in ["TRENDING", "TRANSITION"]:
            score += 5
        score += max(0, 35 - abs(rsi - 50)) * 0.15
        score += min(adx, 35) * 0.25
        score += min(atr_pct, 5) * 2
        score += min(volume_ratio, 3)

    elif strategy == "GRID_TRADING":
        if regime in ["RANGE", "VOLATILE_RANGE"]:
            score += 8
        score += max(0, 20 - adx) * 0.4
        score += atr_pct * 3

    elif strategy == "BOLLINGER_MEAN_REVERSION":
        if regime in ["RANGE", "VOLATILE_RANGE"]:
            score += 7
        score += max(0, 20 - adx) * 0.35
        score += abs(rsi - 50) * 0.15
        score += atr_pct * 2

    elif strategy == "RSI_STRATEGY":
        if rsi <= 35 or rsi >= 65:
            score += 8
        score += abs(rsi - 50) * 0.4
        score += min(atr_pct, 5)

    elif strategy == "TREND_FOLLOWING":
        if regime == "STRONG_TREND":
            score += 10
        elif regime == "TRENDING":
            score += 7
        score += min(adx, 50) * 0.35
        score += abs(ema_slope) * 3
        score += min(volume_ratio, 3)
        score += sentiment * 2

    return max(0, score)


# ==========================================================
# 🔍 ANALYZE COIN
# ==========================================================

def analyze_coin(symbol):
    try:
        df = get_klines(symbol, "1h", 200)
        if len(df) < 100:
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
            score = calculate_strategy_score(
                strategy,
                adx,
                rsi,
                atr_pct,
                volume_ratio,
                ema_slope,
                sentiment,
                regime
            )
            signal = generate_strategy_signal(strategy, df, sentiment, regime)
            # Directional validation
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
# 🏆 STRATEGY-CENTRIC SCREENING
# ==========================================================

def screen_coins():
    print("\n" + "=" * 70)
    print(f"🔍 MARKET SCREENING {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 70)

    analyses = []
    for symbol in SYMBOLS_POOL:
        result = analyze_coin(symbol)
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
            print(f"🔄 DUPLICATE {symbol}: {len(candidates)} strategies → WINNER {winner['strategy']} Score={winner['score']:.2f}")

    unique_candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = unique_candidates[:MAX_SELECTIONS]

    print("\n🏆 FINAL SELECTION:")
    for i, coin in enumerate(selected, 1):
        print(f"{i}. {coin['symbol']} | {coin['strategy']} | {coin['signal']} | Score={coin['score']:.2f} | ADX={coin['adx']:.1f} | RSI={coin['rsi']:.1f} | Regime={coin['regime']}")
    return selected


# ==========================================================
# 📋 SELECTION REPORT
# ==========================================================

def send_selection_report(selected):
    if not selected:
        send_telegram("⚠️ *SIGNAL ОЛДСОНГҮЙ*\n━━━━━━━━━━━━━━━━━\nЭнэ cycle-д trade нээхгүй.")
        return
    msg = "🏆 *ШИНЭ TOP SIGNALS*\n━━━━━━━━━━━━━━━━━\n"
    for i, coin in enumerate(selected, 1):
        msg += (
            f"{i}. `{coin['symbol']}`\n"
            f"   Strategy: `{coin['strategy']}`\n"
            f"   Signal: `{coin['signal']}`\n"
            f"   Score: `{coin['score']:.2f}`\n"
            f"   ADX: `{coin['adx']:.1f}` | RSI: `{coin['rsi']:.1f}`\n"
            f"   Regime: `{coin['regime']}`\n\n"
        )
    send_telegram(msg, pin=True)


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
# 🔄 RECOVER EXISTING POSITIONS
# ==========================================================

def sync_existing_positions():
    positions = get_positions()
    if not positions:
        return
    for pos in positions:
        symbol = pos["symbol"]
        if symbol in active_trade_info:
            continue
        amount = pos["positionAmt"]
        side = "BUY" if amount > 0 else "SELL"
        active_trade_info[symbol] = {
            "strategy": "RECOVERED",
            "side": side,
            "entry_price": pos["entryPrice"],
            "quantity": abs(amount),
            "opened_at": time.time(),
            "opened_at_ms": int(time.time() * 1000),
            "entry_order_id": None,
            "sl_order_id": None,
            "tp_order_id": None,
            "recovered": True
        }
        print(f"🔄 RECOVERED POSITION: {symbol}")
        send_telegram(
            f"🔄 *POSITION RECOVERED*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📌 `{symbol}`\n"
            f"📈 `{side}`\n"
            f"💰 Entry: ${pos['entryPrice']:,.4f}\n"
            f"📦 Qty: `{abs(amount)}`\n"
            f"⚠️ Strategy metadata lost after restart."
        )


# ==========================================================
# 🚀 EXECUTE TRADES
# ==========================================================

def execute_trades(selected_coins, total_balance):
    if not selected_coins:
        return
    positions = get_positions()
    existing_symbols = {p["symbol"] for p in positions}
    current_margin_used = 0.0
    for pos in positions:
        current_margin_used += abs(pos["positionAmt"]) * pos["entryPrice"] / LEVERAGE
    max_margin = total_balance * MAX_TOTAL_MARGIN_USAGE

    for coin in selected_coins:
        symbol = coin["symbol"]
        strategy = coin["strategy"]
        signal = coin["signal"]
        if signal not in ["BUY", "SELL"]:
            continue
        if symbol in existing_symbols:
            print(f"⏸️ {symbol}: already has position")
            continue
        if total_balance < MIN_BALANCE_USDT:
            send_telegram(f"⚠️ *БАЛАНС БАГА*\n${total_balance:.2f}")
            return
        margin = total_balance * TRADE_ALLOCATION
        if current_margin_used + margin > max_margin:
            print(f"⏸️ {symbol}: portfolio margin limit")
            continue
        if not ensure_leverage(symbol, LEVERAGE):
            continue
        price = coin["price"]
        notional = margin * LEVERAGE
        raw_quantity = notional / price
        quantity = round_quantity(symbol, raw_quantity)
        if quantity <= 0:
            continue

        cancel_all_orders(symbol)
        order_side = "BUY" if signal == "BUY" else "SELL"
        close_side = "SELL" if signal == "BUY" else "BUY"
        print(f"\n🚀 OPEN {symbol}\nStrategy={strategy}\nSignal={signal}\nQty={quantity}")

        order = place_market_order(symbol, order_side, quantity)
        if is_api_error(order):
            send_telegram(f"❌ *ORDER FAILED*\n`{symbol}`\n`{str(order)[:1000]}`")
            continue

        entry_price = safe_float(order.get("avgPrice"), price)
        if entry_price <= 0:
            entry_price = price
        opened_at_ms = int(time.time() * 1000)

        if signal == "BUY":
            sl_price = entry_price * (1 - STOP_LOSS_PCT / 100)
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
        else:
            sl_price = entry_price * (1 + STOP_LOSS_PCT / 100)
            tp_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)
        sl_price = round_price(symbol, sl_price)
        tp_price = round_price(symbol, tp_price)

        sl_order = place_stop_loss_order(symbol, close_side, quantity, sl_price)
        if is_api_error(sl_order):
            send_telegram(f"🚨 *CRITICAL RISK ERROR*\n`{symbol}`\nSL үүссэнгүй.\nPosition хаахыг оролдож байна.")
            close_result = place_market_order(symbol, close_side, quantity, reduce_only=True)
            if is_api_error(close_result):
                send_telegram(f"🚨 *CRITICAL*\n`{symbol}` хаах order failed.")
            continue

        tp_order = place_take_profit_order(symbol, close_side, quantity, tp_price)
        if is_api_error(tp_order):
            send_telegram(f"⚠️ *TP FAILED*\n`{symbol}`\nSL идэвхтэй.")

        active_trade_info[symbol] = {
            "strategy": strategy,
            "side": signal,
            "entry_price": entry_price,
            "quantity": quantity,
            "opened_at": time.time(),
            "opened_at_ms": opened_at_ms,
            "entry_order_id": order.get("orderId"),
            "sl_order_id": sl_order.get("orderId") if isinstance(sl_order, dict) else None,
            "tp_order_id": tp_order.get("orderId") if isinstance(tp_order, dict) else None,
            "recovered": False
        }
        existing_symbols.add(symbol)
        current_margin_used += margin

        send_telegram(
            f"🚀 *ШИНЭ ПОЗИЦ НЭЭГДЛЭЭ*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📌 `{symbol}`\n"
            f"📊 Strategy: `{strategy}`\n"
            f"📈 Signal: `{signal}`\n"
            f"💰 Entry: `${entry_price:,.4f}`\n"
            f"🛑 SL: `${sl_price:,.4f}` (-{STOP_LOSS_PCT:.1f}%)\n"
            f"🎯 TP: `${tp_price:,.4f}` (+{TAKE_PROFIT_PCT:.1f}%)\n"
            f"📦 Qty: `{quantity}`\n"
            f"💵 Margin: `${margin:.2f}`\n"
            f"⚡ Leverage: `{LEVERAGE}x`\n"
            f"⭐ Score: `{coin['score']:.2f}`\n"
            f"📈 ADX: `{coin['adx']:.1f}`\n"
            f"📉 RSI: `{coin['rsi']:.1f}`\n"
            f"🌊 Regime: `{coin['regime']}`"
        )
        time.sleep(1)


# ==========================================================
# 📡 MONITOR POSITIONS
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
        strategy = trade_data.get("strategy", "UNKNOWN")
        opened_at_ms = trade_data.get(
            "opened_at_ms",
            int(trade_data.get("opened_at", time.time()) * 1000)
        )
        pnl = get_trade_realized_pnl(symbol, opened_at_ms)
        if strategy != "RECOVERED":
            update_strategy_performance(strategy, pnl)
        print(f"🔴 CLOSED {symbol} | Strategy={strategy} | PnL=${pnl:.2f}")
        send_telegram(
            f"{'🟢' if pnl > 0 else '🔴'} *ПОЗИЦ ХААГДЛАА*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📌 `{symbol}`\n"
            f"📊 `{strategy}`\n"
            f"💰 PnL: `{'+' if pnl >= 0 else ''}${pnl:.2f}`"
        )
        cancel_all_orders(symbol)

    if not positions:
        return

    now = time.time()
    if now - last_telegram_report_time < TELEGRAM_REPORT_INTERVAL_SEC:
        return

    msg = "📊 *ПОЗИЦЫН МОНИТОР*\n━━━━━━━━━━━━━━━━━\n"
    total_pnl = 0.0
    for pos in positions:
        symbol = pos["symbol"]
        pnl = pos["unRealizedProfit"]
        total_pnl += pnl
        trade_data = active_trade_info.get(symbol, {})
        strategy = trade_data.get("strategy", "UNKNOWN")
        side = trade_data.get("side", "UNKNOWN")
        msg += (
            f"🔹 `{symbol}` `{side}`\n"
            f"   Strategy: `{strategy}`\n"
            f"   Entry: ${pos['entryPrice']:,.4f}\n"
            f"   Mark: ${pos['markPrice']:,.4f}\n"
            f"   PnL: `{'+' if pnl >= 0 else ''}${pnl:.2f}`\n"
            f"   Qty: `{abs(pos['positionAmt'])}`\n\n"
        )
    msg += (
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 TOTAL UNREALIZED: `{'+' if total_pnl >= 0 else ''}${total_pnl:.2f}`"
    )
    send_telegram(msg)
    last_telegram_report_time = now


# ==========================================================
# 📈 STRATEGY PERFORMANCE
# ==========================================================

def update_strategy_performance(strategy, pnl):
    global total_realized_pnl
    if strategy not in strategy_stats:
        return
    stats = strategy_stats[strategy]
    stats["trades"] += 1
    stats["total_pnl"] += pnl
    total_realized_pnl += pnl  # 🎯 ЗОРИЛТЫН АШИГ

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
                f"⚠️ *STRATEGY PAUSED*\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"📊 `{strategy}`\n"
                f"📉 Loss streak: `{stats['consecutive_losses']}`\n"
                f"⏸️ Pause: `{STRATEGY_COOLDOWN_CYCLES}` cycles"
            )


def update_strategy_cooldowns():
    for strategy, stats in strategy_stats.items():
        if stats["paused_cycles"] <= 0:
            continue
        stats["paused_cycles"] -= 1
        if stats["paused_cycles"] <= 0:
            stats["active"] = True
            stats["consecutive_losses"] = 0
            send_telegram(
                f"🔄 *STRATEGY REACTIVATED*\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"📊 `{strategy}`"
            )


def get_active_strategies():
    return [s for s, stats in strategy_stats.items() if stats["active"]]


def send_performance_report():
    if not STRATEGY_PERFORMANCE_TRACKING:
        return
    msg = "📊 *СТРАТЕГИЙН ГҮЙЦЭТГЭЛ*\n━━━━━━━━━━━━━━━━━\n"
    total_pnl = 0.0
    for strategy, stats in strategy_stats.items():
        if stats["trades"] == 0:
            continue
        win_rate = stats["wins"] / stats["trades"] * 100
        status = "🟢 ACTIVE" if stats["active"] else "🔴 PAUSED"
        pnl = stats["total_pnl"]
        msg += (
            f"🔹 `{strategy}` {status}\n"
            f"   Trades: `{stats['trades']}`\n"
            f"   Win: `{stats['wins']}` | Loss: `{stats['losses']}`\n"
            f"   Win rate: `{win_rate:.1f}%`\n"
            f"   PnL: `{'+' if pnl >= 0 else ''}${pnl:.2f}`\n"
            f"   Loss streak: `{stats['consecutive_losses']}`\n\n"
        )
        total_pnl += pnl
    msg += f"━━━━━━━━━━━━━━━━━\n💰 TOTAL PnL: `{'+' if total_pnl >= 0 else ''}${total_pnl:.2f}`"
    send_telegram(msg)


# ==========================================================
# 📆 CYCLE SUMMARY
# ==========================================================

def send_cycle_summary():
    global cycle_start_time, last_cycle_balance
    current_balance = get_usdt_balance()
    balance_change = current_balance - last_cycle_balance
    msg = (
        f"📆 *6 ЦАГИЙН ЦИКЛ*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.fromtimestamp(cycle_start_time).strftime('%H:%M:%S')} → {datetime.now().strftime('%H:%M:%S')}\n"
        f"💰 Balance change: `{'+' if balance_change >= 0 else ''}${balance_change:.2f}`\n"
        f"💵 Balance: `${current_balance:.2f}`\n"
        f"🧠 Active strategies: `{len(get_active_strategies())}`"
    )
    send_telegram(msg, pin=True)
    cycle_start_time = time.time()
    last_cycle_balance = current_balance


# ==========================================================
# 🚀 MAIN
# ==========================================================

def main():
    global last_cycle_balance, cycle_start_time, total_realized_pnl

    print("=" * 70)
    print("🤖 SMART MULTI-STRATEGY PORTFOLIO BOT v3 (Target: 300$)")
    print("=" * 70)

    try:
        validate_config()
    except Exception as e:
        print(f"❌ CONFIG ERROR: {e}")
        return

    try:
        load_exchange_info()
    except Exception as e:
        print(f"⚠️ Exchange info: {e}")

    try:
        sync_existing_positions()
    except Exception as e:
        print(f"❌ Position sync: {e}")

    send_telegram(
        "🤖 *SMART PORTFOLIO BOT v3 АСЛАА*\n"
        "━━━━━━━━━━━━━━━━━\n"
        "📊 6 Strategy × 15 Coins\n"
        "🏆 Strategy-centric selection\n"
        "🎯 Best coin per strategy\n"
        "🔄 Duplicate resolution\n"
        "🏆 Top 6 signals\n"
        "📈 BUY + SELL\n"
        "🧠 Market regime\n"
        "🛡️ SL / TP\n"
        "🔄 Adaptive strategy\n"
        "📊 Performance tracking\n"
        "🔄 Position recovery\n"
        f"🎯 Target Profit: `${TARGET_PROFIT}`"
    )

    try:
        last_cycle_balance = get_usdt_balance()
    except Exception:
        last_cycle_balance = 0.0
    cycle_start_time = time.time()

    # Initial screening
    try:
        selected = screen_coins()
        send_selection_report(selected)
        execute_trades(selected, get_usdt_balance())
    except Exception as e:
        error = traceback.format_exc()
        print(f"❌ Initial error:\n{error}")
        send_telegram(f"❌ *АНХНЫ АЛДАА*\n`{str(e)[:3000]}`")

    last_selection_time = time.time()
    performance_report_time = time.time()
    cycle_count = 0

    while True:
        try:
            current_time = time.time()

            # Monitor
            try:
                monitor_positions()
            except Exception as e:
                print(f"❌ Monitor: {e}")

            # 🎯 TARGET PROFIT CHECK
            if total_realized_pnl >= TARGET_PROFIT:
                msg = (
                    f"🎯 *ЗОРИЛТОТ АШИГ ХҮРСЭН!*\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"💰 Бодит ашиг: `${total_realized_pnl:.2f}`\n"
                    f"🎯 Зорилт: `${TARGET_PROFIT:.2f}`\n"
                    f"⏸️ Бот зогсож байна.\n"
                    f"🔄 Дахин эхлүүлэхийн тулд гарын авлагаар асаана уу."
                )
                send_telegram(msg)
                print(f"\n✅ {msg}\n")
                sys.exit(0)

            # 6 HOUR CYCLE
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
                    send_telegram(f"❌ *АРИЛЖААНЫ АЛДАА*\n`{str(e)[:3000]}`")

                try:
                    send_performance_report()
                except Exception as e:
                    print(f"❌ Performance: {e}")

                last_selection_time = current_time

            # Daily report
            if current_time - performance_report_time >= 86400:
                try:
                    send_performance_report()
                except Exception as e:
                    print(f"❌ Daily report: {e}")
                performance_report_time = current_time

            time.sleep(MONITOR_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("\n🛑 BOT STOPPED")
            send_telegram("🛑 *БОТ ЗОГСЛОО*")
            break
        except Exception as e:
            error = traceback.format_exc()
            print(f"❌ MAIN ERROR\n{error}")
            try:
                send_telegram(f"❌ *ГОЛ АЛДАА*\n`{error[:3500]}`")
            except:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
