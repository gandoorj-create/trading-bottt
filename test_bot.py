"""
Pure-function тестүүд: сүлжээ/Binance/Telegram рүү хандахгүй indicator,
signal scoring, тоон helper функцүүдийг шалгана.

Ажиллуулах: pytest суулгаад "pytest test_bot.py -v"
"""
import math

import numpy as np
import pandas as pd
import pytest

import bot


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def make_df(closes, highs=None, lows=None, volumes=None):
    closes = list(closes)
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    volumes = volumes if volumes is not None else [100.0] * n
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def uptrend_df(n=60, start=100.0, step=1.0):
    closes = [start + i * step for i in range(n)]
    return make_df(closes)


def downtrend_df(n=60, start=200.0, step=1.0):
    closes = [start - i * step for i in range(n)]
    return make_df(closes)


def flat_df(n=60, price=100.0):
    return make_df([price] * n)


# ----------------------------------------------------------------
# Тоон helper-үүд
# ----------------------------------------------------------------

class TestSafeFloat:
    def test_valid_number(self):
        assert bot.safe_float("1.5") == 1.5

    def test_invalid_returns_default(self):
        assert bot.safe_float("not-a-number") == 0.0
        assert bot.safe_float(None, default=-1) == -1

    def test_none_input(self):
        assert bot.safe_float(None) == 0.0


class TestClamp:
    def test_within_range(self):
        assert bot.clamp(5, 0, 10) == 5

    def test_below_minimum(self):
        assert bot.clamp(-5, 0, 10) == 0

    def test_above_maximum(self):
        assert bot.clamp(15, 0, 10) == 10


class TestRoundDown:
    def test_truncates_not_rounds(self):
        # 1.2999 -> 1.29 гэж truncate хийнэ, 1.30 руу дугуйлахгүй
        assert bot.round_down(1.2999, 2) == 1.29

    def test_zero_decimals(self):
        assert bot.round_down(7.9, 0) == 7.0

    def test_exact_value_not_reduced_by_float_error(self):
        # 1e-12 epsilon нь 0.1-ийн float representation алдааг нөхнө
        assert bot.round_down(0.1 + 0.2, 1) == 0.3


class TestApiErrorHelpers:
    def test_negative_code_is_error(self):
        assert bot.is_api_error({"code": -1021, "msg": "Timestamp error"}) is True

    def test_positive_or_missing_code_is_not_error(self):
        assert bot.is_api_error({"code": 0}) is False
        assert bot.is_api_error({}) is False

    def test_non_dict_is_not_error(self):
        assert bot.is_api_error(None) is False
        assert bot.is_api_error([1, 2, 3]) is False

    def test_api_error_text_formats_dict(self):
        assert "code" in bot.api_error_text({"code": -1, "msg": "x"})


# ----------------------------------------------------------------
# Indicators
# ----------------------------------------------------------------

class TestEMA:
    def test_flat_series_equals_price(self):
        df = flat_df(price=50.0)
        ema = bot.calculate_ema(df, 10)
        assert ema.iloc[-1] == pytest.approx(50.0)

    def test_uptrend_ema_below_last_close(self):
        df = uptrend_df()
        ema = bot.calculate_ema(df, 20)
        assert ema.iloc[-1] < df["close"].iloc[-1]


class TestRSI:
    def test_bounded_between_0_and_100(self):
        df = uptrend_df()
        rsi = bot.calculate_rsi(df)
        assert rsi.between(0, 100).all()

    def test_pure_uptrend_is_overbought(self):
        df = uptrend_df(n=40)
        rsi = bot.calculate_rsi(df).iloc[-1]
        assert rsi == pytest.approx(100.0)

    def test_pure_downtrend_is_oversold(self):
        df = downtrend_df(n=40)
        rsi = bot.calculate_rsi(df).iloc[-1]
        assert rsi < 5

    def test_flat_series_is_neutral(self):
        df = flat_df()
        rsi = bot.calculate_rsi(df).iloc[-1]
        assert rsi == pytest.approx(50.0)


class TestATR:
    def test_non_negative(self):
        df = uptrend_df()
        atr = bot.calculate_atr(df)
        assert (atr.fillna(0) >= 0).all()

    def test_flat_series_has_small_atr(self):
        df = flat_df()
        atr = bot.calculate_atr(df).iloc[-1]
        # high/low нь close-оос ±1% байгаа тул ATR тэгээс их ч жижиг байх ёстой
        assert 0 < atr < 5


class TestMACD:
    def test_uptrend_histogram_positive(self):
        df = uptrend_df(n=80)
        macd, signal, hist = bot.calculate_macd(df)
        assert hist.iloc[-1] > 0

    def test_downtrend_histogram_negative(self):
        df = downtrend_df(n=80)
        macd, signal, hist = bot.calculate_macd(df)
        assert hist.iloc[-1] < 0


class TestBollinger:
    def test_upper_above_lower(self):
        df = uptrend_df()
        upper, middle, lower = bot.calculate_bollinger(df)
        assert (upper.iloc[-1] > middle.iloc[-1] > lower.iloc[-1])

    def test_flat_series_bands_collapse(self):
        df = flat_df()
        upper, middle, lower = bot.calculate_bollinger(df)
        assert upper.iloc[-1] == pytest.approx(lower.iloc[-1])


