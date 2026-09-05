"""
indicators.py
Техникийн индикаторууд. Гадаад хамааралгүй, зөвхөн DataFrame авч тооцоолно.
"""
import numpy as np
import pandas as pd


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
    direction.iloc[0] = 1
    supertrend.iloc[0] = lower_band.iloc[0]

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

    return supertrend, direction


def calculate_vwap(df):
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
    return vwap


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
    # avg_loss == 0 with positive gains is a pure uptrend -> RSI 100 (not 50).
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    # Warm-up NaN and a flat series (gain == loss == 0) -> neutral 50.
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
