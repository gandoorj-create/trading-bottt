import hashlib
import hmac
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime

# ==========================================================
# 🔑 API БОЛОН TELEGRAM ТОХИРГОО
# ==========================================================
API_KEY = "tyRDudce0UlVVEA9jqLRbiHulMGlCtzIMsBQqduZtrARuxFhHgJJVuoYk7l3TvrG"
API_SECRET = "4NuMPGZhbsMfAerDIQeyBV0vR1v7aOuwSh8tm3RrQUPm1HkUNf1DQB98neXutUKX"
BASE_URL = "https://demo-fapi.binance.com"

# Telegram Bot тохиргоо
BOT_TOKEN = "8786518803:AAG8yVyTdBfOw0pOsieHOynoQnt7Qr7nl94"
CHAT_ID = "6886167068"

# ==========================================================
# 📊 СТРАТЕГИЙН ТОХИРГОО
# ==========================================================
SYMBOLS_POOL = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "MATICUSDT", "LINKUSDT",
    "DOTUSDT", "UNIUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT"
]
SELECTION_INTERVAL_HOURS = 6   # 6 цаг тутам coin сонголт
MONITOR_INTERVAL_SEC = 60       # 1 минут тутам позиц хянах
TRADE_ALLOCATION = 0.20         # Дансны 20%
STOP_LOSS_PCT = 2.0             # 2% SL
TAKE_PROFIT_PCT = 1.0           # 1% TP (ашиг түгжих)
MAX_SELECTIONS = 5              # 5 coin сонгох

# ==========================================================
# 🔐 ГАРЫН ҮСЭГ (Binance API V3)
# ==========================================================
def get_signature(params_str, secret):
    return hmac.new(secret.encode('utf-8'), params_str.encode('utf-8'), hashlib.sha256).hexdigest()

def send_signed_request(method, endpoint, params=None):
    timestamp = int(time.time() * 1000)
    if params is None:
        params = {}
    params['timestamp'] = timestamp
    params['recvWindow'] = 5000
    query_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = get_signature(query_str, API_SECRET)
    url = f"{BASE_URL}{endpoint}?{query_str}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    
    if method.upper() == "GET":
        resp = requests.get(url, headers=headers)
    elif method.upper() == "POST":
        resp = requests.post(url, headers=headers)
    elif method.upper() == "DELETE":
        resp = requests.delete(url, headers=headers)
    else:
        raise ValueError("Unsupported method")
    
    return resp.json()

def send_public_request(endpoint, params=None):
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, params=params)
    return resp.json()

# ==========================================================
# 💰 ҮЛДЭГДЭЛ, ПОЗИЦ
# ==========================================================
def get_usdt_balance():
    data = send_signed_request("GET", "/fapi/v2/balance")
    for item in data:
        if item['asset'] == 'USDT':
            return float(item['balance'])
    return 0.0

def get_positions():
    data = send_signed_request("GET", "/fapi/v2/positionRisk")
    positions = []
    for pos in data:
        if float(pos['positionAmt']) != 0:
            positions.append({
                'symbol': pos['symbol'],
                'positionAmt': float(pos['positionAmt']),
                'entryPrice': float(pos['entryPrice']),
                'markPrice': float(pos['markPrice']),
                'unRealizedProfit': float(pos['unRealizedProfit'])
            })
    return positions

def place_market_order(symbol, side, quantity):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity
    }
    return send_signed_request("POST", "/fapi/v1/order", params)

def place_stop_loss_order(symbol, side, quantity, stop_price):
    """Stop-loss захиалга (STOP_MARKET)"""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "stopPrice": stop_price,
        "quantity": quantity,
        "workingType": "MARK_PRICE"
    }
    return send_signed_request("POST", "/fapi/v1/order", params)

def place_take_profit_order(symbol, side, quantity, tp_price):
    """Take-Profit захиалга (LIMIT)"""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "price": tp_price,
        "quantity": quantity,
        "timeInForce": "GTC"
    }
    return send_signed_request("POST", "/fapi/v1/order", params)

def cancel_all_orders(symbol):
    params = {"symbol": symbol}
    return send_signed_request("DELETE", "/fapi/v1/allOpenOrders", params)

def get_current_price(symbol):
    try:
        data = send_public_request("/fapi/v1/ticker/price", params={"symbol": symbol})
        return float(data['price'])
    except:
        return 0.0

