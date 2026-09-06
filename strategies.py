"""
strategies.py
Зах зээлийн горим тодорхойлох, стратеги тус бүрийн signal ба оноо.
"""
import pandas as pd
from settings import *
import indicators
import market_data
from logging_setup import get_logger

log = get_logger(__name__)


# lastFundingRate is per funding interval (~0.0001 typical, ~0.0075 extreme).
# The previous 0.01 gate was effectively never reached, so funding never moved
# sentiment. 0.05% already marks a crowded side.
FUNDING_SENTIMENT_THRESHOLD = 0.0005


def mtf_from_frames(df_4h, df_1h):
    """Хоёр давтамжийн чиглэл нийлж байгаа эсэх (ЦЭВЭР тооцоо).

    Live нь биржээс 4h лаа татаж, backtest нь 1h-ээс нэгтгэж энд дамжуулна —
    шийдвэрийн логик нь ганц газарт байх тул хоёр зам салахгүй.
    """
    if not MTF_ENABLED:
        return "NEUTRAL"
    if df_4h is None or df_1h is None or len(df_4h) < 20 or len(df_1h) < 20:
        return "NEUTRAL"

    ema_4h = indicators.calculate_ema(df_4h, 50).iloc[-1]
    close_4h = df_4h["close"].iloc[-1]
    ema_1h = indicators.calculate_ema(df_1h, 50).iloc[-1]
    close_1h = df_1h["close"].iloc[-1]

    trend_4h = "BUY" if close_4h > ema_4h else "SELL"
    trend_1h = "BUY" if close_1h > ema_1h else "SELL"

    if trend_4h == "BUY" and trend_1h == "BUY":
        return "BULLISH"
    if trend_4h == "SELL" and trend_1h == "SELL":
        return "BEARISH"
    return "NEUTRAL"


def get_mtf_signal(symbol):
    if not MTF_ENABLED:
        return "NEUTRAL"
    try:
        df_4h = market_data.get_klines(symbol, "4h", 50)
        df_1h = market_data.get_klines(symbol, "1h", 50)
        return mtf_from_frames(df_4h, df_1h)
    except Exception as e:
        log.warning(f"⚠️ MTF error {symbol}: {e}")
        return "NEUTRAL"


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


