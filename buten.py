import hashlib
import hmac
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import traceback

# ==========================================================
# 🔑 API БОЛОН TELEGRAM ТОХИРГОО
# ==========================================================
API_KEY = "tyRDudce0UlVVEA9jqLRbiHulMGlCtzIMsBQqduZtrARuxFhHgJJVuoYk7l3TvrG"
API_SECRET = "4NuMPGZhbsMfAerDIQeyBV0vR1v7aOuwSh8tm3RrQUPm1HkUNf1DQB98neXutUKX"
BASE_URL = "https://demo-fapi.binance.com"

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

SELECTION_INTERVAL_MINUTES = 360       # 6 ЦАГ
MONITOR_INTERVAL_SEC = 60              # 1 минут
TELEGRAM_REPORT_INTERVAL_SEC = 300     # 5 МИНУТ

# ✅ 6 койн, тус бүрд 16.67% (нийт 100%)
TRADE_ALLOCATION = 0.1667              
STOP_LOSS_PCT = 2.0                 
TAKE_PROFIT_PCT = 1.0               
MAX_SELECTIONS = 6                   # 6 койн сонгох

# ==========================================================
# 🔐 ГАРЫН ҮСЭГ (өмнөхтэй адил, богиносгосон)
# ==========================================================
def get_signature(params_str, secret):
    return hmac.new(secret.encode('utf-8'), params_str.encode('utf-8'), hashlib.sha256).hexdigest()

def send_signed_request(method, endpoint, params=None):
    timestamp = int(time.time() * 1000)
    if params is None: params = {}
    params['timestamp'] = timestamp
    params['recvWindow'] = 5000
    query_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = get_signature(query_str, API_SECRET)
    url = f"{BASE_URL}{endpoint}?{query_str}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    if method.upper() == "GET": resp = requests.get(url, headers=headers)
    elif method.upper() == "POST": resp = requests.post(url, headers=headers)
    elif method.upper() == "DELETE": resp = requests.delete(url, headers=headers)
    else: raise ValueError("Unsupported method")
    return resp.json()

def send_public_request(endpoint, params=None):
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, params=params)
    return resp.json()

# ==========================================================
# 💰 ҮЛДЭГДЭЛ, ПОЗИЦ, ЗАХИАЛГА
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
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity}
    return send_signed_request("POST", "/fapi/v1/order", params)

def place_stop_loss_order(symbol, side, quantity, stop_price):
    params = {
        "symbol": symbol, "side": side, "type": "STOP_MARKET",
        "stopPrice": stop_price, "quantity": quantity, "workingType": "MARK_PRICE"
    }
    return send_signed_request("POST", "/fapi/v1/order", params)

def place_take_profit_order(symbol, side, quantity, tp_price):
    params = {
        "symbol": symbol, "side": side, "type": "LIMIT",
        "price": tp_price, "quantity": quantity, "timeInForce": "GTC"
    }
    return send_signed_request("POST", "/fapi/v1/order", params)

def cancel_all_orders(symbol):
    params = {"symbol": symbol}
    return send_signed_request("DELETE", "/fapi/v1/allOpenOrders", params)

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

def calculate_ema_slope(df, period=50):
    ema = df['close'].ewm(span=period).mean()
    slope = (ema.iloc[-1] - ema.iloc[-5]) / ema.iloc[-5] * 100
    return slope