# ==========================================================
# 📈 ЗАХ ЗЭЭЛИЙН ШИНЖИЛГЭЭ (Индикаторууд)
# ==========================================================
def get_klines(symbol, interval="1h", limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = send_public_request("/fapi/v1/klines", params)
    df = pd.DataFrame(data, columns=['time','open','high','low','close','volume','close_time',
                                      'quote_asset_volume','number_of_trades',
                                      'taker_buy_base_asset_volume','taker_buy_quote_asset_volume','ignore'])
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df

def calculate_adx(df, period=14):
    up_move = df['high'].diff()
    down_move = -df['low'].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(window=period).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(window=period).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.rolling(window=period).mean()
    return adx.fillna(0).iloc[-1]

def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50).iloc[-1]

def calculate_atr_pct(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    price = df['close'].iloc[-1]
    return atr / price * 100

def get_avg_volume(df, period=10):
    return df['volume'].iloc[-period:].mean()

def analyze_coin(symbol):
    try:
        df = get_klines(symbol, interval="1h", limit=100)
        adx = calculate_adx(df)
        rsi = calculate_rsi(df)
        atr_pct = calculate_atr_pct(df)
        volume = get_avg_volume(df)
        price = df['close'].iloc[-1]
        
        # Оноо өгөх (Score)
        score = 0
        if 0.3 <= atr_pct <= 1.5:
            score += 30
        if adx > 25:
            score += 30
        elif adx < 18:
            score += 20
        if volume > 1000:
            score += 20
        if 30 < rsi < 70:
            score += 20
        
        # Regime тодорхойлох
        if adx >= 25:
            regime = "TRENDING"
        elif adx <= 18:
            regime = "RANGE-BOUND"
        else:
            regime = "TRANSITIONAL"
            
        return {
            'symbol': symbol,
            'price': price,
            'adx': adx,
            'rsi': rsi,
            'atr_pct': atr_pct,
            'volume': volume,
            'regime': regime,
            'score': score
        }
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

# ==========================================================
# 🧠 СТРАТЕГИЙН СОНГОЛТ
# ==========================================================
def select_strategy(coin_data):
    regime = coin_data['regime']
    if regime == "TRENDING":
        return "EMA_CROSSOVER"
    elif regime == "RANGE-BOUND":
        if coin_data['atr_pct'] > 0.5:
            return "GRID_TRADING"
        else:
            return "BOLLINGER_MEAN_REVERSION"
    else:
        return "HOLD"

# ==========================================================
# 📋 COIN ШАЛГАРУУЛАЛТ
# ==========================================================
def screen_coins():
    print(f"\n🔍 [{datetime.now().strftime('%H:%M')}] Screening coins...")
    results = []
    for sym in SYMBOLS_POOL:
        data = analyze_coin(sym)
        if data:
            results.append(data)
    
    results.sort(key=lambda x: x['score'], reverse=True)
    
    selected = []
    selected_symbols = set()
    for coin in results:
        if coin['symbol'] not in selected_symbols:
            selected.append(coin)
            selected_symbols.add(coin['symbol'])
            if len(selected) >= MAX_SELECTIONS:
                break
    
    print("🏆 Top 5 coins selected:")
    for i, coin in enumerate(selected, 1):
        print(f"   {i}. {coin['symbol']} | Score: {coin['score']} | Regime: {coin['regime']}")
    
    return selected

# ==========================================================
# 💼 ЗАХИАЛГА ГҮЙЦЭТГЭХ (SL + TP)
# ==========================================================
def execute_trades(selected_coins, total_balance):
    for coin_data in selected_coins:
        symbol = coin_data['symbol']
        price = coin_data['price']
        strategy = select_strategy(coin_data)
        
        if strategy == "HOLD":
            print(f"⏸️ {symbol}: Strategy HOLD, skipping trade.")
            continue
        
        side = "BUY"
        allocation_usdt = total_balance * TRADE_ALLOCATION
        quantity = round(allocation_usdt / price, 3)
        
        if quantity < 0.001:
            print(f"⚠️ {symbol}: Quantity too small ({quantity}), skipping.")
            continue
        
        # 1. Хуучин захиалгуудыг цуцлах
        cancel_all_orders(symbol)
        
        # 2. Market BUY захиалга
        print(f"🚀 {symbol}: Opening {side} position | Qty: {quantity} | Strategy: {strategy}")
        order = place_market_order(symbol, side, quantity)
        print(f"   Order Response: {order}")
        
        # 3. Entry price авах
        entry_price = float(order['avgPrice']) if order.get('avgPrice') else price
        
        # 4. SL ба TP үнийг тооцоолох
        sl_price = round(entry_price * (1 - STOP_LOSS_PCT / 100), 2)
        tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT / 100), 2)
        
        # 5. Stop-Loss захиалга (доошоо хамгаалах)
        sl_order = place_stop_loss_order(symbol, "SELL", quantity, sl_price)
        print(f"   ✅ SL placed at ${sl_price} (-{STOP_LOSS_PCT}%)")
        
        # 6. Take-Profit захиалга (дээшээ ашиг түгжих)
        tp_order = place_take_profit_order(symbol, "SELL", quantity, tp_price)
        print(f"   ✅ TP placed at ${tp_price} (+{TAKE_PROFIT_PCT}%)")
        
        # 7. Telegram мэдэгдэл
        msg = (
            f"🟢 *NEW TRADE OPENED*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📌 Coin: `{symbol}`\n"
            f"📊 Strategy: `{strategy}`\n"
            f"📈 Side: `{side}`\n"
            f"💰 Entry: `${entry_price:,.2f}`\n"
            f"🛑 SL: `${sl_price:,.2f}` (-{STOP_LOSS_PCT}%)\n"
            f"🎯 TP: `${tp_price:,.2f}` (+{TAKE_PROFIT_PCT}%)\n"
            f"📦 Qty: `{quantity}` BTC\n"
            f"💵 Alloc: `${allocation_usdt:,.2f}` ({TRADE_ALLOCATION*100}% of balance)\n"
            f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(msg)
        time.sleep(1)

# ==========================================================
# 📊 ПОЗИЦ ХЯНАХ
# ==========================================================
def monitor_positions():
    positions = get_positions()
    if not positions:
        return
    
    msg = f"📊 *POSITION UPDATE ({datetime.now().strftime('%H:%M')})*\n━━━━━━━━━━━━━━━━━\n"
    total_pnl = 0.0
    for pos in positions:
        pnl = pos['unRealizedProfit']
        total_pnl += pnl
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        msg += (
            f"🔹 `{pos['symbol']}`\n"
            f"   Entry: ${pos['entryPrice']:,.2f} | Mark: ${pos['markPrice']:,.2f}\n"
            f"   PnL: `{pnl_str}` | Size: {abs(pos['positionAmt'])} BTC\n\n"
        )
    msg += f"━━━━━━━━━━━━━━━━━\n💵 *Total PnL*: `+${total_pnl:.2f}`" if total_pnl >= 0 else f"`-${abs(total_pnl):.2f}`"
    send_telegram(msg)

# ==========================================================
# 📱 TELEGRAM ХОЛБОО
# ==========================================================
def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"❌ Telegram error: {resp.text}")
    except Exception as e:
        print(f"❌ Telegram send error: {e}")

# ==========================================================
# 🚀 ҮНДСЭН ГОГЦОО
# ==========================================================
def main():
    print("=" * 70)
    print("  💼 PORTFOLIO BOT (5 Coins, 20% Each, SL+TP, Telegram)")
    print("=" * 70)
    print(f"  Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("  Press Ctrl+C to stop.")
    send_telegram("🤖 *Bot Started!* Monitoring portfolio...")
    
    last_selection_time = 0
    selected_coins = []
    
    try:
        while True:
            current_time = time.time()
            total_balance = get_usdt_balance()
            
            if (current_time - last_selection_time) > SELECTION_INTERVAL_HOURS * 3600:
                print(f"\n🔄 Re-selecting coins at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
                selected_coins = screen_coins()
                execute_trades(selected_coins, total_balance)
                last_selection_time = current_time
            
            monitor_positions()
            
            print(f"\n💤 Next monitor in {MONITOR_INTERVAL_SEC}s...")
            time.sleep(MONITOR_INTERVAL_SEC)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Bot stopped by user.")
        send_telegram("🛑 *Bot Stopped*")
    except Exception as e:
        print(f"ERROR: {e}")
        send_telegram(f"❌ *Error*: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