class TestChop:
    def test_bounded_range(self):
        df = uptrend_df()
        chop = bot.calculate_chop(df)
        assert chop.between(0, 100).all()

    def test_strong_trend_has_low_chop(self):
        df = uptrend_df(n=60, step=2.0)
        chop = bot.calculate_chop(df).iloc[-1]
        assert chop < 61.8


class TestSupertrend:
    def test_returns_direction_series_of_1_or_minus_1(self):
        df = uptrend_df()
        st, direction = bot.calculate_supertrend(df)
        assert set(direction.dropna().unique()).issubset({1, -1})

    def test_strong_uptrend_ends_bullish(self):
        df = uptrend_df(n=60, step=3.0)
        st, direction = bot.calculate_supertrend(df)
        assert direction.iloc[-1] == 1

    def test_strong_downtrend_ends_bearish(self):
        df = downtrend_df(n=60, step=3.0)
        st, direction = bot.calculate_supertrend(df)
        assert direction.iloc[-1] == -1


class TestVWAP:
    def test_flat_series_equals_price(self):
        df = flat_df(price=42.0)
        vwap = bot.calculate_vwap(df)
        assert vwap.iloc[-1] == pytest.approx(42.0)

    def test_uptrend_vwap_below_last_close(self):
        df = uptrend_df()
        vwap = bot.calculate_vwap(df)
        assert vwap.iloc[-1] < df["close"].iloc[-1]


class TestVolumeRatio:
    def test_equal_volume_ratio_is_one(self):
        df = flat_df()
        assert bot.calculate_volume_ratio(df) == pytest.approx(1.0)

    def test_spike_above_average_is_greater_than_one(self):
        df = flat_df(n=30)
        df.loc[df.index[-1], "volume"] = 1000.0
        assert bot.calculate_volume_ratio(df) > 1.0

    def test_zero_average_returns_default(self):
        df = flat_df(n=25, price=10.0)
        df["volume"] = 0.0
        assert bot.calculate_volume_ratio(df) == 1.0


# ----------------------------------------------------------------
# Regime + Strategy scoring
# ----------------------------------------------------------------

class TestDetermineRegime:
    def test_low_chop_strong_slope_is_strong_trend(self):
        assert bot.determine_regime(chop=30, adx=40, ema_slope=1.5, atr_pct=1.0) == "STRONG_TREND"

    def test_low_chop_weak_slope_is_trending(self):
        assert bot.determine_regime(chop=30, adx=25, ema_slope=0.2, atr_pct=1.0) == "TRENDING"

    def test_high_chop_high_atr_is_volatile_range(self):
        assert bot.determine_regime(chop=70, adx=15, ema_slope=0.1, atr_pct=1.0) == "VOLATILE_RANGE"

    def test_high_chop_low_atr_is_range(self):
        assert bot.determine_regime(chop=70, adx=15, ema_slope=0.1, atr_pct=0.1) == "RANGE"

    def test_mid_chop_is_transition(self):
        assert bot.determine_regime(chop=50, adx=20, ema_slope=0.1, atr_pct=0.5) == "TRANSITION"

    def test_nan_chop_falls_back_to_adx_atr(self):
        assert bot.determine_regime(chop=float("nan"), adx=35, ema_slope=1.0, atr_pct=1.0) == "STRONG_TREND"


class TestCalculateStrategyScore:
    @pytest.mark.parametrize("strategy", [
        "SUPERTREND", "MACD_MOMENTUM", "GRID_TRADING",
        "BOLLINGER_MEAN_REVERSION", "RSI_STRATEGY", "TREND_FOLLOWING",
    ])
    def test_score_never_negative(self, strategy):
        score = bot.calculate_strategy_score(
            strategy, adx=10, rsi=50, atr_pct=0.1, volume_ratio=0.5,
            ema_slope=0.0, sentiment=-1.0, regime="RANGE", chop=70, mtf_signal="NEUTRAL",
        )
        assert score >= 0

    def test_supertrend_prefers_trending_regime(self):
        base_kwargs = dict(adx=30, rsi=50, atr_pct=1.0, volume_ratio=1.5,
                            ema_slope=1.0, sentiment=0.0, chop=30, mtf_signal="BULLISH")
        trending_score = bot.calculate_strategy_score("SUPERTREND", regime="TRENDING", **base_kwargs)
        range_score = bot.calculate_strategy_score("SUPERTREND", regime="RANGE", **base_kwargs)
        assert trending_score > range_score

    def test_grid_trading_prefers_range_regime(self):
        base_kwargs = dict(adx=10, rsi=50, atr_pct=1.0, volume_ratio=1.0,
                            ema_slope=0.0, sentiment=0.0, chop=70, mtf_signal="NEUTRAL")
        range_score = bot.calculate_strategy_score("GRID_TRADING", regime="RANGE", **base_kwargs)
        trending_score = bot.calculate_strategy_score("GRID_TRADING", regime="TRENDING", **base_kwargs)
        assert range_score > trending_score