def analyze_coin(symbol):
    try:
        df = get_klines(symbol, interval="1h", limit=100)
        adx = calculate_adx(df)
        rsi = calculate_rsi(df)
        atr_pct = calculate_atr_pct(df)
        volume = get_avg_volume(df)
        price = df['close'].iloc[-1]
        ema_slope = calculate_ema_slope(df)
        
        # Стратегийн тохирлыг тодорхойлох
        strategy_suitability = {}
        
        # 1. EMA CROSSOVER - Тренд тодорхой (ADX > 25)
        if adx > 25:
            strategy_suitability['EMA_CROSSOVER'] = adx * 2 + (ema_slope * 5 if ema_slope > 0 else 0)
        
        # 2. MACD MOMENTUM - Тренд сул (ADX 20-25)
        if 20 <= adx <= 25:
            strategy_suitability['MACD_MOMENTUM'] = (adx - 15) * 3 + (atr_pct * 10 if atr_pct > 0.3 else 0)
        
        # 3. GRID TRADING - RANGE-BOUND (ADX < 18) + хэлбэлзэл > 0.3%
        if adx < 18 and atr_pct > 0.3:
            strategy_suitability['GRID_TRADING'] = (18 - adx) * 2 + atr_pct * 20
        
        # 4. BOLLINGER MEAN REVERSION - RANGE-BOUND + хэлбэлзэл > 0.5%
        if adx < 18 and atr_pct > 0.5:
            strategy_suitability['BOLLINGER_MEAN_REVERSION'] = (18 - adx) * 1.5 + atr_pct * 15
        
        # 5. RSI STRATEGY - RSI хэт туйлширсан (< 30 эсвэл > 70)
        if rsi < 35 or rsi > 65:
            rsi_score = (35 - rsi) * 3 if rsi < 35 else (rsi - 65) * 3
            strategy_suitability['RSI_STRATEGY'] = rsi_score + atr_pct * 10
        
        # 6. TREND FOLLOWING - Хүчтэй тренд (ADX > 30) + EMA налуу
        if adx > 30 and abs(ema_slope) > 0.5:
            strategy_suitability['TREND_FOLLOWING'] = adx * 1.5 + abs(ema_slope) * 10
        
        # Хамгийн тохирсон стратегийг сонгох
        if strategy_suitability:
            best_strategy = max(strategy_suitability, key=strategy_suitability.get)
            best_score = strategy_suitability[best_strategy]
        else:
            best_strategy = "HOLD"
            best_score = 0
        
        return {
            'symbol': symbol,
            'price': price,
            'adx': adx,
            'rsi': rsi,
            'atr_pct': atr_pct,
            'volume': volume,
            'regime': "TRENDING" if adx > 25 else "RANGE-BOUND" if adx < 18 else "TRANSITIONAL",
            'score': best_score,
            'strategy': best_strategy
        }
    except Exception as e:
        print(f"❌ analyze_coin error for {symbol}: {e}")
        return None

def screen_coins_by_strategy():
    """
    Стратеги тус бүрд хамгийн тохирох 1 койныг сонгоно.
    Нийт 6 стратеги, 6 койн буцаана.
    """
    print(f"\n🔍 [{datetime.now().strftime('%H:%M:%S')}] Стратеги тус бүрт койн шинжилж байна...")
    
    # 1. Бүх койнд шинжилгээ хийх
    all_results = []
    for sym in SYMBOLS_POOL:
        data = analyze_coin(sym)
        if data:
            all_results.append(data)
    
    # 2. Стратеги тус бүрд хамгийн өндөр оноотой койныг сонгох
    selected = []
    used_symbols = set()
    strategy_list = ["EMA_CROSSOVER", "MACD_MOMENTUM", "GRID_TRADING", 
                     "BOLLINGER_MEAN_REVERSION", "RSI_STRATEGY", "TREND_FOLLOWING"]
    
    for strategy in strategy_list:
        # Тухайн стратегид тохирох койнуудыг шүүх
        candidates = [c for c in all_results if c['strategy'] == strategy and c['symbol'] not in used_symbols]
        if candidates:
            # Хамгийн өндөр оноотойг сонгох
            best = max(candidates, key=lambda x: x['score'])
            selected.append(best)
            used_symbols.add(best['symbol'])
        else:
            # Хэрэв тухайн стратегид тохирох койн байхгүй бол хамгийн өндөр оноотой ерөнхий койныг сонгох
            remaining = [c for c in all_results if c['symbol'] not in used_symbols]
            if remaining:
                best = max(remaining, key=lambda x: x['score'])
                # Стратегийг нь хүчээр оноох
                best['strategy'] = strategy
                selected.append(best)
                used_symbols.add(best['symbol'])
    
    print("🏆 Стратеги тус бүрт сонгогдсон койнууд:")
    for i, coin in enumerate(selected, 1):
        print(f"   {i}. {coin['symbol']} | Стратеги: {coin['strategy']} | Оноо: {coin['score']:.1f}")
    
    return selected