def generate_strategy_signal(strategy, df, sentiment, regime, chop=None):
    # 1h EMA20/50/200, adx, ema_slope нь зөвхөн TREND_FOLLOWING-д хэрэглэгддэг
    # байсан бөгөөд тэр нь 4h руу шилжсэн тул энд тооцоолохоо больсон —
    # стратеги бүрийн дуудалт тутамд дэмий тооцоо хийхгүй.
    close = df["close"].iloc[-1]
    rsi_series = indicators.calculate_rsi(df)
    rsi = rsi_series.iloc[-1]
    macd, macd_signal, histogram = indicators.calculate_macd(df)
    upper, middle, lower = indicators.calculate_bollinger(df)

    vwap = indicators.calculate_vwap(df).iloc[-1] if VWAP_ENABLED else close

    if strategy == "SUPERTREND":
        st, direction = indicators.calculate_supertrend(df, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
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
        # volume_ratio нь calculate_strategy_score-д ордог ч энд шалгагддаггүй
        # байсан тул histogram огцом хөдлөх мөртлөө эзлэхүүнгүй (noise) мөч ч
        # BUY/SELL болж, зөвхөн оноогоор доогуур байрлаж чаддаг байв —
        # оноо хангалттай өндөр байвал хуурамч signal ч дамжина.
        volume_ratio = indicators.calculate_volume_ratio(df)
        if (histogram.iloc[-1] > 0 and histogram.iloc[-1] > histogram.iloc[-2]
                and rsi < 70 and volume_ratio >= MACD_MIN_VOLUME_RATIO):
            return "BUY"
        if (histogram.iloc[-1] < 0 and histogram.iloc[-1] < histogram.iloc[-2]
                and rsi > 30 and volume_ratio >= MACD_MIN_VOLUME_RATIO):
            return "SELL"

    elif strategy == "BREAKOUT":
        # Bollinger band-аас volume spike-тайгаар цуцарвал continuation гэж
        # үзнэ (fade биш) — өмнөх стратегиуд бүгд extreme дээр буцаад ирнэ гэж
        # бооцоолдог байсан тул range эвдэрч байгаа мөчид ямар ч стратеги
        # ажилладаггүй байв. Volume баталгаагүй хөдөлгөөнийг signal болгохгүй.
        volume_ratio = indicators.calculate_volume_ratio(df)
        if close > upper.iloc[-1] and volume_ratio >= BREAKOUT_VOLUME_RATIO and sentiment >= -0.4:
            return "BUY"
        if close < lower.iloc[-1] and volume_ratio >= BREAKOUT_VOLUME_RATIO and sentiment <= 0.4:
            return "SELL"

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
        # Өндөр давтамжийн (4h) макро тренд. SUPERTREND нь 1h дээр эргэлтийн
        # мөчийг барьдаг тактик стратеги — хоёулаа 1h дээр ажиллавал ижил
        # горимд зэрэг өндөр оноо авч, портфель цэвэр тренд-хазайлттай болдог.
        # 4h дээр шилжүүлснээр хугацааны давхрага үнэхээр өөр болно: EMA20/50/100
        # нь 3–17 хоногийн тренд, харин chop дээр ADX 1h-д 25 орчим (босгонд
        # аюултай ойрхон) байхад 4h-д 17 орчим унадаг.
        htf = indicators.resample_ohlcv(df, TREND_HTF_FACTOR)
        if htf is None or len(htf) < TREND_HTF_SLOW_EMA:
            return "HOLD"
        h_fast = indicators.calculate_ema(htf, TREND_HTF_FAST_EMA)
        h_mid = indicators.calculate_ema(htf, TREND_HTF_MID_EMA)
        h_slow = indicators.calculate_ema(htf, TREND_HTF_SLOW_EMA)
        h_adx = indicators.calculate_adx(htf).iloc[-1]
        h_slope = (h_mid.iloc[-1] - h_mid.iloc[-5]) / h_mid.iloc[-5] * 100

        if (h_adx > TREND_HTF_MIN_ADX
                and h_fast.iloc[-1] > h_mid.iloc[-1] > h_slow.iloc[-1]
                and h_slope > TREND_HTF_MIN_SLOPE and sentiment >= -0.3):
            return "BUY"
        if (h_adx > TREND_HTF_MIN_ADX
                and h_fast.iloc[-1] < h_mid.iloc[-1] < h_slow.iloc[-1]
                and h_slope < -TREND_HTF_MIN_SLOPE and sentiment <= 0.3):
            return "SELL"
    return "HOLD"


def calculate_strategy_score(strategy, adx, rsi, atr_pct, volume_ratio, ema_slope, sentiment, regime, chop, mtf_signal):
    score = 0.0
    
    # MTF унтраалттай үед get_mtf_signal нь үргэлж "NEUTRAL" буцаадаг тул энэ
    # торгууль бүх стратегид ялгаагүй тусч, оноог утгагүйгээр дардаг байв.
    # Торгууль нь зөвхөн MTF асаалттай, чиглэл нь үнэхээр тодорхойгүй үед л утгатай.
    mtf_penalty = -5 if (MTF_ENABLED and mtf_signal == "NEUTRAL") else 0
    
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

    elif strategy == "BREAKOUT":
        # Volume нь энэ стратегийн цөм баталгаа тул хамгийн их жинтэй.
        # Тайван RANGE дотор гарсан breakout нь ихэвчлэн хуурамч (band-ын
        # статистик савалгаа) тул VOLATILE_RANGE/TRANSITION-ыг илүү өндөр үнэлнэ.
        if regime in ["VOLATILE_RANGE", "TRANSITION"]: score += 8
        elif regime in ["TRENDING", "STRONG_TREND"]: score += 4
        score += min(volume_ratio, 5) * 3 + atr_pct * 2 + min(adx, 40) * 0.2
        score += mtf_penalty * 0.5

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