# ==========================================================
# 📱 TELEGRAM ХОЛБОО
# ==========================================================
def send_telegram(text, pin=False):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ Telegram мэдэгдэл илгээгдлээ.")
            if pin:
                result = resp.json()
                if result.get('ok'):
                    message_id = result['result']['message_id']
                    pin_url = f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage"
                    pin_payload = {"chat_id": CHAT_ID, "message_id": message_id}
                    pin_resp = requests.post(pin_url, json=pin_payload, timeout=10)
                    if pin_resp.status_code == 200:
                        print("📌 Зурвас бэхлэгдлээ.")
                    else:
                        print(f"❌ Бэхлэхэд алдаа: {pin_resp.text}")
                else:
                    print(f"❌ Илгээхэд алдаа: {result}")
        else:
            print(f"❌ Telegram алдаа: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ Telegram илгээхэд алдаа: {e}")

# ==========================================================
# 💼 ЗАХИАЛГА ГҮЙЦЭТГЭХ (6 КОЙН, 16.67% ТУС БҮР)
# ==========================================================
def execute_trades(selected_coins, total_balance):
    if not selected_coins:
        send_telegram("⚠️ *КОЙН СОНГОГДООГҮЙ*\n━━━━━━━━━━━━━━━━━\nКойн шинжилгээ амжилтгүй боллоо.")
        return

    any_trade_opened = False
    for coin_data in selected_coins:
        symbol = coin_data['symbol']
        price = coin_data['price']
        strategy = coin_data['strategy']
        
        if strategy == "HOLD":
            print(f"⏸️ {symbol}: ХҮЛЭЭХ төлөв")
            continue

        if total_balance < 10:
            send_telegram(f"⚠️ *ҮЛДЭГДЭЛ ХАНГАЛТГҮЙ*\n━━━━━━━━━━━━━━━━━\nUSDT: `${total_balance:.2f}`\nХамгийн багадаа $10 байх ёстой.")
            return

        # ✅ 6 койн тул 16.67%
        allocation_usdt = total_balance * TRADE_ALLOCATION
        quantity = round(allocation_usdt / price, 3)

        if quantity < 0.001:
            print(f"⚠️ {symbol}: Хэмжээ хэтэрхий бага ({quantity})")
            continue

        any_trade_opened = True
        cancel_all_orders(symbol)

        print(f"🚀 {symbol}: {strategy} стратегиар нээж байна | Хэмжээ: {quantity}")
        order = place_market_order(symbol, "BUY", quantity)

        entry_price = float(order['avgPrice']) if order.get('avgPrice') else price
        sl_price = round(entry_price * (1 - STOP_LOSS_PCT / 100), 2)
        tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT / 100), 2)

        place_stop_loss_order(symbol, "SELL", quantity, sl_price)
        place_take_profit_order(symbol, "SELL", quantity, tp_price)

        msg = (f"🟢 *ШИНЭ АРИЛЖАА НЭЭГДЛЭЭ*\n━━━━━━━━━━━━━━━━━\n"
               f"📌 Койн: `{symbol}`\n📊 Стратеги: `{strategy}`\n📈 Чиглэл: `ХУДАЛДАН АВАХ`\n"
               f"💰 Нээсэн үнэ: `${entry_price:,.2f}`\n🛑 Алдагдал хязгаар (SL): `${sl_price:,.2f}` (-{STOP_LOSS_PCT}%)\n"
               f"🎯 Ашиг түгжих (TP): `${tp_price:,.2f}` (+{TAKE_PROFIT_PCT}%)\n"
               f"📦 Хэмжээ: `{quantity}` {symbol.replace('USDT','')}\n"
               f"💵 Зарцуулсан: `${allocation_usdt:,.2f}` ({TRADE_ALLOCATION*100:.2f}% of balance)\n"
               f"⏰ Цаг: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        send_telegram(msg)
        time.sleep(1)

    if not any_trade_opened:
        send_telegram("⏳ *АРИЛЖАА ХИЙГДЭЭГҮЙ*\n━━━━━━━━━━━━━━━━━\nСонгогдсон бүх койн `HOLD` төлөвтэй эсвэл хэмжээ хэтэрхий бага байна.")

# ==========================================================
# 📊 ПОЗИЦ ХЯНАХ (5 МИНУТ ТУТАМ)
# ==========================================================
last_telegram_report_time = 0

def monitor_positions():
    global last_telegram_report_time
    positions = get_positions()
    if not positions: return

    current_time = time.time()
    if (current_time - last_telegram_report_time) > TELEGRAM_REPORT_INTERVAL_SEC:
        msg = f"📊 *ПОЗИЦЫН МЭДЭЭЛЭЛ ({datetime.now().strftime('%H:%M')})*\n━━━━━━━━━━━━━━━━━\n"
        total_pnl = 0.0
        for pos in positions:
            pnl = pos['unRealizedProfit']; total_pnl += pnl
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            msg += (f"🔹 `{pos['symbol']}`\n"
                    f"   Нээсэн үнэ: ${pos['entryPrice']:,.2f} | Одоогийн үнэ: ${pos['markPrice']:,.2f}\n"
                    f"   Ашиг/Алдагдал: `{pnl_str}` | Хэмжээ: {abs(pos['positionAmt'])} BTC\n\n")
        msg += f"━━━━━━━━━━━━━━━━━\n💵 *НИЙТ АШИГ/АЛДАГДАЛ*: `+${total_pnl:.2f}`" if total_pnl >= 0 else f"`-${abs(total_pnl):.2f}`"
        send_telegram(msg)
        last_telegram_report_time = current_time

# ==========================================================
# 🔄 ЦИКЛИЙН ХУРААНГУЙ (6 ЦАГ ТУТАМ)
# ==========================================================
cycle_start_time = time.time()
cycle_realized_pnl = 0.0
last_balance = 0.0

def send_cycle_summary():
    global cycle_start_time, cycle_realized_pnl, last_balance
    current_balance = get_usdt_balance()
    realized_pnl = current_balance - last_balance
    cycle_realized_pnl += realized_pnl

    msg = (f"📆 *6 ЦАГИЙН ЦИКЛИЙН ХУРААНГУЙ*\n"
           f"━━━━━━━━━━━━━━━━━\n"
           f"⏰ Хугацаа: {datetime.fromtimestamp(cycle_start_time).strftime('%H:%M:%S')} - {datetime.now().strftime('%H:%M:%S')}\n"
           f"💰 Бодит ашиг/алдагдал: `+${cycle_realized_pnl:.2f}`" if cycle_realized_pnl >= 0 else f"`-${abs(cycle_realized_pnl):.2f}`")
    send_telegram(msg, pin=True)

    cycle_start_time = time.time()
    cycle_realized_pnl = 0.0
    last_balance = current_balance

# ==========================================================
# 🚀 ҮНДСЭН ГОГЦОО
# ==========================================================
def main():
    global last_telegram_report_time, cycle_start_time, cycle_realized_pnl, last_balance

    print("=" * 70)
    print(f"  💼 ПОРТФОЛИЙН БОТ (6 стратеги, 6 койн, 16.67% тус бүр)")
    print("=" * 70)
    print(f"  Эхлэх цаг: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    send_telegram(f"🤖 *БОТ АСЛАА!* (6 стратеги, 6 койн, 16.67% тус бүр)", pin=False)

    # ==========================================================
    # БОТ АСМАГЦ ШУУД КОЙН ШИНЖИЛГЭЭ + АРИЛЖАА
    # ==========================================================
    print("\n🚀 Анхны койн шинжилгээ хийж байна...")
    try:
        selected_coins = screen_coins_by_strategy()
        if selected_coins:
            coin_list = "\n".join([f"   {i+1}. {c['symbol']} | Стратеги: {c['strategy']} | Оноо: {c['score']:.1f}" for i, c in enumerate(selected_coins)])
            msg = f"📋 *ШИНЭ ЗООСОН КОЙНУУД (ЭХЛЭЛ)*\n━━━━━━━━━━━━━━━━━\n{coin_list}"
            send_telegram(msg, pin=True)
        else:
            send_telegram("⚠️ *КОЙН СОНГОЛТ АМЖИЛТГҮЙ*\n━━━━━━━━━━━━━━━━━\nAPI-д холбогдоход асуудал гарсан байна.", pin=False)
        
        total_balance = get_usdt_balance()
        execute_trades(selected_coins, total_balance)
    except Exception as e:
        print(f"❌ Анхны койн шинжилгээний алдаа: {e}")
        send_telegram(f"❌ *АНХНЫ АЛДАА*\n━━━━━━━━━━━━━━━━━\n{e}", pin=False)

    last_selection_time = time.time()
    last_balance = get_usdt_balance()
    cycle_start_time = time.time()
    cycle_count = 0

    # ==========================================================
    # 🔄 ҮНДСЭН ГОГЦОО (6 ЦАГ ТУТАМ)
    # ==========================================================
    while True:
        try:
            current_time = time.time()
            elapsed = current_time - last_selection_time

            if elapsed >= SELECTION_INTERVAL_MINUTES * 60:
                cycle_count += 1
                print(f"\n🔄 {cycle_count}-р ЦИКЛ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}-д эхэллээ")

                try:
                    send_cycle_summary()
                except Exception as e:
                    print(f"❌ Циклийн хураангуй алдаа: {e}")

                try:
                    selected_coins = screen_coins_by_strategy()
                except Exception as e:
                    print(f"❌ Койн шинжилгээний алдаа: {e}")
                    selected_coins = []

                if selected_coins:
                    coin_list = "\n".join([f"   {i+1}. {c['symbol']} | Стратеги: {c['strategy']} | Оноо: {c['score']:.1f}" for i, c in enumerate(selected_coins)])
                    msg = f"📋 *ШИНЭ ЗООСОН КОЙНУУД*\n━━━━━━━━━━━━━━━━━\n{coin_list}"
                    send_telegram(msg, pin=True)
                else:
                    send_telegram("⚠️ *КОЙН СОНГОЛТ АМЖИЛТГҮЙ*\n━━━━━━━━━━━━━━━━━\nAPI-д холбогдоход асуудал гарсан байна.", pin=False)

                try:
                    total_balance = get_usdt_balance()
                    execute_trades(selected_coins, total_balance)
                except Exception as e:
                    print(f"❌ Арилжаа нээхэд алдаа: {e}")
                    send_telegram(f"❌ *АРИЛЖААНЫ АЛДАА*\n━━━━━━━━━━━━━━━━━\n{e}")

                last_selection_time = current_time
                last_balance = total_balance if 'total_balance' in locals() else get_usdt_balance()
                print(f"✅ {cycle_count}-р цикл дууслаа. Дараагийн цикл {SELECTION_INTERVAL_MINUTES} минутын дараа.")

            try:
                monitor_positions()
            except Exception as e:
                print(f"❌ Позиц хянах алдаа: {e}")

            time.sleep(MONITOR_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("\n🛑 Ботыг хэрэглэгч зогсоосон.")
            send_telegram("🛑 *БОТ ЗОГСООСОН*", pin=False)
            break
        except Exception as main_error:
            error_msg = f"❌ *ГОЛ АЛДАА*\n━━━━━━━━━━━━━━━━━\n{traceback.format_exc()}"
            print(error_msg)
            try:
                send_telegram(error_msg[:4000], pin=False)
            except:
                pass
            time.sleep(30)

if __name__ == "__main__":
    main()
