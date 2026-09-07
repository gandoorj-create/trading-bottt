"""
bot.py-ийн тестүүд.

Сүлжээ рүү огт хандахгүй: conftest.py дэх autouse fixture-үүд requests-ийг
блоклож, Telegram илгээлтийг мок болгож, state файлуудыг tmp директорт чиглүүлж,
global state-ийг тест бүрийн өмнө цэвэрлэдэг.

Ажиллуулах:
    pip install -r requirements-dev.txt
    pytest -v
"""
import csv
from datetime import datetime, timedelta
import json

import pandas as pd
import pytest
import pytz

import time

import account
import binance_client
import execution
import indicators
import journal
import market_data
import news
import order_api
import persistence
import position_manager
import reports
import risk
import screening
import strategies
import utils
from state import STRATEGY_NAMES, state as bot_state
from conftest import patch_setting


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


def noisy_uptrend_df(n=260, start=100.0, step=1.0):
    """EMA-200 хүртэл хангалттай урт, бага зэрэг чичиргээтэй өсөх trend."""
    closes = [start + i * step + (1.5 if i % 3 == 0 else -1.0) for i in range(n)]
    return make_df(closes)


class FakeResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}


# ----------------------------------------------------------------
# Тоон helper-үүд
# ----------------------------------------------------------------

class TestSafeFloat:
    def test_valid_number(self):
        assert utils.safe_float("1.5") == 1.5

    def test_invalid_returns_default(self):
        assert utils.safe_float("not-a-number") == 0.0
        assert utils.safe_float(None, default=-1) == -1

    def test_none_input(self):
        assert utils.safe_float(None) == 0.0


class TestClamp:
    def test_within_range(self):
        assert utils.clamp(5, 0, 10) == 5

    def test_below_minimum(self):
        assert utils.clamp(-5, 0, 10) == 0

    def test_above_maximum(self):
        assert utils.clamp(15, 0, 10) == 10


class TestRoundDown:
    def test_truncates_not_rounds(self):
        # 1.2999 -> 1.29 гэж truncate хийнэ, 1.30 руу дугуйлахгүй
        assert utils.round_down(1.2999, 2) == 1.29

    def test_zero_decimals(self):
        assert utils.round_down(7.9, 0) == 7.0

    def test_exact_value_not_reduced_by_float_error(self):
        # 1e-12 epsilon нь 0.1-ийн float representation алдааг нөхнө
        assert utils.round_down(0.1 + 0.2, 1) == 0.3


class TestApiErrorHelpers:
    def test_negative_code_is_error(self):
        assert utils.is_api_error({"code": -1021, "msg": "Timestamp error"}) is True

    def test_positive_or_missing_code_is_not_error(self):
        assert utils.is_api_error({"code": 0}) is False
        assert utils.is_api_error({}) is False

    def test_non_dict_is_not_error(self):
        assert utils.is_api_error(None) is False
        assert utils.is_api_error([1, 2, 3]) is False

    def test_api_error_text_formats_dict(self):
        assert "code" in utils.api_error_text({"code": -1, "msg": "x"})


# ----------------------------------------------------------------
# Indicators
# ----------------------------------------------------------------

class TestEMA:
    def test_flat_series_equals_price(self):
        df = flat_df(price=50.0)
        ema = indicators.calculate_ema(df, 10)
        assert ema.iloc[-1] == pytest.approx(50.0)

    def test_uptrend_ema_below_last_close(self):
        df = uptrend_df()
        ema = indicators.calculate_ema(df, 20)
        assert ema.iloc[-1] < df["close"].iloc[-1]


class TestRSI:
    def test_bounded_between_0_and_100(self):
        df = uptrend_df()
        rsi = indicators.calculate_rsi(df)
        assert rsi.between(0, 100).all()

    def test_pure_uptrend_is_overbought(self):
        df = uptrend_df(n=40)
        rsi = indicators.calculate_rsi(df).iloc[-1]
        assert rsi == pytest.approx(100.0)

    def test_pure_downtrend_is_oversold(self):
        df = downtrend_df(n=40)
        rsi = indicators.calculate_rsi(df).iloc[-1]
        assert rsi < 5

    def test_flat_series_is_neutral(self):
        df = flat_df()
        rsi = indicators.calculate_rsi(df).iloc[-1]
        assert rsi == pytest.approx(50.0)


class TestATR:
    def test_non_negative(self):
        df = uptrend_df()
        atr = indicators.calculate_atr(df)
        assert (atr.fillna(0) >= 0).all()

    def test_flat_series_has_small_atr(self):
        df = flat_df()
        atr = indicators.calculate_atr(df).iloc[-1]
        # high/low нь close-оос ±1% байгаа тул ATR тэгээс их ч жижиг байх ёстой
        assert 0 < atr < 5


class TestMACD:
    def test_uptrend_histogram_positive(self):
        df = uptrend_df(n=80)
        macd, signal, hist = indicators.calculate_macd(df)
        assert hist.iloc[-1] > 0

    def test_downtrend_histogram_negative(self):
        df = downtrend_df(n=80)
        macd, signal, hist = indicators.calculate_macd(df)
        assert hist.iloc[-1] < 0


class TestBollinger:
    def test_upper_above_lower(self):
        df = uptrend_df()
        upper, middle, lower = indicators.calculate_bollinger(df)
        assert (upper.iloc[-1] > middle.iloc[-1] > lower.iloc[-1])

    def test_flat_series_bands_collapse(self):
        df = flat_df()
        upper, middle, lower = indicators.calculate_bollinger(df)
        assert upper.iloc[-1] == pytest.approx(lower.iloc[-1])


class TestChop:
    def test_bounded_range(self):
        df = uptrend_df()
        chop = indicators.calculate_chop(df)
        assert chop.between(0, 100).all()

    def test_strong_trend_has_low_chop(self):
        df = uptrend_df(n=60, step=2.0)
        chop = indicators.calculate_chop(df).iloc[-1]
        assert chop < 61.8


class TestSupertrend:
    def test_returns_direction_series_of_1_or_minus_1(self):
        df = uptrend_df()
        st, direction = indicators.calculate_supertrend(df)
        assert set(direction.dropna().unique()).issubset({1, -1})

    def test_strong_uptrend_ends_bullish(self):
        df = uptrend_df(n=60, step=3.0)
        st, direction = indicators.calculate_supertrend(df)
        assert direction.iloc[-1] == 1

    def test_strong_downtrend_ends_bearish(self):
        df = downtrend_df(n=60, step=3.0)
        st, direction = indicators.calculate_supertrend(df)
        assert direction.iloc[-1] == -1


class TestVWAP:
    def test_flat_series_equals_price(self):
        df = flat_df(price=42.0)
        vwap = indicators.calculate_vwap(df)
        assert vwap.iloc[-1] == pytest.approx(42.0)

    def test_uptrend_vwap_below_last_close(self):
        df = uptrend_df()
        vwap = indicators.calculate_vwap(df)
        assert vwap.iloc[-1] < df["close"].iloc[-1]


class TestVolumeRatio:
    def test_equal_volume_ratio_is_one(self):
        df = flat_df()
        assert indicators.calculate_volume_ratio(df) == pytest.approx(1.0)

    def test_spike_above_average_is_greater_than_one(self):
        df = flat_df(n=30)
        df.loc[df.index[-1], "volume"] = 1000.0
        assert indicators.calculate_volume_ratio(df) > 1.0

    def test_zero_average_returns_default(self):
        df = flat_df(n=25, price=10.0)
        df["volume"] = 0.0
        assert indicators.calculate_volume_ratio(df) == 1.0


# ----------------------------------------------------------------
# Regime + Strategy scoring
# ----------------------------------------------------------------

class TestDetermineRegime:
    def test_low_chop_strong_slope_is_strong_trend(self):
        assert strategies.determine_regime(chop=30, adx=40, ema_slope=1.5, atr_pct=1.0) == "STRONG_TREND"

    def test_low_chop_weak_slope_is_trending(self):
        assert strategies.determine_regime(chop=30, adx=25, ema_slope=0.2, atr_pct=1.0) == "TRENDING"

    def test_high_chop_high_atr_is_volatile_range(self):
        assert strategies.determine_regime(chop=70, adx=15, ema_slope=0.1, atr_pct=1.0) == "VOLATILE_RANGE"

    def test_high_chop_low_atr_is_range(self):
        assert strategies.determine_regime(chop=70, adx=15, ema_slope=0.1, atr_pct=0.1) == "RANGE"

    def test_mid_chop_is_transition(self):
        assert strategies.determine_regime(chop=50, adx=20, ema_slope=0.1, atr_pct=0.5) == "TRANSITION"

    def test_nan_chop_falls_back_to_adx_atr(self):
        assert strategies.determine_regime(chop=float("nan"), adx=35, ema_slope=1.0, atr_pct=1.0) == "STRONG_TREND"


class TestCalculateStrategyScore:
    @pytest.mark.parametrize("strategy", [
        "SUPERTREND", "MACD_MOMENTUM", "BREAKOUT",
        "BOLLINGER_MEAN_REVERSION", "RSI_STRATEGY", "TREND_FOLLOWING",
    ])
    def test_score_never_negative(self, strategy):
        score = strategies.calculate_strategy_score(
            strategy, adx=10, rsi=50, atr_pct=0.1, volume_ratio=0.5,
            ema_slope=0.0, sentiment=-1.0, regime="RANGE", chop=70, mtf_signal="NEUTRAL",
        )
        assert score >= 0

    def test_supertrend_prefers_trending_regime(self):
        base_kwargs = dict(adx=30, rsi=50, atr_pct=1.0, volume_ratio=1.5,
                            ema_slope=1.0, sentiment=0.0, chop=30, mtf_signal="BULLISH")
        trending_score = strategies.calculate_strategy_score("SUPERTREND", regime="TRENDING", **base_kwargs)
        range_score = strategies.calculate_strategy_score("SUPERTREND", regime="RANGE", **base_kwargs)
        assert trending_score > range_score

    def test_breakout_prefers_volatile_regime_over_calm_range(self):
        # Тайван RANGE дотор гарсан band-ын статистик савалгаа ихэвчлэн
        # хуурамч байдаг тул VOLATILE_RANGE/TRANSITION-ыг илүү өндөр үнэлнэ
        base_kwargs = dict(adx=20, rsi=50, atr_pct=1.0, volume_ratio=2.0,
                            ema_slope=0.0, sentiment=0.0, chop=50, mtf_signal="NEUTRAL")
        volatile_score = strategies.calculate_strategy_score("BREAKOUT", regime="VOLATILE_RANGE", **base_kwargs)
        range_score = strategies.calculate_strategy_score("BREAKOUT", regime="RANGE", **base_kwargs)
        assert volatile_score > range_score

    def test_breakout_score_scales_with_volume(self):
        base_kwargs = dict(adx=20, rsi=50, atr_pct=1.0, ema_slope=0.0,
                            sentiment=0.0, regime="VOLATILE_RANGE", chop=50, mtf_signal="NEUTRAL")
        low_volume = strategies.calculate_strategy_score("BREAKOUT", volume_ratio=1.0, **base_kwargs)
        high_volume = strategies.calculate_strategy_score("BREAKOUT", volume_ratio=4.0, **base_kwargs)
        assert high_volume > low_volume

    def test_neutral_mtf_penalises_trend_strategies(self):
        base_kwargs = dict(adx=35, rsi=55, atr_pct=1.0, volume_ratio=2.0,
                            ema_slope=1.0, sentiment=0.2, regime="TRENDING", chop=30)
        with_mtf = strategies.calculate_strategy_score("SUPERTREND", mtf_signal="BULLISH", **base_kwargs)
        without_mtf = strategies.calculate_strategy_score("SUPERTREND", mtf_signal="NEUTRAL", **base_kwargs)
        assert without_mtf == pytest.approx(with_mtf - 5)

    def test_unknown_strategy_scores_zero(self):
        score = strategies.calculate_strategy_score(
            "NOT_A_STRATEGY", adx=30, rsi=50, atr_pct=1.0, volume_ratio=1.0,
            ema_slope=1.0, sentiment=0.0, regime="TRENDING", chop=30, mtf_signal="BULLISH",
        )
        assert score == 0


# ----------------------------------------------------------------
# Signal generation
# ----------------------------------------------------------------

class TestGenerateStrategySignal:
    @pytest.mark.parametrize("strategy", STRATEGY_NAMES)
    def test_returns_valid_signal_for_every_strategy(self, strategy):
        df = noisy_uptrend_df()
        signal = strategies.generate_strategy_signal(strategy, df, sentiment=0.0, regime="TRENDING")
        assert signal in ("BUY", "SELL", "HOLD")

    def _macd_burst_df(self, direction="up", volumes=None):
        """Доошлоод (эсвэл өсөөд) сүүлийн хэдэн мөрд эсрэг чиглэлд огцом
        хурдассан momentum burst — MACD histogram шинээр өсч/буурч эхэлдэг,
        RSI хэт extreme болоогүй тохиолдол."""
        if direction == "up":
            lead = [150.0 - i * 0.8 for i in range(40)]
            burst = [lead[-1] + i * 1.2 for i in range(1, 8)]
        else:
            lead = [100.0 + i * 0.8 for i in range(40)]
            burst = [lead[-1] - i * 1.2 for i in range(1, 8)]
        return make_df(lead + burst, volumes=volumes)

    def test_macd_momentum_buys_on_normal_volume(self):
        df = self._macd_burst_df("up")
        assert strategies.generate_strategy_signal("MACD_MOMENTUM", df, sentiment=0.0, regime="TRENDING") == "BUY"

    def test_macd_momentum_holds_on_thin_volume(self):
        # histogram-ийн нөхцөл ижил хэвээр, зөвхөн сүүлийн мөрийн эзлэхүүн
        # дундажаас доогуур бол — үүнгүйгээр momentum burst chart-ийн шуугиан
        # (thin volume) дээр ч BUY гардаг байсан
        df = self._macd_burst_df("up", volumes=[100.0] * 46 + [20.0])
        assert strategies.generate_strategy_signal("MACD_MOMENTUM", df, sentiment=0.0, regime="TRENDING") == "HOLD"

    def test_macd_momentum_sells_on_normal_volume(self):
        df = self._macd_burst_df("down")
        assert strategies.generate_strategy_signal("MACD_MOMENTUM", df, sentiment=0.0, regime="TRENDING") == "SELL"

    def test_macd_momentum_holds_sell_on_thin_volume(self):
        df = self._macd_burst_df("down", volumes=[100.0] * 46 + [20.0])
        assert strategies.generate_strategy_signal("MACD_MOMENTUM", df, sentiment=0.0, regime="TRENDING") == "HOLD"

    def test_rsi_strategy_buys_when_oversold(self):
        df = downtrend_df(n=260, start=500.0, step=1.0)
        signal = strategies.generate_strategy_signal("RSI_STRATEGY", df, sentiment=0.0, regime="RANGE")
        assert signal == "BUY"

    def test_rsi_strategy_holds_when_sentiment_opposes(self):
        # rsi < 30 боловч sentiment -0.6-аас доош бол BUY өгөх ёсгүй
        df = downtrend_df(n=260, start=500.0, step=1.0)
        signal = strategies.generate_strategy_signal("RSI_STRATEGY", df, sentiment=-0.9, regime="RANGE")
        assert signal == "HOLD"

    def test_trend_following_buys_a_sustained_uptrend(self):
        # 600 1h лаа = 150 4h лаа — 4h EMA-100-д хүрэлцэнэ
        df = noisy_uptrend_df(n=600, step=2.0)
        assert strategies.generate_strategy_signal(
            "TREND_FOLLOWING", df, sentiment=0.0, regime="STRONG_TREND") == "BUY"

    def test_trend_following_blocked_by_negative_sentiment(self):
        df = noisy_uptrend_df(n=600, step=2.0)
        assert strategies.generate_strategy_signal(
            "TREND_FOLLOWING", df, sentiment=-0.9, regime="STRONG_TREND") == "HOLD"

    def test_trend_following_sells_a_sustained_downtrend(self):
        df = make_df([1500.0 - i * 2.0 for i in range(600)])
        assert strategies.generate_strategy_signal(
            "TREND_FOLLOWING", df, sentiment=0.0, regime="STRONG_TREND") == "SELL"

    def test_trend_following_holds_without_enough_4h_history(self):
        # 260 1h лаа = 65 4h лаа — EMA-100 тооцоологдох боломжгүй тул
        # таамаглахын оронд HOLD буцаана
        df = noisy_uptrend_df(n=260, step=2.0)
        assert strategies.generate_strategy_signal(
            "TREND_FOLLOWING", df, sentiment=0.0, regime="STRONG_TREND") == "HOLD"

    def test_trend_following_holds_when_trend_has_flattened(self):
        # Урт өсөлтийн дараа үнэ тэгширсэн: EMA эрэмбэ хэвээр, ADX өндөр хэвээр,
        # гэвч сүүлийн налуу босгоос доош — идэвхгүй болсон трендэд орохгүй
        rise = [100.0 + i * 2.0 for i in range(440)]
        df = make_df(rise + [rise[-1]] * 160)

        assert strategies.generate_strategy_signal(
            "TREND_FOLLOWING", df, sentiment=0.0, regime="STRONG_TREND") == "HOLD"

    def test_trend_following_ignores_1h_noise_that_would_fire_on_1h(self):
        # Богино хугацааны огцом үсрэлт 4h макро трендийг өөрчлөхгүй —
        # 1h дээр ажилладаг байхад ийм шуугиан signal өгч болох байсан
        flat = [100.0 + (i % 5) * 0.2 for i in range(590)]
        spike = [flat[-1] + i * 3.0 for i in range(1, 11)]
        df = make_df(flat + spike)
        assert strategies.generate_strategy_signal(
            "TREND_FOLLOWING", df, sentiment=0.0, regime="STRONG_TREND") == "HOLD"

    def test_breakout_buy_requires_volume_confirmation(self):
        # Band-аас цуцарсан ч эзлэхүүн энгийн хэвээр байвал HOLD — үүнгүйгээр
        # хуурамч (noise) хөдөлгөөнийг signal болгож болохгүй
        base = [100.0 + (i % 2) * 0.1 for i in range(40)]
        df = make_df(base[:-1] + [112.0], volumes=[100.0] * 40)
        assert strategies.generate_strategy_signal("BREAKOUT", df, sentiment=0.0, regime="VOLATILE_RANGE") == "HOLD"

    def test_breakout_buy_with_volume_spike(self):
        base = [100.0 + (i % 2) * 0.1 for i in range(40)]
        df = make_df(base[:-1] + [112.0], volumes=[100.0] * 39 + [400.0])
        assert strategies.generate_strategy_signal("BREAKOUT", df, sentiment=0.0, regime="VOLATILE_RANGE") == "BUY"

    def test_breakout_sell_with_volume_spike(self):
        base = [100.0 + (i % 2) * 0.1 for i in range(40)]
        df = make_df(base[:-1] + [89.0], volumes=[100.0] * 39 + [400.0])
        assert strategies.generate_strategy_signal("BREAKOUT", df, sentiment=0.0, regime="VOLATILE_RANGE") == "SELL"

    def test_breakout_holds_when_price_stays_inside_bands(self):
        # Volume spike ирсэн ч үнэ band дотор хэвээр бол breakout биш
        base = [100.0 + (i % 2) * 0.1 for i in range(40)]
        df = make_df(base, volumes=[100.0] * 39 + [400.0])
        assert strategies.generate_strategy_signal("BREAKOUT", df, sentiment=0.0, regime="VOLATILE_RANGE") == "HOLD"

    def test_bollinger_mean_reversion_holds_in_trend_regime(self):
        df = noisy_uptrend_df()
        assert strategies.generate_strategy_signal(
            "BOLLINGER_MEAN_REVERSION", df, sentiment=0.0, regime="TRENDING"
        ) == "HOLD"


# ----------------------------------------------------------------
# Exchange rounding / notional
# ----------------------------------------------------------------

class TestDecimalsFromStep:
    @pytest.mark.parametrize("step,expected", [
        (0.001, 3),
        (0.1, 1),
        (1.0, 0),
        (0.00001, 5),
    ])
    def test_step_to_decimals(self, step, expected):
        assert market_data.decimals_from_step(step) == expected

    def test_invalid_step_falls_back_to_8(self):
        assert market_data.decimals_from_step(0) == 8
        assert market_data.decimals_from_step(None) == 8


class TestRounding:
    def test_quantity_rounds_down_to_step(self, fake_symbol_info):
        assert market_data.round_quantity("BTCUSDT", 0.0019) == 0.001

    def test_price_rounds_down_to_tick(self, fake_symbol_info):
        assert market_data.round_price("BTCUSDT", 100.19) == pytest.approx(100.1)

    def test_integer_step_truncates_fraction(self, fake_symbol_info):
        assert market_data.round_quantity("DOGEUSDT", 15.9) == 15.0

    def test_unknown_symbol_returns_none(self, fake_symbol_info):
        assert market_data.round_quantity("FAKEUSDT", 1.0) is None
        assert market_data.round_price("FAKEUSDT", 1.0) is None


class TestFormatting:
    def test_price_never_uses_scientific_notation(self, fake_symbol_info):
        # str(0.00001) нь '1e-05' болдог — Binance үүнийг татгалзана
        formatted = market_data.format_price("DOGEUSDT", 0.00001)
        assert "e" not in formatted
        assert formatted == "0.00001"

    def test_qty_uses_step_precision(self, fake_symbol_info):
        assert market_data.format_qty("BTCUSDT", 0.5) == "0.500"

    def test_unknown_symbol_falls_back_to_8_decimals(self, fake_symbol_info):
        assert market_data.format_qty("FAKEUSDT", 1.5) == "1.50000000"


class TestCheckMinNotional:
    def test_rejects_below_min_notional(self, fake_symbol_info):
        # 50000 * 0.001 = $50 < $100 minNotional
        assert market_data.check_min_notional("BTCUSDT", 50000, 0.001) is False

    def test_accepts_at_or_above_min_notional(self, fake_symbol_info):
        assert market_data.check_min_notional("BTCUSDT", 50000, 0.01) is True

    def test_unknown_symbol_passes_through(self, fake_symbol_info):
        assert market_data.check_min_notional("FAKEUSDT", 1, 1) is True


# ----------------------------------------------------------------
# Rate limit backoff
# ----------------------------------------------------------------

class TestRateLimitWait:
    def test_honours_retry_after_header(self):
        assert binance_client._rate_limit_wait(FakeResponse({"Retry-After": "42"}), attempt=0) == 42

    def test_exponential_backoff_without_header(self):
        assert binance_client._rate_limit_wait(FakeResponse(), attempt=0) == 2
        assert binance_client._rate_limit_wait(FakeResponse(), attempt=3) == 16

    def test_backoff_capped_at_60s(self):
        assert binance_client._rate_limit_wait(FakeResponse(), attempt=20) == 60

    def test_malformed_retry_after_ignored(self):
        assert binance_client._rate_limit_wait(FakeResponse({"Retry-After": "soon"}), attempt=0) == 2


# ----------------------------------------------------------------
# Drawdown circuit breaker (риск удирдлагын хамгийн чухал хэсэг)
# ----------------------------------------------------------------

class TestDrawdownCircuitBreaker:
    def test_halts_when_drawdown_exceeds_limit(self, monkeypatch, telegram_messages):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot_state, "session_peak_balance", 1000.0)
        monkeypatch.setattr(account, "get_usdt_balance", lambda: 800.0)  # -20%

        risk.check_drawdown_circuit_breaker()

        assert bot_state.drawdown_halt is True
        assert bot_state.safety_lock is True
        assert any("DRAWDOWN" in m for m in telegram_messages)

    def test_does_not_halt_below_limit(self, monkeypatch):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot_state, "session_peak_balance", 1000.0)
        monkeypatch.setattr(account, "get_usdt_balance", lambda: 950.0)  # -5%

        risk.check_drawdown_circuit_breaker()

        assert bot_state.drawdown_halt is False
        assert bot_state.safety_lock is False

    def test_new_high_updates_peak_and_clears_lock(self, monkeypatch):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot_state, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot_state, "drawdown_lock_active", True)
        monkeypatch.setattr(account, "get_usdt_balance", lambda: 1200.0)

        risk.check_drawdown_circuit_breaker()

        assert bot_state.session_peak_balance == 1200.0
        assert bot_state.drawdown_lock_active is False

    def test_disabled_when_limit_is_zero(self, monkeypatch):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 0.0)
        monkeypatch.setattr(bot_state, "session_peak_balance", 1000.0)
        monkeypatch.setattr(account, "get_usdt_balance", lambda: 1.0)  # -99.9%

        risk.check_drawdown_circuit_breaker()

        assert bot_state.drawdown_halt is False

    def test_zero_balance_does_not_trigger_false_halt(self, monkeypatch):
        # Баланс уншиж чадаагүй (0.0 буцсан) тохиолдолд halt хийх ёсгүй
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot_state, "session_peak_balance", 1000.0)
        monkeypatch.setattr(account, "get_usdt_balance", lambda: 0.0)

        risk.check_drawdown_circuit_breaker()

        assert bot_state.drawdown_halt is False

    def test_does_not_re_trigger_while_safety_locked(self, monkeypatch, telegram_messages):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot_state, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot_state, "safety_lock", True)
        monkeypatch.setattr(account, "get_usdt_balance", lambda: 500.0)

        risk.check_drawdown_circuit_breaker()

        assert telegram_messages == []


# ----------------------------------------------------------------
# Strategy performance / cooldown
# ----------------------------------------------------------------

class TestUpdateStrategyPerformance:
    def test_win_increments_wins_and_resets_streak(self, monkeypatch):
        bot_state.strategy_stats["RSI_STRATEGY"]["consecutive_losses"] = 2
        risk.update_strategy_performance("RSI_STRATEGY", 25.0)

        stats = bot_state.strategy_stats["RSI_STRATEGY"]
        assert stats["trades"] == 1
        assert stats["wins"] == 1
        assert stats["consecutive_losses"] == 0
        assert stats["total_pnl"] == 25.0

    def test_loss_increments_streak(self, monkeypatch):
        patch_setting(monkeypatch, "CONSECUTIVE_LOSS_LIMIT", 3)
        risk.update_strategy_performance("RSI_STRATEGY", -10.0)

        stats = bot_state.strategy_stats["RSI_STRATEGY"]
        assert stats["losses"] == 1
        assert stats["consecutive_losses"] == 1
        assert stats["active"] is True

    def test_pauses_strategy_after_loss_limit(self, monkeypatch, telegram_messages):
        patch_setting(monkeypatch, "ADAPTIVE_STRATEGY", True)
        patch_setting(monkeypatch, "CONSECUTIVE_LOSS_LIMIT", 3)
        patch_setting(monkeypatch, "STRATEGY_COOLDOWN_CYCLES", 2)

        for _ in range(3):
            risk.update_strategy_performance("RSI_STRATEGY", -10.0)

        stats = bot_state.strategy_stats["RSI_STRATEGY"]
        assert stats["active"] is False
        assert stats["paused_cycles"] == 2
        assert any("PAUSED" in m for m in telegram_messages)

    def test_adaptive_disabled_never_pauses(self, monkeypatch):
        patch_setting(monkeypatch, "ADAPTIVE_STRATEGY", False)
        patch_setting(monkeypatch, "CONSECUTIVE_LOSS_LIMIT", 2)

        for _ in range(5):
            risk.update_strategy_performance("RSI_STRATEGY", -10.0)

        assert bot_state.strategy_stats["RSI_STRATEGY"]["active"] is True

    def test_unknown_strategy_is_ignored(self):
        risk.update_strategy_performance("NOT_A_STRATEGY", -10.0)
        assert "NOT_A_STRATEGY" not in bot_state.strategy_stats

    def test_strategy_stats_only_no_session_pnl(self):
        # Сессийн ашгийг record_realized_pnl хариуцна — энэ функц зөвхөн
        # стратегийн статистикийг хөтөлнө
        risk.update_strategy_performance("RSI_STRATEGY", 10.0)

        assert bot_state.strategy_stats["RSI_STRATEGY"]["total_pnl"] == 10.0
        assert bot_state.session_realized_pnl == 0.0


class TestRecordRealizedPnl:
    def test_session_pnl_accumulates(self):
        risk.record_realized_pnl("RSI_STRATEGY", 10.0)
        risk.record_realized_pnl("MACD_MOMENTUM", -4.0)

        assert bot_state.session_realized_pnl == pytest.approx(6.0)

    def test_unknown_strategy_still_counts_toward_session_pnl(self):
        # RECOVERED зэрэг танигдаагүй стратегийн ашиг ч тайланд орох ёстой
        risk.record_realized_pnl("RECOVERED", 17.80)

        assert bot_state.session_realized_pnl == pytest.approx(17.80)
        assert "RECOVERED" not in bot_state.strategy_stats

    def test_known_strategy_updates_both(self):
        risk.record_realized_pnl("RSI_STRATEGY", 25.0)

        assert bot_state.session_realized_pnl == pytest.approx(25.0)
        assert bot_state.strategy_stats["RSI_STRATEGY"]["wins"] == 1


class TestStrategyCooldowns:
    def test_paused_cycles_count_down(self):
        bot_state.strategy_stats["RSI_STRATEGY"].update(active=False, paused_cycles=2)
        risk.update_strategy_cooldowns()

        assert bot_state.strategy_stats["RSI_STRATEGY"]["paused_cycles"] == 1
        assert bot_state.strategy_stats["RSI_STRATEGY"]["active"] is False

    def test_reactivates_when_cooldown_finishes(self, telegram_messages):
        bot_state.strategy_stats["RSI_STRATEGY"].update(
            active=False, paused_cycles=1, consecutive_losses=3
        )
        risk.update_strategy_cooldowns()

        stats = bot_state.strategy_stats["RSI_STRATEGY"]
        assert stats["active"] is True
        assert stats["consecutive_losses"] == 0
        assert any("REACTIVATED" in m for m in telegram_messages)

    def test_active_strategies_excludes_paused(self):
        bot_state.strategy_stats["RSI_STRATEGY"]["active"] = False
        active = risk.get_active_strategies()

        assert "RSI_STRATEGY" not in active
        assert "SUPERTREND" in active


# ----------------------------------------------------------------
# State persistence
# ----------------------------------------------------------------

class TestStatePersistence:
    def test_strategy_state_roundtrip(self):
        bot_state.strategy_stats["RSI_STRATEGY"].update(trades=7, wins=4, total_pnl=123.45)
        persistence.save_strategy_state()

        bot_state.strategy_stats["RSI_STRATEGY"].update(trades=0, wins=0, total_pnl=0.0)
        persistence.load_strategy_state()

        stats = bot_state.strategy_stats["RSI_STRATEGY"]
        assert stats["trades"] == 7
        assert stats["wins"] == 4
        assert stats["total_pnl"] == pytest.approx(123.45)

    def test_missing_strategy_file_is_noop(self):
        bot_state.strategy_stats["RSI_STRATEGY"]["trades"] = 3
        persistence.load_strategy_state()  # файл байхгүй
        assert bot_state.strategy_stats["RSI_STRATEGY"]["trades"] == 3

    def test_corrupt_strategy_file_does_not_crash(self, isolated_state_files):
        (isolated_state_files / "strategy_state.json").write_text("{ энэ бол JSON биш")
        bot_state.strategy_stats["RSI_STRATEGY"]["trades"] = 5

        persistence.load_strategy_state()  # алдаа шидэх ёсгүй

        assert bot_state.strategy_stats["RSI_STRATEGY"]["trades"] == 5

    def test_session_state_roundtrip(self, monkeypatch):
        monkeypatch.setattr(bot_state, "session_peak_balance", 1500.0)
        monkeypatch.setattr(bot_state, "session_start_balance", 1000.0)
        persistence.save_session_state()

        data = persistence.load_session_state()

        assert data["session_peak_balance"] == 1500.0
        assert data["session_start_balance"] == 1000.0
        assert "saved_at" in data

    def test_missing_session_file_returns_none(self):
        assert persistence.load_session_state() is None

    def test_non_dict_session_file_returns_none(self, isolated_state_files):
        (isolated_state_files / "session_state.json").write_text(json.dumps([1, 2, 3]))
        assert persistence.load_session_state() is None


# ----------------------------------------------------------------
# Trailing activation
# ----------------------------------------------------------------

class TestTrailingActivation:
    def test_buy_activation_above_entry(self, monkeypatch, fake_symbol_info):
        monkeypatch.setattr(account, "get_positions", lambda: [])
        patch_setting(monkeypatch, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = position_manager.calculate_trailing_activation("BTCUSDT", "BUY", 100.0)

        # Яг утгаар нь шалгана: `> 100` гэсэн шалгуур нь mark_price * 1.001
        # шалын ард нуугдаж, чиглэл эсрэгээрээ болсныг ч давуулж өнгөрөөнө.
        assert activation == pytest.approx(101.0)

    def test_sell_activation_below_entry(self, monkeypatch, fake_symbol_info):
        monkeypatch.setattr(account, "get_positions", lambda: [])
        patch_setting(monkeypatch, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = position_manager.calculate_trailing_activation("BTCUSDT", "SELL", 100.0)

        assert activation == pytest.approx(99.0)

    def test_atr_scaled_level_overrides_the_configured_one(self, monkeypatch, fake_symbol_info):
        monkeypatch.setattr(account, "get_positions", lambda: [])
        patch_setting(monkeypatch, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = position_manager.calculate_trailing_activation("BTCUSDT", "BUY", 100.0, trail_pct=4.0)

        assert activation == pytest.approx(104.0)

    def test_buy_activation_stays_above_mark_price(self, monkeypatch, fake_symbol_info):
        # Mark price аль хэдийн entry-ээс дээш яваад байвал activation түүнээс дээш байх ёстой
        monkeypatch.setattr(account, "get_positions", lambda: [
            {"symbol": "BTCUSDT", "markPrice": 110.0}
        ])
        patch_setting(monkeypatch, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = position_manager.calculate_trailing_activation("BTCUSDT", "BUY", 100.0)

        assert activation > 110.0


# ----------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------

class TestCorrelation:
    def test_identical_series_correlate_to_one(self, monkeypatch):
        df = noisy_uptrend_df(n=60)
        monkeypatch.setattr(market_data, "get_klines", lambda symbol, interval="1h", limit=200: df.copy())

        corr = screening.calculate_correlation("BTCUSDT", "ETHUSDT", lookback=50)

        assert corr == pytest.approx(1.0)

    def test_inverse_series_correlate_negatively(self, monkeypatch):
        up = noisy_uptrend_df(n=60)
        down = make_df([300.0 - c for c in up["close"]])

        def fake_klines(symbol, interval="1h", limit=200):
            return up.copy() if symbol == "BTCUSDT" else down.copy()

        monkeypatch.setattr(market_data, "get_klines", fake_klines)

        corr = screening.calculate_correlation("BTCUSDT", "ETHUSDT", lookback=50)

        # Толин тусгал үнэ — pct_change суурь өөр тул яг -1.0 болохгүй ч
        # хүчтэй сөрөг correlation байх ёстой
        assert corr < -0.9

    def test_short_history_returns_zero(self, monkeypatch):
        short = noisy_uptrend_df(n=5)
        monkeypatch.setattr(market_data, "get_klines", lambda symbol, interval="1h", limit=200: short.copy())

        assert screening.calculate_correlation("BTCUSDT", "ETHUSDT", lookback=50) == 0.0

    def test_api_failure_returns_zero(self, monkeypatch):
        def boom(symbol, interval="1h", limit=200):
            raise ValueError("Kline error")

        monkeypatch.setattr(market_data, "get_klines", boom)

        assert screening.calculate_correlation("BTCUSDT", "ETHUSDT") == 0.0

    def test_cache_avoids_recomputation(self, monkeypatch):
        calls = []

        def counted(symbol1, symbol2, lookback=50):
            calls.append((symbol1, symbol2))
            return 0.5

        monkeypatch.setattr(screening, "calculate_correlation", counted)
        patch_setting(monkeypatch, "CORRELATION_CACHE_TTL", 3600)

        screening.calculate_correlation_cached("BTCUSDT", "ETHUSDT")
        screening.calculate_correlation_cached("BTCUSDT", "ETHUSDT")
        # эсрэг дараалал ч ижил кэшийг ашиглах ёстой
        screening.calculate_correlation_cached("ETHUSDT", "BTCUSDT")

        assert len(calls) == 1


# ----------------------------------------------------------------
# execute_trades хамгаалалтууд (захиалга огт өгөхгүй байх ёстой замууд)
# ----------------------------------------------------------------

@pytest.fixture
def order_spy(monkeypatch):
    """Захиалга өгөх оролдлогыг бүртгэнэ (жинхэнэ захиалга явуулахгүй)."""
    orders = []

    def fake_order(symbol, side, quantity, reduce_only=False, position_side=None, client_order_id=None):
        orders.append({"symbol": symbol, "side": side, "quantity": quantity})
        return {"status": "FILLED", "executedQty": quantity, "avgPrice": 100.0}

    monkeypatch.setattr(order_api, "place_market_order", fake_order)
    monkeypatch.setattr(order_api, "cancel_all_symbol_orders", lambda symbol: None)
    # Хэсэгчилсэн TP нь conditional захиалга — market захиалгын жагсаалтад
    # орохгүй, гэхдээ сүлжээ рүү ч гарахгүй байх ёстой.
    monkeypatch.setattr(order_api, "place_partial_take_profit_order",
                        lambda symbol, side, quantity, tp_price, position_side=None: {"orderId": 1})
    monkeypatch.setattr(account, "ensure_leverage", lambda symbol, leverage=None: True)
    monkeypatch.setattr(account, "get_actual_leverage", lambda symbol: 5)
    return orders


def _coin(symbol="BTCUSDT", signal="BUY", price=100.0):
    return {
        "symbol": symbol,
        "strategy": "RSI_STRATEGY",
        "signal": signal,
        "price": price,
        "score": 50.0,
        "adx": 30.0,
        "rsi": 28.0,
        "regime": "RANGE",
    }


@pytest.fixture
def tradeable(monkeypatch, order_spy, fake_symbol_info):
    """Бүх хамгаалалт нээлттэй, захиалга үнэхээр гарах ёстой нөхцөл."""
    monkeypatch.setattr(account, "get_positions", lambda: [])
    monkeypatch.setattr(account, "get_position_mode", lambda: False)
    monkeypatch.setattr(binance_client, "current_timestamp_ms", lambda: 1_700_000_000_000)
    monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                        lambda symbol, side, qty, entry, pos_side, **kw: (True, 103.0, 101.0))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return order_spy


class TestExecuteTradesHappyPath:
    """Positive control: хамгаалалтын тестүүд утгагүй pass болохгүйг батална."""

    def test_valid_signal_places_order(self, tradeable):
        execution.execute_trades([_coin()], total_balance=1000.0)

        assert len(tradeable) == 1
        assert tradeable[0]["symbol"] == "BTCUSDT"
        assert tradeable[0]["side"] == "BUY"

    def test_sell_signal_places_sell_order(self, tradeable):
        execution.execute_trades([_coin(signal="SELL")], total_balance=1000.0)

        assert tradeable[0]["side"] == "SELL"

    def test_position_size_follows_allocation_and_leverage(self, monkeypatch, tradeable):
        patch_setting(monkeypatch, "ATR_RISK_SIZING_ENABLED", False)
        patch_setting(monkeypatch, "TRADE_ALLOCATION", 0.09)
        patch_setting(monkeypatch, "LEVERAGE", 5)

        execution.execute_trades([_coin(price=100.0)], total_balance=1000.0)

        # margin = 1000 * 0.09 = 90, notional = 90 * 5 = 450, qty = 450 / 100 = 4.5
        assert tradeable[0]["quantity"] == pytest.approx(4.5)

    def test_opened_trade_is_tracked(self, tradeable):
        execution.execute_trades([_coin()], total_balance=1000.0)

        assert "BTCUSDT" in bot_state.active_trade_info
        assert bot_state.active_trade_info["BTCUSDT"]["strategy"] == "RSI_STRATEGY"

    def test_failed_protection_closes_position_immediately(self, monkeypatch, tradeable, telegram_messages):
        monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side, **kw: (False, None, None))
        monkeypatch.setattr(account, "get_trade_realized_pnl", lambda symbol, opened_at_ms: -1.5)

        execution.execute_trades([_coin()], total_balance=1000.0)

        # нээх + яаралтай хаах = 2 захиалга, позиц хөтлөгдөж үлдэх ёсгүй
        assert len(tradeable) == 2
        assert tradeable[1]["side"] == "SELL"
        assert "BTCUSDT" not in bot_state.active_trade_info
        assert any("EMERGENCY CLOSED" in m for m in telegram_messages)

    def test_unfilled_order_does_not_create_phantom_position(self, monkeypatch, tradeable, telegram_messages):
        monkeypatch.setattr(order_api, "place_market_order",
                            lambda *a, **kw: {"status": "EXPIRED", "executedQty": 0, "avgPrice": 0})

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert "BTCUSDT" not in bot_state.active_trade_info
        assert any("NOT FILLED" in m for m in telegram_messages)


class TestExecuteTradesGuards:
    """
    Тест бүр `tradeable` fixture дээр суурилна — өөрөөр хэлбэл захиалга гарах
    бүх нөхцөл бүрдсэн байх бөгөөд ЗӨВХӨН шалгаж буй хамгаалалт нь захиалгыг
    зогсоох ёстой. Ингэснээр хамаагүй өөр шалтгаанаар "pass" болохгүй.
    """

    def test_safety_lock_blocks_all_trades(self, monkeypatch, tradeable):
        monkeypatch.setattr(bot_state, "safety_lock", True)

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_empty_selection_places_no_orders(self, tradeable):
        execution.execute_trades([], total_balance=1000.0)

        assert tradeable == []

    def test_hold_signal_is_skipped(self, tradeable):
        execution.execute_trades([_coin(signal="HOLD")], total_balance=1000.0)

        assert tradeable == []

    def test_symbol_with_existing_position_is_skipped(self, monkeypatch, tradeable):
        monkeypatch.setattr(account, "get_positions", lambda: [
            {"symbol": "BTCUSDT", "positionAmt": 0.5, "entryPrice": 100.0}
        ])

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_low_balance_stops_trading(self, monkeypatch, tradeable, telegram_messages):
        patch_setting(monkeypatch, "MIN_BALANCE_USDT", 10.0)

        execution.execute_trades([_coin()], total_balance=5.0)

        assert tradeable == []
        assert any("БАЛАНС" in m for m in telegram_messages)

    def test_margin_cap_blocks_new_trade(self, monkeypatch, tradeable):
        # Байгаа позиц: 30 * $100 / 5x = $600 margin.
        # Дээд хязгаар: $1000 * 0.55 = $550 — шинэ арилжаа багтахгүй.
        monkeypatch.setattr(account, "get_positions", lambda: [
            {"symbol": "ETHUSDT", "positionAmt": 30.0, "entryPrice": 100.0}
        ])
        patch_setting(monkeypatch, "MAX_TOTAL_MARGIN_USAGE", 0.55)
        patch_setting(monkeypatch, "TRADE_ALLOCATION", 0.09)

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_margin_cap_allows_trade_within_limit(self, monkeypatch, tradeable):
        # Байгаа позиц: 1 * $100 / 5x = $20 margin — хязгаарт багтана
        monkeypatch.setattr(account, "get_positions", lambda: [
            {"symbol": "ETHUSDT", "positionAmt": 1.0, "entryPrice": 100.0}
        ])
        patch_setting(monkeypatch, "MAX_TOTAL_MARGIN_USAGE", 0.55)
        patch_setting(monkeypatch, "TRADE_ALLOCATION", 0.09)

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert len(tradeable) == 1

    def test_unprotected_symbol_is_skipped(self, monkeypatch, tradeable):
        monkeypatch.setattr(bot_state, "unprotected_symbols", {"BTCUSDT"})

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_below_min_notional_is_skipped(self, tradeable, fake_symbol_info):
        fake_symbol_info["BTCUSDT"]["minNotional"] = 100_000.0

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_below_min_qty_is_skipped(self, tradeable, fake_symbol_info):
        fake_symbol_info["BTCUSDT"]["minQty"] = 1000.0

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_leverage_failure_skips_symbol(self, monkeypatch, tradeable):
        monkeypatch.setattr(account, "ensure_leverage", lambda symbol, leverage=None: False)

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_unknown_symbol_without_exchange_info_is_skipped(self, tradeable):
        execution.execute_trades([_coin(symbol="FAKEUSDT")], total_balance=1000.0)

        assert tradeable == []


# ----------------------------------------------------------------
# monitor_positions — хаагдсан позицыг таних, тайланг хязгаарлах
# ----------------------------------------------------------------

def _position(symbol="BTCUSDT", amt=1.0, entry=100.0, mark=105.0, pnl=5.0):
    return {
        "symbol": symbol,
        "positionAmt": amt,
        "entryPrice": entry,
        "markPrice": mark,
        "unRealizedProfit": pnl,
        "positionSide": "BOTH",
    }


def _trade_info(strategy="RSI_STRATEGY", side="BUY"):
    return {
        "strategy": strategy,
        "side": side,
        "entry_price": 100.0,
        "quantity": 1.0,
        "position_side": "BOTH",
        "opened_at": 1_700_000_000.0,
        "opened_at_ms": 1_700_000_000_000,
    }


@pytest.fixture
def monitor_env(monkeypatch):
    """monitor_positions-ийн гадаад хамаарлыг мок болгоно."""
    finalized = []

    monkeypatch.setattr(position_manager, "finalize_trade",
                        lambda symbol, trade_data: finalized.append(symbol) or 12.5)
    monkeypatch.setattr(order_api, "cancel_all_symbol_orders", lambda symbol: None)
    monkeypatch.setattr(account, "get_usdt_balance", lambda: 1000.0)
    monkeypatch.setattr(bot_state, "last_telegram_report_time", 0.0)
    return finalized


class TestMonitorPositions:
    def test_closed_position_is_finalized_and_untracked(self, monkeypatch, monitor_env):
        # Bot нь BTCUSDT-г хөтөлж байсан ч биржид байхгүй болсон = хаагдсан
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(account, "get_positions", lambda: [])

        position_manager.monitor_positions()

        assert monitor_env == ["BTCUSDT"]
        assert "BTCUSDT" not in bot_state.active_trade_info

    def test_open_position_is_not_finalized(self, monkeypatch, monitor_env):
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(account, "get_positions", lambda: [_position()])

        position_manager.monitor_positions()

        assert monitor_env == []
        assert "BTCUSDT" in bot_state.active_trade_info

    def test_only_the_closed_symbol_is_finalized(self, monkeypatch, monitor_env):
        monkeypatch.setattr(bot_state, "active_trade_info", {
            "BTCUSDT": _trade_info(),
            "ETHUSDT": _trade_info(),
        })
        monkeypatch.setattr(account, "get_positions", lambda: [_position("ETHUSDT")])

        position_manager.monitor_positions()

        assert monitor_env == ["BTCUSDT"]
        assert set(bot_state.active_trade_info) == {"ETHUSDT"}

    def test_cancels_leftover_orders_of_closed_position(self, monkeypatch, monitor_env):
        cancelled = []
        monkeypatch.setattr(order_api, "cancel_all_symbol_orders", lambda symbol: cancelled.append(symbol))
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(account, "get_positions", lambda: [])

        position_manager.monitor_positions()

        assert cancelled == ["BTCUSDT"]

    def test_cancel_failure_does_not_break_monitoring(self, monkeypatch, monitor_env):
        def boom(symbol):
            raise RuntimeError("API down")

        monkeypatch.setattr(order_api, "cancel_all_symbol_orders", boom)
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(account, "get_positions", lambda: [])

        position_manager.monitor_positions()  # алдаа шидэх ёсгүй

        assert monitor_env == ["BTCUSDT"]

    def test_report_sent_when_interval_elapsed(self, monkeypatch, monitor_env, telegram_messages):
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(account, "get_positions", lambda: [_position()])
        patch_setting(monkeypatch, "TELEGRAM_REPORT_INTERVAL_SEC", 0)

        position_manager.monitor_positions()

        assert any("МОНИТОР" in m for m in telegram_messages)
        assert bot_state.last_telegram_report_time > 0

    def test_report_throttled_within_interval(self, monkeypatch, monitor_env, telegram_messages):
        import time as _time

        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(account, "get_positions", lambda: [_position()])
        patch_setting(monkeypatch, "TELEGRAM_REPORT_INTERVAL_SEC", 300)
        monkeypatch.setattr(bot_state, "last_telegram_report_time", _time.time())

        position_manager.monitor_positions()

        assert telegram_messages == []

    def test_no_positions_sends_no_report(self, monkeypatch, monitor_env, telegram_messages):
        monkeypatch.setattr(bot_state, "active_trade_info", {})
        monkeypatch.setattr(account, "get_positions", lambda: [])
        patch_setting(monkeypatch, "TELEGRAM_REPORT_INTERVAL_SEC", 0)

        position_manager.monitor_positions()

        assert telegram_messages == []


# ----------------------------------------------------------------
# handle_target_reached — ашгийн зорилтод хүрэхэд бүх позиц хаагдах ёстой
# ----------------------------------------------------------------

@pytest.fixture
def target_env(monkeypatch):
    monkeypatch.setattr(account, "get_usdt_balance", lambda: 1300.0)
    monkeypatch.setattr(position_manager, "finalize_trade", lambda symbol, trade_data: 150.0)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    patch_setting(monkeypatch, "TARGET_PROFIT", 300.0)


class TestHandleTargetReached:
    def test_successful_close_returns_true_and_clears_tracking(self, monkeypatch, target_env, telegram_messages):
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(position_manager, "close_all_positions_and_verify", lambda: True)
        monkeypatch.setattr(account, "get_positions", lambda: [])

        assert position_manager.handle_target_reached(310.0) is True
        assert bot_state.active_trade_info == {}
        assert any("TARGET REALIZED" in m for m in telegram_messages)

    def test_safety_lock_engaged_before_closing(self, monkeypatch, target_env):
        seen = {}

        def fake_close():
            seen["locked_during_close"] = bot_state.safety_lock
            return True

        monkeypatch.setattr(bot_state, "active_trade_info", {})
        monkeypatch.setattr(position_manager, "close_all_positions_and_verify", fake_close)
        monkeypatch.setattr(account, "get_positions", lambda: [])

        position_manager.handle_target_reached(310.0)

        # Хаах явцад шинэ арилжаа нээгдэхээс сэргийлж түгжээ тавьсан байх ёстой
        assert seen["locked_during_close"] is True

    def test_failed_close_returns_false_and_keeps_lock(self, monkeypatch, target_env, telegram_messages):
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(position_manager, "close_all_positions_and_verify", lambda: False)
        monkeypatch.setattr(account, "get_positions", lambda: [_position()])

        assert position_manager.handle_target_reached(310.0) is False
        assert bot_state.safety_lock is True
        assert any("CLOSE INCOMPLETE" in m for m in telegram_messages)

    def test_leftover_position_after_close_fails_safety_check(self, monkeypatch, target_env, telegram_messages):
        # close_all нь True гэж мэдээлсэн ч бодит байдал дээр позиц үлдсэн
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(position_manager, "close_all_positions_and_verify", lambda: True)
        monkeypatch.setattr(account, "get_positions", lambda: [_position()])

        assert position_manager.handle_target_reached(310.0) is False
        assert bot_state.safety_lock is True
        assert any("FINAL SAFETY CHECK FAILED" in m for m in telegram_messages)

    def test_all_tracked_trades_are_finalized(self, monkeypatch, target_env):
        finalized = []
        monkeypatch.setattr(position_manager, "finalize_trade",
                            lambda symbol, trade_data: finalized.append(symbol) or 100.0)
        monkeypatch.setattr(bot_state, "active_trade_info", {
            "BTCUSDT": _trade_info(), "ETHUSDT": _trade_info(),
        })
        monkeypatch.setattr(position_manager, "close_all_positions_and_verify", lambda: True)
        monkeypatch.setattr(account, "get_positions", lambda: [])

        position_manager.handle_target_reached(310.0)

        assert sorted(finalized) == ["BTCUSDT", "ETHUSDT"]


# ----------------------------------------------------------------
# screen_coins — сонголтын шүүлтүүр
# ----------------------------------------------------------------

def _analysis(symbol, strategy_signals):
    """strategy_signals: {strategy: (signal, score)}"""
    strategies = {}
    for strategy, (signal, score) in strategy_signals.items():
        strategies[strategy] = {
            "strategy": strategy,
            "symbol": symbol,
            "price": 100.0,
            "score": score,
            "signal": signal,
            "adx": 30.0,
            "rsi": 45.0,
            "regime": "TRENDING",
        }
    return {"symbol": symbol, "price": 100.0, "strategies": strategies}


@pytest.fixture
def screen_env(monkeypatch):
    """screen_coins-ийн сүлжээ болон тайлангийн хамаарлыг мок болгоно."""
    monkeypatch.setattr(account, "get_positions", lambda: [])
    monkeypatch.setattr(account, "get_usdt_balance", lambda: 1000.0)
    monkeypatch.setattr(account, "get_actual_leverage", lambda symbol: 5)
    monkeypatch.setattr(reports, "send_selection_report",
                        lambda selected, all_candidates=None, skipped_reasons=None: None)
    patch_setting(monkeypatch, "CORRELATION_ENABLED", False)
    patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 20.0)
    patch_setting(monkeypatch, "MAX_SELECTIONS", 6)


def _use_analyses(monkeypatch, analyses):
    by_symbol = {a["symbol"]: a for a in analyses}
    patch_setting(monkeypatch, "SYMBOLS_POOL", list(by_symbol))
    monkeypatch.setattr(screening, "analyze_coin",
        lambda symbol, check_correlation=True, active_symbols=None: by_symbol.get(symbol),
    )


class TestScreenCoins:
    def test_selects_signal_above_min_score(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 50.0)})])

        selected = screening.screen_coins()

        assert [c["symbol"] for c in selected] == ["BTCUSDT"]

    def test_low_score_signal_is_filtered_out(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 5.0)})])

        assert screening.screen_coins() == []

    def test_hold_signal_is_ignored(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("HOLD", 90.0)})])

        assert screening.screen_coins() == []

    def test_paused_strategy_is_ignored(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)})])
        bot_state.strategy_stats["RSI_STRATEGY"]["active"] = False

        assert screening.screen_coins() == []

    def test_duplicate_symbol_keeps_highest_score(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {
            "RSI_STRATEGY": ("BUY", 40.0),
            "SUPERTREND": ("BUY", 80.0),
        })])

        selected = screening.screen_coins()

        assert len(selected) == 1
        assert selected[0]["strategy"] == "SUPERTREND"

    def test_takes_multiple_candidates_per_strategy(self, monkeypatch, screen_env):
        # Нэг стратеги хэд хэдэн coin дээр signal өгвөл бүгдийг нь авна —
        # өмнө нь зөвхөн хамгийн өндөр онооных нь л ордог байсан
        patch_setting(monkeypatch, "MAX_CANDIDATES_PER_STRATEGY", 3)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"RSI_STRATEGY": ("BUY", 80.0)}),
            _analysis("SOLUSDT", {"RSI_STRATEGY": ("BUY", 70.0)}),
        ])

        selected = screening.screen_coins()

        assert sorted(c["symbol"] for c in selected) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_candidate_cap_per_strategy_is_respected(self, monkeypatch, screen_env):
        patch_setting(monkeypatch, "MAX_CANDIDATES_PER_STRATEGY", 2)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"RSI_STRATEGY": ("BUY", 80.0)}),
            _analysis("SOLUSDT", {"RSI_STRATEGY": ("BUY", 70.0)}),
        ])

        selected = screening.screen_coins()

        # Хамгийн өндөр оноотой 2 нь үлдэнэ
        assert sorted(c["symbol"] for c in selected) == ["BTCUSDT", "ETHUSDT"]

    def test_cap_of_one_keeps_old_behaviour(self, monkeypatch, screen_env):
        patch_setting(monkeypatch, "MAX_CANDIDATES_PER_STRATEGY", 1)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"RSI_STRATEGY": ("BUY", 80.0)}),
        ])

        selected = screening.screen_coins()

        assert [c["symbol"] for c in selected] == ["BTCUSDT"]

    def test_respects_max_selections(self, monkeypatch, screen_env):
        patch_setting(monkeypatch, "MAX_SELECTIONS", 2)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"SUPERTREND": ("BUY", 80.0)}),
            _analysis("SOLUSDT", {"MACD_MOMENTUM": ("BUY", 70.0)}),
        ])

        assert len(screening.screen_coins()) == 2

    def test_correlated_symbol_is_removed(self, monkeypatch, screen_env):
        patch_setting(monkeypatch, "CORRELATION_ENABLED", True)
        patch_setting(monkeypatch, "CORRELATION_THRESHOLD", 0.65)
        monkeypatch.setattr(screening, "calculate_correlation_cached",
                            lambda s1, s2, lookback=50: 0.95)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"SUPERTREND": ("BUY", 80.0)}),
        ])

        selected = screening.screen_coins()

        assert len(selected) == 1

    def test_rejected_candidate_does_not_reject_others(self, monkeypatch, screen_env):
        """A-B хамааралтай, B-C хамааралтай, A-C хамааралгүй.

        B хасагдсан тул C-г хасах эрхгүй — өмнө нь хасагдсан нэр дэвшигч
        бусдыг хасаад байсан тул сонголт хэт нимгэрдэг байв.
        """
        pairs = {
            frozenset(["BTCUSDT", "ETHUSDT"]): 0.95,
            frozenset(["ETHUSDT", "SOLUSDT"]): 0.95,
            frozenset(["BTCUSDT", "SOLUSDT"]): 0.10,
        }
        patch_setting(monkeypatch, "CORRELATION_ENABLED", True)
        patch_setting(monkeypatch, "CORRELATION_THRESHOLD", 0.85)
        monkeypatch.setattr(screening, "calculate_correlation_cached",
                            lambda s1, s2, lookback=50: pairs[frozenset([s1, s2])])
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"SUPERTREND": ("BUY", 80.0)}),
            _analysis("SOLUSDT", {"MACD_MOMENTUM": ("BUY", 70.0)}),
        ])

        selected = [c["symbol"] for c in screening.screen_coins()]

        assert selected == ["BTCUSDT", "SOLUSDT"]

    def test_highest_scoring_candidate_wins_the_slot(self, monkeypatch, screen_env):
        # SUPERTREND нь стратегийн жагсаалтад эхэнд, гэхдээ оноо нь бага —
        # хамааралтай хос дотроос өндөр оноотой нь үлдэх ёстой
        patch_setting(monkeypatch, "CORRELATION_ENABLED", True)
        patch_setting(monkeypatch, "CORRELATION_THRESHOLD", 0.85)
        monkeypatch.setattr(screening, "calculate_correlation_cached", lambda s1, s2, lookback=50: 0.95)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"SUPERTREND": ("BUY", 15.0)}),
            _analysis("ETHUSDT", {"TREND_FOLLOWING": ("BUY", 27.0)}),
        ])

        selected = screening.screen_coins()

        assert [c["symbol"] for c in selected] == ["ETHUSDT"]

    def test_selection_is_ordered_by_score(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"SUPERTREND": ("BUY", 20.0)}),
            _analysis("ETHUSDT", {"TREND_FOLLOWING": ("BUY", 90.0)}),
            _analysis("SOLUSDT", {"MACD_MOMENTUM": ("BUY", 50.0)}),
        ])

        scores = [c["score"] for c in screening.screen_coins()]

        assert scores == sorted(scores, reverse=True)

    def test_uncorrelated_symbols_both_kept(self, monkeypatch, screen_env):
        patch_setting(monkeypatch, "CORRELATION_ENABLED", True)
        patch_setting(monkeypatch, "CORRELATION_THRESHOLD", 0.65)
        monkeypatch.setattr(screening, "calculate_correlation_cached",
                            lambda s1, s2, lookback=50: 0.1)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"SUPERTREND": ("BUY", 80.0)}),
        ])

        assert len(screening.screen_coins()) == 2

    def test_failed_analysis_is_skipped(self, monkeypatch, screen_env):
        # analyze_coin алдаа гарвал None буцаадаг — энэ нь бүх screening-ийг унагаах ёсгүй
        patch_setting(monkeypatch, "SYMBOLS_POOL", ["BTCUSDT", "ETHUSDT"])
        monkeypatch.setattr(screening, "analyze_coin",
            lambda symbol, check_correlation=True, active_symbols=None:
                None if symbol == "BTCUSDT" else _analysis("ETHUSDT", {"RSI_STRATEGY": ("BUY", 50.0)}),
        )

        selected = screening.screen_coins()

        assert [c["symbol"] for c in selected] == ["ETHUSDT"]


# ----------------------------------------------------------------
# API алдааг "позиц байхгүй" гэж андуурахгүй байх
#
# send_signed_request нь сүлжээний алдаанд {"code": -9999} буцаадаг. Өмнө нь
# get_positions үүнийг хоосон жагсаалт болгож хувиргадаг байсан тул нэг л
# сүлжээний саатал дараах гинжин урвалыг өдөөж байв:
#   амьд позицууд "хаагдсан" болно → SL/TP цуцлагдана → хөтлөлтөөс хасагдана
# ----------------------------------------------------------------

def _api_failure(*args, **kwargs):
    return {"code": -9999, "msg": "Connection timeout"}


class TestGetPositionsFailure:
    def test_api_error_raises_instead_of_empty_list(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request", _api_failure)

        with pytest.raises(account.PositionFetchError):
            account.get_positions()

    def test_genuinely_empty_list_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request", lambda *a, **kw: [])

        assert account.get_positions() == []

    def test_zero_amount_positions_are_filtered(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request", lambda *a, **kw: [
            {"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"},
            {"symbol": "ETHUSDT", "positionAmt": "1.5", "entryPrice": "100",
             "markPrice": "105", "unRealizedProfit": "7.5"},
        ])

        positions = account.get_positions()

        assert [p["symbol"] for p in positions] == ["ETHUSDT"]


class TestMonitorPositionsOnApiFailure:
    def test_open_trades_are_not_treated_as_closed(self, monkeypatch, monitor_env):
        monkeypatch.setattr(binance_client, "send_signed_request", _api_failure)
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})

        position_manager.monitor_positions()

        assert monitor_env == []
        assert "BTCUSDT" in bot_state.active_trade_info

    def test_protection_orders_are_not_cancelled(self, monkeypatch, monitor_env):
        cancelled = []
        monkeypatch.setattr(order_api, "cancel_all_symbol_orders", lambda symbol: cancelled.append(symbol))
        monkeypatch.setattr(binance_client, "send_signed_request", _api_failure)
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})

        position_manager.monitor_positions()

        assert cancelled == []


class TestCloseAllOnApiFailure:
    def test_unreadable_positions_do_not_report_success(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request", _api_failure)

        assert position_manager.close_all_positions_and_verify() is False

    def test_verify_failure_does_not_report_all_closed(self, monkeypatch):
        # Эхний унших нь амжилттай (1 позиц), баталгаажуулах уншилтууд алдаатай
        calls = {"n": 0}

        def flaky(method, endpoint, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100",
                         "markPrice": "100", "unRealizedProfit": "0"}]
            return {"code": -9999, "msg": "Connection timeout"}

        monkeypatch.setattr(binance_client, "send_signed_request", flaky)
        monkeypatch.setattr(order_api, "cancel_all_symbol_orders", lambda symbol: None)
        monkeypatch.setattr(position_manager, "close_one_position", lambda pos: None)
        patch_setting(monkeypatch, "CLOSE_VERIFY_ATTEMPTS", 2)
        patch_setting(monkeypatch, "CLOSE_VERIFY_DELAY_SEC", 0)
        monkeypatch.setattr(time, "sleep", lambda seconds: None)

        assert position_manager.close_all_positions_and_verify() is False


class TestSafetyRecoveryOnApiFailure:
    def test_lock_is_not_released_when_positions_unreadable(self, monkeypatch):
        bot_state.safety_lock = True
        monkeypatch.setattr(binance_client, "send_signed_request", _api_failure)

        assert position_manager.safety_recovery() is False
        assert bot_state.safety_lock is True

    def test_lock_released_when_genuinely_flat(self, monkeypatch, telegram_messages):
        bot_state.safety_lock = True
        monkeypatch.setattr(binance_client, "send_signed_request", lambda *a, **kw: [])

        assert position_manager.safety_recovery() is True
        assert bot_state.safety_lock is False


class TestTargetReachedOnApiFailure:
    def test_unverified_final_check_is_not_success(self, monkeypatch, target_env, telegram_messages):
        monkeypatch.setattr(bot_state, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(position_manager, "close_all_positions_and_verify", lambda: True)
        monkeypatch.setattr(binance_client, "send_signed_request", _api_failure)

        assert position_manager.handle_target_reached(310.0) is False
        assert bot_state.safety_lock is True
        assert any("UNVERIFIED" in m for m in telegram_messages)


class TestOrderPathSurvivesApiFailure:
    def test_trailing_activation_falls_back_to_entry_price(self, monkeypatch, fake_symbol_info):
        monkeypatch.setattr(binance_client, "send_signed_request", _api_failure)
        patch_setting(monkeypatch, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = position_manager.calculate_trailing_activation("BTCUSDT", "BUY", 100.0)

        # Алдаа шидэхгүй — хамгаалалтын захиалга үүсэх боломжтой хэвээр
        assert activation > 100.0

    def test_filled_order_still_gets_protection_when_positions_unreadable(
        self, monkeypatch, order_spy, fake_symbol_info, telegram_messages
    ):
        protections = []
        monkeypatch.setattr(account, "get_position_mode", lambda: False)
        monkeypatch.setattr(binance_client, "current_timestamp_ms", lambda: 1_700_000_000_000)
        monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side, **kw:
                                protections.append(symbol) or (True, 103.0, 101.0))
        monkeypatch.setattr(time, "sleep", lambda seconds: None)
        # Захиалгын өмнөх уншилт OK, захиалгын дараах уншилт алдаатай
        calls = {"n": 0}

        def flaky(method, endpoint, *a, **kw):
            calls["n"] += 1
            return [] if calls["n"] == 1 else {"code": -9999, "msg": "timeout"}

        monkeypatch.setattr(binance_client, "send_signed_request", flaky)

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert len(order_spy) == 1
        assert protections == ["BTCUSDT"]
        assert "BTCUSDT" in bot_state.active_trade_info


# ----------------------------------------------------------------
# RECOVERED позиц: ашиг нь бүртгэгдэх, стратеги нь сэргэх
#
# Restart-ын дараа sync_existing_positions позицуудыг авдаг. Өмнө нь тэдгээр нь
# "RECOVERED" болж, finalize_trade эрт `return 0.0` хийдэг байсан тул ашиг нь
# Session Realized-д огт тусахгүй, хаагдсан мэдэгдэл ч ирдэггүй байв.
# ----------------------------------------------------------------

class TestRecoveredTradeFinalization:
    def test_recovered_pnl_counts_toward_session(self, monkeypatch):
        monkeypatch.setattr(account, "get_trade_realized_pnl", lambda symbol, opened_at_ms: 17.80)

        pnl = position_manager.finalize_trade("BNBUSDT", _trade_info(strategy="RECOVERED"))

        assert pnl == pytest.approx(17.80)
        assert bot_state.session_realized_pnl == pytest.approx(17.80)

    def test_recovered_close_sends_notification(self, monkeypatch, telegram_messages):
        monkeypatch.setattr(account, "get_trade_realized_pnl", lambda symbol, opened_at_ms: 17.80)

        position_manager.finalize_trade("BNBUSDT", _trade_info(strategy="RECOVERED"))

        assert any("ПОЗИЦ ХААГДЛАА" in m for m in telegram_messages)

    def test_recovered_does_not_pollute_strategy_stats(self, monkeypatch):
        monkeypatch.setattr(account, "get_trade_realized_pnl", lambda symbol, opened_at_ms: 17.80)

        position_manager.finalize_trade("BNBUSDT", _trade_info(strategy="RECOVERED"))

        assert "RECOVERED" not in bot_state.strategy_stats
        assert all(s["trades"] == 0 for s in bot_state.strategy_stats.values())

    def test_known_strategy_close_updates_stats(self, monkeypatch):
        monkeypatch.setattr(account, "get_trade_realized_pnl", lambda symbol, opened_at_ms: 40.0)

        position_manager.finalize_trade("BTCUSDT", _trade_info(strategy="RSI_STRATEGY"))

        assert bot_state.strategy_stats["RSI_STRATEGY"]["trades"] == 1
        assert bot_state.session_realized_pnl == pytest.approx(40.0)



class TestStrategyRestoreAcrossRestart:
    """Нээлттэй арилжааны symbol → стратеги холбоос дискэнд хадгалагдаж,
    дахин асахад сэргэх ёстой."""

    def _live_position(self, monkeypatch, symbol="BTCUSDT", amt=1.0):
        monkeypatch.setattr(account, "get_positions", lambda: [_position(symbol, amt=amt)])
        monkeypatch.setattr(binance_client, "send_signed_request", lambda *a, **kw: [])
        monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side, **kw: (True, 103.0, 101.0))

    def test_saved_trade_is_written_to_disk(self):
        bot_state.active_trade_info["BTCUSDT"] = _trade_info(strategy="MACD_MOMENTUM")
        persistence.save_session_state()

        saved = persistence.load_session_state()

        assert saved["active_trades"]["BTCUSDT"]["strategy"] == "MACD_MOMENTUM"

    def test_strategy_is_restored_instead_of_recovered(self, monkeypatch):
        bot_state.active_trade_info["BTCUSDT"] = _trade_info(strategy="MACD_MOMENTUM", side="BUY")
        persistence.save_session_state()
        bot_state.active_trade_info = {}  # restart дуурайлгах
        self._live_position(monkeypatch)

        position_manager.sync_existing_positions()

        assert bot_state.active_trade_info["BTCUSDT"]["strategy"] == "MACD_MOMENTUM"

    def test_original_open_time_is_restored(self, monkeypatch):
        info = _trade_info(strategy="MACD_MOMENTUM", side="BUY")
        bot_state.active_trade_info["BTCUSDT"] = info
        persistence.save_session_state()
        bot_state.active_trade_info = {}
        self._live_position(monkeypatch)

        position_manager.sync_existing_positions()

        # opened_at_ms нь realized PnL-ийг хаанаас тоолохыг тодорхойлдог
        assert bot_state.active_trade_info["BTCUSDT"]["opened_at_ms"] == info["opened_at_ms"]

    def test_side_mismatch_falls_back_to_recovered(self, monkeypatch):
        # Хадгалсан бүртгэл SELL, биржид байгаа нь BUY — өөр арилжаа гэж үзнэ
        bot_state.active_trade_info["BTCUSDT"] = _trade_info(strategy="MACD_MOMENTUM", side="SELL")
        persistence.save_session_state()
        bot_state.active_trade_info = {}
        self._live_position(monkeypatch, amt=1.0)  # эерэг тоо = BUY

        position_manager.sync_existing_positions()

        assert bot_state.active_trade_info["BTCUSDT"]["strategy"] == "RECOVERED"

    def test_no_saved_record_falls_back_to_recovered(self, monkeypatch):
        self._live_position(monkeypatch)

        position_manager.sync_existing_positions()

        assert bot_state.active_trade_info["BTCUSDT"]["strategy"] == "RECOVERED"

    def test_stale_snapshot_is_ignored(self, monkeypatch, isolated_state_files):
        stale = {
            "session_peak_balance": 0.0,
            "saved_at": int(time.time()) - 200_000,  # 2 хоногийн өмнөх
            "active_trades": {"BTCUSDT": _trade_info(strategy="MACD_MOMENTUM", side="BUY")},
        }
        (isolated_state_files / "session_state.json").write_text(json.dumps(stale))
        self._live_position(monkeypatch)

        position_manager.sync_existing_positions()

        assert bot_state.active_trade_info["BTCUSDT"]["strategy"] == "RECOVERED"

    def test_unknown_strategy_name_is_not_trusted(self, monkeypatch):
        bot_state.active_trade_info["BTCUSDT"] = _trade_info(strategy="HACKED_STRATEGY", side="BUY")
        persistence.save_session_state()
        bot_state.active_trade_info = {}
        self._live_position(monkeypatch)

        position_manager.sync_existing_positions()

        assert bot_state.active_trade_info["BTCUSDT"]["strategy"] == "RECOVERED"

    def test_new_position_is_persisted_immediately(self, tradeable):
        execution.execute_trades([_coin()], total_balance=1000.0)

        saved = persistence.load_session_state()

        assert saved["active_trades"]["BTCUSDT"]["strategy"] == "RSI_STRATEGY"


# ----------------------------------------------------------------
# State хадгалах директор (Railway volume)
# ----------------------------------------------------------------

class TestStateStorageCheck:
    def test_creates_missing_directory(self, monkeypatch, tmp_path):
        target = tmp_path / "volume" / "nested"
        patch_setting(monkeypatch, "STATE_DIR", str(target))
        patch_setting(monkeypatch, "STATE_DIR_IS_PERSISTENT", True)

        assert persistence.check_state_storage() is True
        assert target.is_dir()

    def test_write_probe_is_cleaned_up(self, monkeypatch, tmp_path):
        patch_setting(monkeypatch, "STATE_DIR", str(tmp_path))
        patch_setting(monkeypatch, "STATE_DIR_IS_PERSISTENT", True)

        persistence.check_state_storage()

        assert list(tmp_path.iterdir()) == []

    def test_persistent_dir_sends_no_warning(self, monkeypatch, tmp_path, telegram_messages):
        patch_setting(monkeypatch, "STATE_DIR", str(tmp_path))
        patch_setting(monkeypatch, "STATE_DIR_IS_PERSISTENT", True)

        persistence.check_state_storage()

        assert telegram_messages == []

    def test_ephemeral_dir_warns(self, monkeypatch, tmp_path, telegram_messages):
        patch_setting(monkeypatch, "STATE_DIR", str(tmp_path))
        patch_setting(monkeypatch, "STATE_DIR_IS_PERSISTENT", False)

        assert persistence.check_state_storage() is True
        assert any("ТҮР ЗУУРЫН" in m for m in telegram_messages)

    def test_unwritable_dir_reports_failure(self, monkeypatch, telegram_messages):
        patch_setting(monkeypatch, "STATE_DIR", "/proc/definitely-not-writable")
        patch_setting(monkeypatch, "STATE_DIR_IS_PERSISTENT", True)

        assert persistence.check_state_storage() is False
        assert any("STATE STORAGE АЛДАА" in m for m in telegram_messages)


# ----------------------------------------------------------------
# Algo (conditional) захиалгын endpoint
#
# /fapi/v1/algoOpenOrders нь -5000 "Path is invalid" буцаадаг тул хуучин SL/TP
# хэзээ ч цуцлагддаггүй, мөн бүх позиц "хамгаалалтгүй" мэт харагддаг байв.
# ----------------------------------------------------------------

def _algo_order(order_id=101, order_type="STOP_MARKET", symbol="BTCUSDT", id_field="algoId"):
    return {"symbol": symbol, id_field: order_id, "orderType": order_type}


def _invalid_path(*args, **kwargs):
    return {"code": -5000, "msg": "Path is invalid"}


class TestAlgoEndpointDiscovery:
    def test_first_working_candidate_is_used(self, monkeypatch):
        tried = []

        def api(method, endpoint, params=None, **kw):
            tried.append(endpoint)
            if endpoint == "/fapi/v1/algoOrders":
                return [_algo_order()]
            return {"code": -5000, "msg": "Path is invalid"}

        monkeypatch.setattr(binance_client, "send_signed_request", api)

        assert order_api.discover_algo_list_endpoint("BTCUSDT") == "/fapi/v1/algoOrders"
        assert tried[0] == "/fapi/v1/openAlgoOrders"  # эхний хувилбараас эхэлнэ

    def test_result_is_cached(self, monkeypatch):
        calls = {"n": 0}

        def api(method, endpoint, params=None, **kw):
            calls["n"] += 1
            return [] if endpoint == "/fapi/v1/openAlgoOrders" else _invalid_path()

        monkeypatch.setattr(binance_client, "send_signed_request", api)

        order_api.discover_algo_list_endpoint("BTCUSDT")
        before = calls["n"]
        order_api.discover_algo_list_endpoint("BTCUSDT")

        assert calls["n"] == before

    def test_no_working_endpoint_alerts_once(self, monkeypatch, telegram_messages):
        monkeypatch.setattr(binance_client, "send_signed_request", _invalid_path)

        assert order_api.discover_algo_list_endpoint("BTCUSDT") is None
        order_api.discover_algo_list_endpoint("BTCUSDT")  # 2 дахь удаа

        assert len([m for m in telegram_messages if "ENDPOINT ОЛДСОНГҮЙ" in m]) == 1

    def test_wrapped_list_response_is_accepted(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request",
                            lambda m, e, p=None, **kw: {"orders": [_algo_order()]}
                            if e == "/fapi/v1/openAlgoOrders" else _invalid_path())

        assert order_api.discover_algo_list_endpoint("BTCUSDT") == "/fapi/v1/openAlgoOrders"


class TestGetOpenAlgoOrders:
    def test_returns_only_conditional_orders_for_symbol(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request", lambda m, e, p=None, **kw: [
            _algo_order(1, "STOP_MARKET"),
            _algo_order(2, "TAKE_PROFIT_MARKET"),
            _algo_order(3, "LIMIT"),                      # conditional биш
            _algo_order(4, "STOP_MARKET", symbol="ETHUSDT"),  # өөр symbol
        ] if e == "/fapi/v1/openAlgoOrders" else _invalid_path())

        orders = order_api.get_open_algo_orders("BTCUSDT")

        assert [o["algoId"] for o in orders] == [1, 2]

    def test_unknown_endpoint_returns_none_not_empty(self, monkeypatch):
        # None = "мэдэхгүй", [] = "байхгүй" — хоёрыг хольж болохгүй
        monkeypatch.setattr(binance_client, "send_signed_request", _invalid_path)

        assert order_api.get_open_algo_orders("BTCUSDT") is None

    def test_fetch_failure_after_discovery_returns_none(self, monkeypatch):
        # Endpoint нь олдсон ч дараагийн уншилт нь сүлжээний алдаанд унасан тохиолдол —
        # энэ нь "SL/TP байхгүй" гэсэн утга биш
        calls = {"n": 0}

        def api(method, endpoint, params=None, **kw):
            if endpoint != "/fapi/v1/openAlgoOrders":
                return _invalid_path()
            calls["n"] += 1
            return [] if calls["n"] == 1 else {"code": -9999, "msg": "Connection timeout"}

        monkeypatch.setattr(binance_client, "send_signed_request", api)
        order_api.discover_algo_list_endpoint("BTCUSDT")  # эхлээд endpoint-оо олно

        assert order_api.get_open_algo_orders("BTCUSDT") is None

    def test_genuinely_no_orders_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request",
                            lambda m, e, p=None, **kw: [] if e == "/fapi/v1/openAlgoOrders"
                            else _invalid_path())

        assert order_api.get_open_algo_orders("BTCUSDT") == []


class TestCancelAlgoOrders:
    def test_cancels_each_order_individually(self, monkeypatch):
        deleted = []

        def api(method, endpoint, params=None, **kw):
            if method == "GET" and endpoint == "/fapi/v1/openAlgoOrders":
                return [_algo_order(1), _algo_order(2, "TAKE_PROFIT_MARKET")]
            if method == "DELETE" and endpoint == "/fapi/v1/algoOrder":
                deleted.append(params)
                return {"status": "CANCELED"}
            return _invalid_path()

        monkeypatch.setattr(binance_client, "send_signed_request", api)

        order_api.cancel_all_algo_orders("BTCUSDT")

        assert [p["algoId"] for p in deleted] == [1, 2]

    def test_falls_back_to_order_id_field(self, monkeypatch):
        deleted = []

        def api(method, endpoint, params=None, **kw):
            if method == "GET" and endpoint == "/fapi/v1/openAlgoOrders":
                return [_algo_order(77, id_field="orderId")]
            if method == "DELETE":
                deleted.append(params)
                return {"status": "CANCELED"}
            return _invalid_path()

        monkeypatch.setattr(binance_client, "send_signed_request", api)

        order_api.cancel_all_algo_orders("BTCUSDT")

        assert deleted == [{"symbol": "BTCUSDT", "orderId": 77}]

    def test_unknown_endpoint_reports_api_error(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request", _invalid_path)

        result = order_api.cancel_all_algo_orders("BTCUSDT")

        # is_api_error-оор танигдах ёстой — дуудагч нь бүтэлгүйтлийг мэдэх ёстой
        assert utils.is_api_error(result) is True

    def test_no_open_orders_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request",
                            lambda m, e, p=None, **kw: [] if m == "GET" else _invalid_path())

        result = order_api.cancel_all_algo_orders("BTCUSDT")

        assert result == []
        assert utils.is_api_error(result) is False


class TestProtectionDetectionOnRecovery:
    def _recover(self, monkeypatch, algo_response):
        rebuilt = []
        monkeypatch.setattr(account, "get_positions", lambda: [_position("BTCUSDT")])
        monkeypatch.setattr(binance_client, "send_signed_request",
                            lambda m, e, p=None, **kw: algo_response(m, e))
        monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side, **kw:
                                rebuilt.append(symbol) or (True, 103.0, 101.0))
        position_manager.sync_existing_positions()
        return rebuilt

    def test_existing_protection_is_not_rebuilt(self, monkeypatch):
        rebuilt = self._recover(
            monkeypatch,
            lambda m, e: [_algo_order()] if e == "/fapi/v1/openAlgoOrders" else _invalid_path(),
        )

        assert rebuilt == []

    def test_missing_protection_is_rebuilt(self, monkeypatch):
        rebuilt = self._recover(
            monkeypatch,
            lambda m, e: [] if e == "/fapi/v1/openAlgoOrders" else _invalid_path(),
        )

        assert rebuilt == ["BTCUSDT"]

    def test_unknown_protection_state_rebuilds(self, monkeypatch):
        # Мэдэхгүй үед хамгаалалтгүй үлдэхээс давхардсан SL дээр нь
        rebuilt = self._recover(monkeypatch, lambda m, e: _invalid_path())

        assert rebuilt == ["BTCUSDT"]


# ----------------------------------------------------------------
# MTF шүүлтүүр
#
# Өмнө нь 4h/1h чиглэл зөрсөн (NEUTRAL) coin бүрмөсөн хаягддаг байсан бөгөөд
# үүний зэрэгцээ MTF-ийн эсрэг чиглэлийн арилжааг саадгүй нэвтрүүлдэг байв.
# ----------------------------------------------------------------

@pytest.fixture
def analyze_env(monkeypatch):
    """analyze_coin-ийн сүлжээний хамаарлыг мок болгоно."""
    df = noisy_uptrend_df(n=260)
    monkeypatch.setattr(market_data, "get_klines", lambda symbol, interval="1h", limit=200, **kw: df.copy())
    monkeypatch.setattr(market_data, "get_funding_rate", lambda symbol: 0.0)
    patch_setting(monkeypatch, "CORRELATION_ENABLED", False)
    patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 0.0)
    patch_setting(monkeypatch, "MTF_ENABLED", True)
    return df


class TestMtfFilter:
    def test_neutral_coin_is_still_analysed(self, monkeypatch, analyze_env):
        # Өмнө нь энэ None буцаадаг байсан — coin бүрмөсөн хаягддаг байв
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "NEUTRAL")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert result is not None
        assert result["mtf"] == "NEUTRAL"

    def test_bullish_mtf_blocks_sell_signals(self, monkeypatch, analyze_env):
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "BULLISH")
        monkeypatch.setattr(strategies, "generate_strategy_signal",
                            lambda strategy, df, sentiment, regime, chop=None: "SELL")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert all(r["signal"] == "HOLD" for r in result["strategies"].values())

    def test_bullish_mtf_allows_buy_signals(self, monkeypatch, analyze_env):
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "BULLISH")
        monkeypatch.setattr(strategies, "generate_strategy_signal",
                            lambda strategy, df, sentiment, regime, chop=None: "BUY")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert any(r["signal"] == "BUY" for r in result["strategies"].values())

    def test_bearish_mtf_blocks_buy_signals(self, monkeypatch, analyze_env):
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "BEARISH")
        monkeypatch.setattr(strategies, "generate_strategy_signal",
                            lambda strategy, df, sentiment, regime, chop=None: "BUY")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert all(r["signal"] == "HOLD" for r in result["strategies"].values())

    def test_neutral_mtf_allows_both_directions(self, monkeypatch, analyze_env):
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "NEUTRAL")
        monkeypatch.setattr(strategies, "generate_strategy_signal",
                            lambda strategy, df, sentiment, regime, chop=None: "SELL")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert any(r["signal"] == "SELL" for r in result["strategies"].values())

    def test_disabled_mtf_does_not_filter(self, monkeypatch, analyze_env):
        patch_setting(monkeypatch, "MTF_ENABLED", False)
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "NEUTRAL")
        monkeypatch.setattr(strategies, "generate_strategy_signal",
                            lambda strategy, df, sentiment, regime, chop=None: "SELL")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert any(r["signal"] == "SELL" for r in result["strategies"].values())


class TestLowScoreDiagnostic:
    """Оноогоор таслагдсан signal тайланд харагдах ёстой.

    Өмнө нь analyze_coin оноо багадвал signal-ыг HOLD болгодог тул
    screen_coins дахь шалгалт (BUY/SELL эсэх) хэзээ ч биелдэггүй байв.
    """

    def test_raw_signal_is_preserved_when_score_is_low(self, monkeypatch, analyze_env):
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 1000.0)  # бүгдийг таслана
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "BULLISH")
        monkeypatch.setattr(strategies, "generate_strategy_signal",
                            lambda strategy, df, sentiment, regime, chop=None: "BUY")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert all(r["signal"] == "HOLD" for r in result["strategies"].values())
        assert all(r["raw_signal"] == "BUY" for r in result["strategies"].values())

    def test_raw_signal_reflects_earlier_filters(self, monkeypatch, analyze_env):
        # MTF-ээр хаагдсан бол raw_signal ч HOLD байх ёстой — онооны буруу биш
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "BEARISH")
        monkeypatch.setattr(strategies, "generate_strategy_signal",
                            lambda strategy, df, sentiment, regime, chop=None: "BUY")

        result = screening.analyze_coin("BTCUSDT", check_correlation=False)

        assert all(r["raw_signal"] == "HOLD" for r in result["strategies"].values())

    def test_low_score_signals_are_reported(self, monkeypatch, screen_env):
        reported = {}
        monkeypatch.setattr(reports, "send_selection_report",
                            lambda selected, all_candidates=None, skipped_reasons=None:
                                reported.update(reasons=skipped_reasons))
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 50.0)

        analysis = _analysis("BTCUSDT", {"RSI_STRATEGY": ("HOLD", 30.0)})
        analysis["strategies"]["RSI_STRATEGY"]["raw_signal"] = "BUY"
        _use_analyses(monkeypatch, [analysis])

        screening.screen_coins()

        assert any("Оноо хэт бага" in r for r in reported["reasons"])


class TestLoggingSetup:
    def test_console_only_when_not_persistent(self, tmp_path):
        import logging_setup

        path = logging_setup.setup_logging(str(tmp_path), persistent=False)

        assert path is None
        assert not list(tmp_path.iterdir())

    def test_writes_file_on_persistent_volume(self, tmp_path):
        import logging_setup

        path = logging_setup.setup_logging(str(tmp_path), persistent=True)
        logging_setup.get_logger().error("❌ тестийн алдаа")

        assert path is not None
        assert "тестийн алдаа" in open(path, encoding="utf-8").read()

    def test_level_is_recorded(self, tmp_path):
        import logging_setup

        path = logging_setup.setup_logging(str(tmp_path), persistent=True)
        log = logging_setup.get_logger()
        log.warning("⚠️ анхааруулга")
        log.info("мэдээлэл")

        content = open(path, encoding="utf-8").read()
        assert "WARNING" in content
        assert "INFO" in content

    def test_unwritable_dir_does_not_raise(self):
        import logging_setup

        # Алдаа шидэхгүй, зөвхөн консол руу үлдэнэ
        assert logging_setup.setup_logging("/proc/not-writable", persistent=True) is None

    def test_no_state_dir_is_console_only(self):
        import logging_setup

        assert logging_setup.setup_logging(None, persistent=True) is None


class TestRealizedPnlFees:
    """realizedPnl нь шимтгэлгүй дүн — шимтгэл заавал хасагдах ёстой."""

    def _trades(self, monkeypatch, rows):
        monkeypatch.setattr(binance_client, "send_signed_request", lambda *a, **kw: rows)

    def test_commission_is_deducted(self, monkeypatch):
        self._trades(monkeypatch, [
            {"time": 2000, "realizedPnl": "100.0", "commission": "1.5",
             "commissionAsset": "USDT", "marginAsset": "USDT"},
        ])

        assert account.get_trade_realized_pnl("BTCUSDT", 1000) == pytest.approx(98.5)

    def test_fees_accumulate_across_fills(self, monkeypatch):
        self._trades(monkeypatch, [
            {"time": 2000, "realizedPnl": "60.0", "commission": "1.0",
             "commissionAsset": "USDT", "marginAsset": "USDT"},
            {"time": 2100, "realizedPnl": "40.0", "commission": "1.2",
             "commissionAsset": "USDT", "marginAsset": "USDT"},
        ])

        assert account.get_trade_realized_pnl("BTCUSDT", 1000) == pytest.approx(97.8)

    def test_small_gross_win_can_become_net_loss(self, monkeypatch):
        # Энэ л шалтгаанаар win rate хиймлээр өсдөг байсан
        self._trades(monkeypatch, [
            {"time": 2000, "realizedPnl": "1.0", "commission": "2.3",
             "commissionAsset": "USDT", "marginAsset": "USDT"},
        ])

        assert account.get_trade_realized_pnl("BTCUSDT", 1000) < 0

    def test_foreign_fee_asset_is_not_subtracted(self, monkeypatch):
        # BNB-ээр төлсөн шимтгэлийг USDT ашгаас шууд хасах нь нэгж зөрчинө
        self._trades(monkeypatch, [
            {"time": 2000, "realizedPnl": "50.0", "commission": "0.01",
             "commissionAsset": "BNB", "marginAsset": "USDT"},
        ])

        assert account.get_trade_realized_pnl("BTCUSDT", 1000) == pytest.approx(50.0)

    def test_missing_commission_field_is_safe(self, monkeypatch):
        self._trades(monkeypatch, [{"time": 2000, "realizedPnl": "25.0"}])

        assert account.get_trade_realized_pnl("BTCUSDT", 1000) == pytest.approx(25.0)

    def test_trades_before_open_time_are_ignored(self, monkeypatch):
        self._trades(monkeypatch, [
            {"time": 100, "realizedPnl": "999.0", "commission": "1.0",
             "commissionAsset": "USDT", "marginAsset": "USDT"},
            {"time": 20000, "realizedPnl": "10.0", "commission": "0.5",
             "commissionAsset": "USDT", "marginAsset": "USDT"},
        ])

        assert account.get_trade_realized_pnl("BTCUSDT", 10000) == pytest.approx(9.5)

    def test_api_error_returns_zero(self, monkeypatch):
        self._trades(monkeypatch, {"code": -9999, "msg": "timeout"})

        assert account.get_trade_realized_pnl("BTCUSDT", 1000) == 0.0


class TestDivisionGuards:
    def test_zero_leverage_falls_back_to_configured(self, monkeypatch):
        # leverage=0 нь margin тооцоололд 0-д хуваах алдаа өгч циклийг унагаана
        monkeypatch.setattr(binance_client, "send_signed_request",
                            lambda *a, **kw: [{"leverage": "0"}])
        patch_setting(monkeypatch, "LEVERAGE", 5)

        assert account.get_actual_leverage("BTCUSDT") == 5

    def test_valid_leverage_is_used_and_cached(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request",
                            lambda *a, **kw: [{"leverage": "10"}])

        assert account.get_actual_leverage("BTCUSDT") == 10
        assert bot_state.leverage_cache["BTCUSDT"] == 10

    def test_zero_leverage_is_not_cached(self, monkeypatch):
        monkeypatch.setattr(binance_client, "send_signed_request",
                            lambda *a, **kw: [{"leverage": "0"}])

        account.get_actual_leverage("BTCUSDT")

        assert "BTCUSDT" not in bot_state.leverage_cache

    def test_zero_price_skips_analysis(self, monkeypatch, analyze_env):
        zero = make_df([0.0] * 260)
        monkeypatch.setattr(market_data, "get_klines", lambda symbol, interval="1h", limit=200, **kw: zero.copy())
        monkeypatch.setattr(strategies, "get_mtf_signal", lambda symbol: "BULLISH")

        assert screening.analyze_coin("BTCUSDT", check_correlation=False) is None


class TestMtfScorePenalty:
    def _score(self, mtf_signal):
        return strategies.calculate_strategy_score(
            "SUPERTREND", adx=35, rsi=55, atr_pct=1.0, volume_ratio=2.0,
            ema_slope=1.0, sentiment=0.0, regime="TRENDING", chop=30,
            mtf_signal=mtf_signal,
        )

    def test_neutral_is_penalised_when_mtf_enabled(self, monkeypatch):
        patch_setting(monkeypatch, "MTF_ENABLED", True)

        assert self._score("NEUTRAL") == pytest.approx(self._score("BULLISH") - 5)

    def test_no_penalty_when_mtf_disabled(self, monkeypatch):
        # MTF унтраалттай бол get_mtf_signal үргэлж NEUTRAL буцаадаг тул
        # торгууль бүх стратегид ялгаагүй тусах ёсгүй
        patch_setting(monkeypatch, "MTF_ENABLED", False)

        assert self._score("NEUTRAL") == pytest.approx(self._score("BULLISH"))


class TestResampleOhlcv:
    def test_aggregates_ohlcv_correctly(self):
        df = make_df([10.0, 12.0, 8.0, 11.0], volumes=[1.0, 2.0, 3.0, 4.0])

        htf = indicators.resample_ohlcv(df, factor=4)

        assert len(htf) == 1
        assert htf["open"].iloc[0] == 10.0                  # эхнийх
        assert htf["close"].iloc[0] == 11.0                 # сүүлийнх
        assert htf["high"].iloc[0] == pytest.approx(12.0 * 1.01)   # хамгийн өндөр
        assert htf["low"].iloc[0] == pytest.approx(8.0 * 0.99)     # хамгийн нам
        assert htf["volume"].iloc[0] == 10.0                # нийлбэр

    def test_trims_from_the_front_so_last_bar_is_included(self):
        # 10 лаа, factor 4 → 2 бүтэн бүлэг, эхний 2 лаа тайрагдана
        df = make_df([float(i) for i in range(10)])

        htf = indicators.resample_ohlcv(df, factor=4)

        assert len(htf) == 2
        assert htf["close"].iloc[-1] == 9.0                 # сүүлийн лаа багтсан
        assert htf["open"].iloc[0] == 2.0                   # эхний 2 нь тайрагдсан

    def test_returns_none_when_too_short(self):
        assert indicators.resample_ohlcv(make_df([1.0, 2.0]), factor=4) is None

    def test_bar_count_is_input_divided_by_factor(self):
        htf = indicators.resample_ohlcv(make_df([float(i) for i in range(600)]), factor=4)

        assert len(htf) == 150


class TestFindStrongLevels:
    def test_levels_come_from_price_history(self):
        df = make_df([100.0, 110.0, 90.0, 105.0])

        support, resistance = market_data.find_strong_levels(df)

        # high = close * 1.01, low = close * 0.99
        assert support == pytest.approx(90.0 * 0.99)
        assert resistance == pytest.approx(110.0 * 1.01)

    def test_support_below_resistance_on_real_series(self):
        support, resistance = market_data.find_strong_levels(noisy_uptrend_df(n=260))

        assert support < resistance

    def test_lookback_window_is_respected(self):
        # Эхний лааны хэт өндөр утга lookback-аас гадуур үлдэх ёстой
        df = make_df([1000.0] + [100.0] * 120)

        _, resistance = market_data.find_strong_levels(df, lookback=100)

        assert resistance == pytest.approx(100.0 * 1.01)

    def test_empty_frame_returns_none(self):
        assert market_data.find_strong_levels(make_df([])) == (None, None)


# ----------------------------------------------------------------
# Хэсэгчилсэн take-profit (scale-out) ба breakeven stop
#
# Өмнө нь гарц ганц байсан: 4.5% TP эсвэл 3% SL. "+2% хүрээд буцаж SL цохисон"
# арилжаа бүтэн алдагдал болдог байв. Одоо позицын хагасыг 2% дээр тасалж
# аваад, үлдсэн хэсгийн stop-ыг breakeven руу зөөнө.
# ----------------------------------------------------------------

@pytest.fixture
def partial_tp_spy(monkeypatch, fake_symbol_info):
    """Хэсэгчилсэн TP-ийн conditional захиалгыг барьж аваад бүртгэнэ."""
    placed = []

    def fake(symbol, side, quantity, tp_price, position_side=None):
        placed.append({"symbol": symbol, "side": side, "quantity": quantity,
                       "tp_price": tp_price, "position_side": position_side})
        return {"orderId": 7}

    monkeypatch.setattr(order_api, "place_partial_take_profit_order", fake)
    monkeypatch.setattr(account, "get_position_mode", lambda: False)
    patch_setting(monkeypatch, "PARTIAL_TP_ENABLED", True)
    patch_setting(monkeypatch, "PARTIAL_TP_PCT", 2.0)
    patch_setting(monkeypatch, "PARTIAL_TP_RATIO", 0.5)
    return placed


class TestPlacePartialTakeProfit:
    def test_half_the_position_is_sold_at_the_configured_profit(self, partial_tp_spy):
        price = position_manager.place_partial_tp("BTCUSDT", "BUY", 4.0, 100.0, "BOTH")

        assert price == pytest.approx(102.0)
        assert len(partial_tp_spy) == 1
        assert partial_tp_spy[0]["quantity"] == pytest.approx(2.0)
        assert partial_tp_spy[0]["side"] == "SELL"
        assert partial_tp_spy[0]["tp_price"] == pytest.approx(102.0)

    def test_short_takes_profit_below_entry(self, partial_tp_spy):
        price = position_manager.place_partial_tp("BTCUSDT", "SELL", 4.0, 100.0, "BOTH")

        assert price == pytest.approx(98.0)
        assert partial_tp_spy[0]["side"] == "BUY"

    def test_ratio_controls_how_much_is_taken(self, monkeypatch, partial_tp_spy):
        patch_setting(monkeypatch, "PARTIAL_TP_RATIO", 0.25)

        position_manager.place_partial_tp("BTCUSDT", "BUY", 4.0, 100.0, "BOTH")

        assert partial_tp_spy[0]["quantity"] == pytest.approx(1.0)

    def test_disabled_places_no_order(self, monkeypatch, partial_tp_spy):
        patch_setting(monkeypatch, "PARTIAL_TP_ENABLED", False)

        assert position_manager.place_partial_tp("BTCUSDT", "BUY", 4.0, 100.0, "BOTH") is None
        assert partial_tp_spy == []

    def test_too_small_a_slice_is_skipped_not_rejected_by_exchange(self, partial_tp_spy):
        # BTCUSDT minNotional = 100. Хагас нь 0.5 * 102 = $51 → биржид татгалзагдана.
        assert position_manager.place_partial_tp("BTCUSDT", "BUY", 1.0, 100.0, "BOTH") is None
        assert partial_tp_spy == []

    def test_api_error_returns_none_so_no_phantom_partial_is_tracked(self, monkeypatch, partial_tp_spy):
        monkeypatch.setattr(order_api, "place_partial_take_profit_order",
                            lambda *a, **kw: {"code": -2021, "msg": "would immediately trigger"})

        assert position_manager.place_partial_tp("BTCUSDT", "BUY", 4.0, 100.0, "BOTH") is None


@pytest.fixture
def breakeven_env(monkeypatch):
    """rebuild_protection_orders-ийг барьж, ямар stop-той дуудагдсаныг бүртгэнэ."""
    rebuilds = []

    def fake_rebuild(symbol, side, qty, entry, pos_side, stop_price=None, levels=None):
        rebuilds.append({"symbol": symbol, "side": side, "qty": qty,
                         "entry": entry, "stop_price": stop_price})
        return True, 104.5, 103.0

    monkeypatch.setattr(position_manager, "rebuild_protection_orders", fake_rebuild)
    monkeypatch.setattr(market_data, "round_price", lambda symbol, price: round(price, 4))
    patch_setting(monkeypatch, "PARTIAL_TP_ENABLED", True)
    patch_setting(monkeypatch, "PARTIAL_TP_RATIO", 0.5)
    patch_setting(monkeypatch, "PARTIAL_TP_PCT", 2.0)
    patch_setting(monkeypatch, "BREAKEVEN_OFFSET_PCT", 0.1)
    return rebuilds


def _partially_filled_trade(side="BUY", quantity=4.0):
    trade = _trade_info(side=side)
    trade["quantity"] = quantity
    trade["partial_tp_price"] = 102.0 if side == "BUY" else 98.0
    trade["breakeven_done"] = False
    return trade


class TestBreakevenAfterPartialFill:
    def test_halved_position_moves_stop_to_breakeven(self, breakeven_env):
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade()

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=2.0)])

        assert len(breakeven_env) == 1
        # entry 100 + 0.1% шимтгэлийн зай
        assert breakeven_env[0]["stop_price"] == pytest.approx(100.1)
        assert breakeven_env[0]["qty"] == pytest.approx(2.0)

    def test_short_breakeven_stop_sits_below_entry(self, breakeven_env):
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade(side="SELL")

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=-2.0)])

        assert breakeven_env[0]["stop_price"] == pytest.approx(99.9)

    def test_tracked_quantity_follows_the_remaining_position(self, breakeven_env):
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade()

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=2.0)])

        assert bot_state.active_trade_info["BTCUSDT"]["quantity"] == pytest.approx(2.0)
        assert bot_state.active_trade_info["BTCUSDT"]["breakeven_done"] is True

    def test_intact_position_keeps_its_original_stop(self, breakeven_env):
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade()

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=4.0)])

        assert breakeven_env == []

    def test_breakeven_is_not_repeated_on_later_cycles(self, breakeven_env):
        # Хоёр дахь удаа позиц ЦААШ багассан ч (гараар хэсэгчлэн хаасан,
        # эсвэл хоёр хэсгээр биелсэн) breakeven-ийг дахин барихгүй — эс тэгвээс
        # мөчлөг тутамд бүх хамгаалалтаа цуцлаад дахин барих эрсдэлтэй.
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade()

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=2.0)])
        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=1.0)])

        assert len(breakeven_env) == 1

    def test_position_without_a_partial_tp_is_left_alone(self, breakeven_env):
        # Partial TP байрлаагүй (API алдаа) байхад хэмжээ буурсан бол энэ нь
        # гараар хаасан гэсэн үг — түүнийг "TP биеллээ" гэж андуурч болохгүй.
        trade = _partially_filled_trade()
        trade["partial_tp_price"] = None
        bot_state.active_trade_info["BTCUSDT"] = trade

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=2.0)])

        assert breakeven_env == []

    def test_failed_rebuild_is_retried_and_flagged_unprotected(self, monkeypatch, breakeven_env, telegram_messages):
        monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side, **kw: (False, None, None))
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade()

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=2.0)])

        trade = bot_state.active_trade_info["BTCUSDT"]
        assert trade["breakeven_done"] is False       # дараагийн мөчлөгт дахин оролдоно
        assert trade["quantity"] == pytest.approx(4.0)
        assert "BTCUSDT" in bot_state.unprotected_symbols
        assert any("BREAKEVEN" in m for m in telegram_messages)

    def test_disabled_feature_never_moves_the_stop(self, monkeypatch, breakeven_env):
        patch_setting(monkeypatch, "PARTIAL_TP_ENABLED", False)
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade()

        position_manager.check_partial_tp_fills([_position("BTCUSDT", amt=2.0)])

        assert breakeven_env == []

    def test_monitor_positions_performs_the_breakeven_move(self, monkeypatch, breakeven_env, monitor_env):
        monkeypatch.setattr(account, "get_positions", lambda: [_position("BTCUSDT", amt=2.0)])
        bot_state.active_trade_info["BTCUSDT"] = _partially_filled_trade()

        position_manager.monitor_positions()

        assert len(breakeven_env) == 1


# ----------------------------------------------------------------
# Мэдээний цагийн хуваарь
#
# Эвентийн өмнөх түр зогсоолт ба эвентийн дараах арилжаа хоёр тусдаа флагтай:
# зогсоолт нь spike дээр stop цохиулахаас хамгаалдаг тул дангаараа утгатай.
# ----------------------------------------------------------------

@pytest.fixture
def news_env(monkeypatch):
    patch_setting(monkeypatch, "NEWS_ENABLED", True)
    patch_setting(monkeypatch, "NEWS_PAUSE_BEFORE", 30)
    patch_setting(monkeypatch, "NEWS_WAIT_AFTER", 15)
    bot_state.last_news_check = datetime.now(pytz.UTC)


def _event_in(minutes):
    return datetime.now(pytz.UTC) + timedelta(minutes=minutes)


class TestNewsPauseWindow:
    def test_new_technical_trades_pause_before_the_event(self, news_env):
        bot_state.next_news_time = _event_in(10)

        news.check_news_status()

        assert bot_state.news_mode_active is True

    def test_no_pause_while_the_event_is_still_far_away(self, news_env):
        bot_state.next_news_time = _event_in(120)

        news.check_news_status()

        assert bot_state.news_mode_active is False

    def test_pause_holds_through_the_settling_window(self, news_env):
        bot_state.next_news_time = _event_in(-5)

        news.check_news_status()

        assert bot_state.news_mode_active is True

    def test_disabled_module_never_pauses(self, monkeypatch, news_env):
        patch_setting(monkeypatch, "NEWS_ENABLED", False)
        bot_state.next_news_time = _event_in(10)

        news.check_news_status()

        assert bot_state.news_mode_active is False


class TestPostNewsTradeIsOptional:
    def _run_cooldown_end(self, monkeypatch):
        traded = []
        monkeypatch.setattr(news, "execute_post_news_trade", lambda: traded.append(1))
        bot_state.next_news_time = _event_in(-20)
        bot_state.news_mode_active = True
        bot_state.news_trade_done = False
        news.check_news_status()
        return traded

    def test_trade_is_skipped_when_only_the_pause_is_enabled(self, monkeypatch, news_env):
        patch_setting(monkeypatch, "NEWS_POST_TRADE_ENABLED", False)

        traded = self._run_cooldown_end(monkeypatch)

        assert traded == []
        assert bot_state.news_mode_active is False   # техник арилжаа үргэлжилнэ

    def test_trade_runs_when_explicitly_enabled(self, monkeypatch, news_env):
        patch_setting(monkeypatch, "NEWS_POST_TRADE_ENABLED", True)

        assert self._run_cooldown_end(monkeypatch) == [1]

    def test_execute_itself_refuses_when_disabled(self, monkeypatch, news_env):
        patch_setting(monkeypatch, "NEWS_POST_TRADE_ENABLED", False)
        monkeypatch.setattr(market_data, "get_klines",
                            lambda *a, **kw: pytest.fail("арилжаа хийх ёсгүй"))

        news.execute_post_news_trade()


def _fake_requests(json_body=None, status=200, text="", content_type="application/json", boom=None):
    """news.requests-ийг орлох хамгийн бага хэрэгжүүлэлт."""
    class R:
        status_code = status
        headers = {"Content-Type": content_type}

        def __init__(self):
            self.text = text

        @staticmethod
        def json():
            if json_body is None:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return json_body

    class FakeRequests:
        calls = []

        @staticmethod
        def get(url, timeout=None, headers=None):
            FakeRequests.calls.append({"url": url, "headers": headers or {}})
            if boom:
                raise boom
            return R()

    return FakeRequests


class TestNewsCalendarParsing:
    def _calendar(self, monkeypatch, items):
        monkeypatch.setattr(news, "requests", _fake_requests(json_body=items))
        patch_setting(monkeypatch, "NEWS_CALENDAR_URL", "https://example.invalid/cal.json")
        patch_setting(monkeypatch, "NEWS_EVENT_KEYWORDS", ["CPI", "FOMC"])
        return news.get_next_news_event()

    def _item(self, title, minutes, country="USD"):
        return {"title": title, "country": country,
                "date": _event_in(minutes).isoformat()}

    def test_earliest_upcoming_event_wins_regardless_of_file_order(self, monkeypatch):
        result = self._calendar(monkeypatch, [
            self._item("CPI m/m", 3000),
            self._item("FOMC Statement", 120),
        ])

        assert (result - datetime.now(pytz.UTC)).total_seconds() / 60 == pytest.approx(120, abs=1)

    def test_past_events_are_ignored(self, monkeypatch):
        result = self._calendar(monkeypatch, [
            self._item("CPI m/m", -60),
            self._item("FOMC Statement", 200),
        ])

        assert (result - datetime.now(pytz.UTC)).total_seconds() / 60 == pytest.approx(200, abs=1)

    def test_unrelated_titles_are_filtered_out(self, monkeypatch):
        assert self._calendar(monkeypatch, [self._item("Flash Manufacturing PMI", 60)]) is None

    def test_non_usd_events_are_filtered_out(self, monkeypatch):
        assert self._calendar(monkeypatch, [self._item("CPI y/y", 60, country="EUR")]) is None

    def test_malformed_rows_do_not_break_the_lookup(self, monkeypatch):
        result = self._calendar(monkeypatch, [
            "мөр биш",
            {"title": "CPI m/m", "country": "USD", "date": "огт огноо биш"},
            self._item("CPI m/m", 90),
        ])

        assert (result - datetime.now(pytz.UTC)).total_seconds() / 60 == pytest.approx(90, abs=1)


class TestRebuildProtectionStopLevel:
    """rebuild_protection_orders нь stop-ыг хаанаас авч байгаа вэ.

    breakeven зөөлт бүхэлдээ энэ параметр дээр тогтдог тул мок биш, жинхэнэ
    функцээр нь шалгана.
    """

    @pytest.fixture
    def protection_spy(self, monkeypatch, fake_symbol_info):
        placed = {}
        monkeypatch.setattr(order_api, "cancel_all_symbol_orders", lambda symbol: {"algo": []})
        monkeypatch.setattr(order_api, "place_stop_loss_order",
                            lambda symbol, side, qty, stop_price, position_side=None:
                                placed.__setitem__("sl", stop_price) or {"orderId": 1})
        monkeypatch.setattr(order_api, "place_take_profit_order",
                            lambda symbol, side, qty, tp_price, position_side=None:
                                placed.__setitem__("tp", tp_price) or {"orderId": 2})
        monkeypatch.setattr(order_api, "place_trailing_stop_order", lambda *a, **kw: {"orderId": 3})
        monkeypatch.setattr(position_manager, "calculate_trailing_activation",
                            lambda symbol, side, entry, trail_pct=None: 103.0)
        return placed

    def test_default_stop_is_the_emergency_level(self, monkeypatch, protection_spy):
        patch_setting(monkeypatch, "EMERGENCY_SL_PCT", 3.0)

        position_manager.rebuild_protection_orders("BTCUSDT", "BUY", 4.0, 100.0, "BOTH")

        assert protection_spy["sl"] == pytest.approx(97.0)

    def test_explicit_stop_price_overrides_the_emergency_level(self, protection_spy):
        position_manager.rebuild_protection_orders("BTCUSDT", "BUY", 4.0, 100.0, "BOTH", stop_price=100.1)

        assert protection_spy["sl"] == pytest.approx(100.1)

    def test_take_profit_target_is_untouched_by_the_stop_override(self, monkeypatch, protection_spy):
        patch_setting(monkeypatch, "TAKE_PROFIT_PCT", 4.5)

        position_manager.rebuild_protection_orders("BTCUSDT", "BUY", 4.0, 100.0, "BOTH", stop_price=100.1)

        assert protection_spy["tp"] == pytest.approx(104.5)

    def test_short_default_stop_sits_above_entry(self, monkeypatch, protection_spy):
        patch_setting(monkeypatch, "EMERGENCY_SL_PCT", 3.0)

        position_manager.rebuild_protection_orders("BTCUSDT", "SELL", 4.0, 100.0, "BOTH")

        assert protection_spy["sl"] == pytest.approx(103.0)


# ----------------------------------------------------------------
# ATR-аар эрсдэлээ тэнцүүлсэн хэмжээ ба гарц
#
# Өмнө нь coin болгон балансын ижил 9%-ийг авдаг байсан тул тайван ба
# хэлбэлзэлтэй coin-ы бодит эрсдэл олон дахин зөрдөг байв.
# ----------------------------------------------------------------

@pytest.fixture
def atr_sizing(monkeypatch):
    patch_setting(monkeypatch, "ATR_RISK_SIZING_ENABLED", True)
    patch_setting(monkeypatch, "EMERGENCY_SL_PCT", 3.0)
    patch_setting(monkeypatch, "TAKE_PROFIT_PCT", 4.5)
    patch_setting(monkeypatch, "PARTIAL_TP_PCT", 2.0)
    patch_setting(monkeypatch, "TRAILING_ACTIVATION_PCT", 3.0)
    patch_setting(monkeypatch, "ATR_SL_MULTIPLIER", 2.0)
    patch_setting(monkeypatch, "ATR_SL_MIN_PCT", 2.0)
    patch_setting(monkeypatch, "ATR_SL_MAX_PCT", 5.0)
    patch_setting(monkeypatch, "RISK_PER_TRADE_PCT", 1.25)
    patch_setting(monkeypatch, "MIN_TRADE_ALLOCATION", 0.05)
    patch_setting(monkeypatch, "MAX_TRADE_ALLOCATION", 0.13)
    patch_setting(monkeypatch, "LEVERAGE", 5)
    patch_setting(monkeypatch, "TRADE_ALLOCATION", 0.09)


class TestAtrExitLevels:
    def test_stop_widens_with_volatility(self, atr_sizing):
        calm = risk.exit_levels(0.6)      # 2 * 0.6 = 1.2 → доод хашлага 2.0
        rough = risk.exit_levels(2.0)     # 2 * 2.0 = 4.0

        assert calm["sl"] == pytest.approx(2.0)
        assert rough["sl"] == pytest.approx(4.0)

    def test_reward_to_risk_ratio_is_preserved_at_every_volatility(self, atr_sizing):
        for atr in (0.5, 1.2, 2.0, 4.0):
            levels = risk.exit_levels(atr)
            assert levels["tp"] / levels["sl"] == pytest.approx(4.5 / 3.0)
            assert levels["partial"] / levels["sl"] == pytest.approx(2.0 / 3.0)
            assert levels["trail"] / levels["sl"] == pytest.approx(1.0)

    def test_stop_is_clamped_at_both_ends(self, atr_sizing):
        assert risk.exit_levels(0.1)["sl"] == pytest.approx(2.0)
        assert risk.exit_levels(9.0)["sl"] == pytest.approx(5.0)

    def test_disabled_returns_the_configured_levels_untouched(self, monkeypatch, atr_sizing):
        patch_setting(monkeypatch, "ATR_RISK_SIZING_ENABLED", False)

        levels = risk.exit_levels(2.0)

        assert levels == {"sl": 3.0, "tp": 4.5, "partial": 2.0, "trail": 3.0}

    def test_missing_atr_falls_back_to_the_configured_levels(self, atr_sizing):
        assert risk.exit_levels(None)["sl"] == pytest.approx(3.0)
        assert risk.exit_levels(0.0)["sl"] == pytest.approx(3.0)


class TestRiskBasedPositionMargin:
    def test_loss_at_stop_is_the_same_share_of_balance_whatever_the_volatility(self, atr_sizing):
        balance = 10_000.0
        for atr in (1.1, 1.5, 2.0):          # хашлагад хүрэхгүй муж
            sl = risk.exit_levels(atr)["sl"]
            notional = risk.position_margin(balance, sl) * 5
            assert notional * sl / 100 == pytest.approx(balance * 0.0125)

    def test_a_volatile_coin_gets_a_smaller_position(self, atr_sizing):
        calm = risk.position_margin(10_000.0, risk.exit_levels(1.1)["sl"])
        rough = risk.position_margin(10_000.0, risk.exit_levels(2.5)["sl"])

        assert rough < calm

    def test_margin_is_capped_so_six_positions_stay_possible(self, atr_sizing):
        # Маш нарийн stop нь эрсдэлийн томьёогоор асар том позиц гаргана
        assert risk.position_margin(10_000.0, 0.4) == pytest.approx(1_300.0)

    def test_margin_has_a_floor_so_tiny_trades_are_not_opened(self, atr_sizing):
        assert risk.position_margin(10_000.0, 20.0) == pytest.approx(500.0)

    def test_disabled_falls_back_to_flat_allocation(self, monkeypatch, atr_sizing):
        patch_setting(monkeypatch, "ATR_RISK_SIZING_ENABLED", False)

        assert risk.position_margin(10_000.0, 3.0) == pytest.approx(900.0)


class TestAtrSizingReachesTheOrder:
    def test_volatile_coin_is_traded_smaller_than_a_calm_one(self, monkeypatch, tradeable, atr_sizing):
        calm = _coin(price=100.0)
        calm["atr_pct"] = 1.1
        execution.execute_trades([calm], total_balance=10_000.0)
        calm_qty = tradeable[0]["quantity"]

        bot_state.active_trade_info.clear()
        tradeable.clear()
        rough = _coin(price=100.0)
        rough["atr_pct"] = 2.5
        execution.execute_trades([rough], total_balance=10_000.0)

        assert tradeable[0]["quantity"] < calm_qty

    def test_protection_orders_are_built_from_the_same_levels(self, monkeypatch, tradeable, atr_sizing):
        captured = {}
        monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side, **kw:
                                captured.update(kw) or (True, 103.0, 101.0))
        coin = _coin(price=100.0)
        coin["atr_pct"] = 2.0

        execution.execute_trades([coin], total_balance=10_000.0)

        assert captured["levels"]["sl"] == pytest.approx(4.0)
        assert captured["levels"]["tp"] == pytest.approx(6.0)

    def test_partial_take_profit_uses_the_scaled_level(self, monkeypatch, tradeable, atr_sizing):
        captured = {}
        monkeypatch.setattr(position_manager, "place_partial_tp",
                            lambda symbol, side, qty, entry, pos_side, **kw:
                                captured.update(kw) or 102.7)
        coin = _coin(price=100.0)
        coin["atr_pct"] = 2.0

        execution.execute_trades([coin], total_balance=10_000.0)

        assert captured["partial_pct"] == pytest.approx(4.0 * 2.0 / 3.0)

    def test_entry_conditions_are_captured_for_the_journal(self, tradeable, atr_sizing):
        coin = _coin(price=100.0)
        coin["atr_pct"] = 2.0
        coin["volume_ratio"] = 2.7

        execution.execute_trades([coin], total_balance=10_000.0)

        context = bot_state.active_trade_info["BTCUSDT"]["entry_context"]
        assert context["score"] == pytest.approx(50.0)
        assert context["regime"] == "RANGE"
        assert context["volume_ratio"] == pytest.approx(2.7)

    def test_the_trade_records_the_levels_it_was_opened_with(self, monkeypatch, tradeable, atr_sizing):
        coin = _coin(price=100.0)
        coin["atr_pct"] = 2.0
        execution.execute_trades([coin], total_balance=10_000.0)

        assert bot_state.active_trade_info["BTCUSDT"]["levels"]["sl"] == pytest.approx(4.0)


# ----------------------------------------------------------------
# Хугацааны stop
#
# Чиглэлээ өгөөгүй позиц слот, маржин, funding идсээр байдаг мөртлөө SL ч TP ч
# цохихгүй тул өөрөө хэзээ ч дуусахгүй.
# ----------------------------------------------------------------

@pytest.fixture
def time_stop_env(monkeypatch, order_spy, fake_symbol_info):
    monkeypatch.setattr(account, "get_position_mode", lambda: False)
    monkeypatch.setattr(order_api, "cancel_all_symbol_orders", lambda symbol: None)
    patch_setting(monkeypatch, "MAX_HOLD_HOURS", 24)
    patch_setting(monkeypatch, "TIME_STOP_FLAT_PCT", 1.0)
    return order_spy


def _stale_trade(side="BUY", hours_ago=30):
    trade = _trade_info(side=side)
    trade["opened_at"] = time.time() - hours_ago * 3600
    return trade


class TestTimeStop:
    def test_flat_position_past_the_deadline_is_closed(self, time_stop_env):
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade()

        position_manager.check_time_stops([_position("BTCUSDT", amt=1.0, entry=100.0, mark=100.4)])

        assert len(time_stop_env) == 1
        assert time_stop_env[0]["side"] == "SELL"

    def test_a_young_position_is_left_alone(self, time_stop_env):
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade(hours_ago=2)

        position_manager.check_time_stops([_position("BTCUSDT", amt=1.0, entry=100.0, mark=100.4)])

        assert time_stop_env == []

    def test_a_position_that_is_actually_moving_is_left_to_its_stops(self, time_stop_env):
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade()

        position_manager.check_time_stops([_position("BTCUSDT", amt=1.0, entry=100.0, mark=102.5)])

        assert time_stop_env == []

    def test_a_losing_position_is_also_left_to_its_stop(self, time_stop_env):
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade()

        position_manager.check_time_stops([_position("BTCUSDT", amt=1.0, entry=100.0, mark=97.5)])

        assert time_stop_env == []

    def test_a_moving_short_is_left_to_its_stops(self, time_stop_env):
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade(side="SELL")

        position_manager.check_time_stops([_position("BTCUSDT", amt=-1.0, entry=100.0, mark=97.5)])

        assert time_stop_env == []

    def test_a_short_reports_its_move_as_profit_not_as_price(self, time_stop_env, telegram_messages):
        # Short дээр үнэ 0.4% ӨСӨХ нь 0.4% АЛДАГДАЛ. Түүхий үнийн хөдөлгөөнийг
        # хэвлэвэл тайлан эсрэгээрээ уншигдана.
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade(side="SELL")

        position_manager.check_time_stops([_position("BTCUSDT", amt=-1.0, entry=100.0, mark=100.4)])

        assert len(time_stop_env) == 1
        assert any("-0.40%" in m for m in telegram_messages)

    def test_close_order_is_not_resent_every_cycle(self, time_stop_env):
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade()
        pos = _position("BTCUSDT", amt=1.0, entry=100.0, mark=100.4)

        position_manager.check_time_stops([pos])
        position_manager.check_time_stops([pos])

        assert len(time_stop_env) == 1

    def test_disabled_when_no_deadline_is_configured(self, monkeypatch, time_stop_env):
        patch_setting(monkeypatch, "MAX_HOLD_HOURS", 0)
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade()

        position_manager.check_time_stops([_position("BTCUSDT", amt=1.0, entry=100.0, mark=100.4)])

        assert time_stop_env == []

    def test_exit_reason_is_recorded_for_the_journal(self, time_stop_env):
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade()

        position_manager.check_time_stops([_position("BTCUSDT", amt=1.0, entry=100.0, mark=100.4)])

        assert bot_state.active_trade_info["BTCUSDT"]["exit_reason"] == "TIME_STOP"

    def test_monitor_positions_applies_the_time_stop(self, monkeypatch, time_stop_env, monitor_env):
        monkeypatch.setattr(account, "get_positions",
                            lambda: [_position("BTCUSDT", amt=1.0, entry=100.0, mark=100.4)])
        bot_state.active_trade_info["BTCUSDT"] = _stale_trade()

        position_manager.monitor_positions()

        assert len(time_stop_env) == 1


# ----------------------------------------------------------------
# Арилжааны бүртгэл (CSV)
#
# Тохиргоог таамгаар биш, хаагдсан арилжааны тоо баримтаар тааруулах суурь.
# ----------------------------------------------------------------

@pytest.fixture
def journal_env(monkeypatch, isolated_state_files):
    patch_setting(monkeypatch, "JOURNAL_ENABLED", True)
    monkeypatch.setattr(account, "get_trade_realized_pnl", lambda symbol, opened_at_ms: 12.5)
    return isolated_state_files / "trades.csv"


def _journalled_trade():
    trade = _trade_info(strategy="BREAKOUT", side="BUY")
    trade["entry_context"] = journal.entry_context({
        "score": 21.5, "adx": 31.0, "rsi": 44.0, "atr_pct": 1.8,
        "volume_ratio": 2.4, "ema_slope": 0.9, "regime": "TRENDING",
        "chop": 40.0, "sentiment": 0.5, "funding": 0.0001, "mtf": "BULLISH",
    })
    trade["levels"] = {"sl": 3.6, "tp": 5.4, "partial": 2.4, "trail": 3.6}
    return trade


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestTradeJournal:
    def test_a_closed_trade_is_written_with_its_entry_conditions(self, journal_env):
        position_manager.finalize_trade("BTCUSDT", _journalled_trade())

        row = _rows(journal_env)[0]
        assert row["symbol"] == "BTCUSDT"
        assert row["strategy"] == "BREAKOUT"
        assert float(row["pnl"]) == pytest.approx(12.5)
        assert float(row["score"]) == pytest.approx(21.5)
        assert row["regime"] == "TRENDING"
        assert row["mtf"] == "BULLISH"

    def test_the_levels_the_trade_actually_used_are_recorded(self, journal_env):
        position_manager.finalize_trade("BTCUSDT", _journalled_trade())

        row = _rows(journal_env)[0]
        assert float(row["sl_pct"]) == pytest.approx(3.6)
        assert float(row["tp_pct"]) == pytest.approx(5.4)

    def test_exit_reason_distinguishes_a_time_stop(self, journal_env):
        trade = _journalled_trade()
        trade["exit_reason"] = "TIME_STOP"

        position_manager.finalize_trade("BTCUSDT", trade)

        assert _rows(journal_env)[0]["exit_reason"] == "TIME_STOP"

    def test_header_is_written_once_and_trades_accumulate(self, journal_env):
        position_manager.finalize_trade("BTCUSDT", _journalled_trade())
        position_manager.finalize_trade("ETHUSDT", _journalled_trade())

        rows = _rows(journal_env)
        assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT"]

    def test_disabled_writes_nothing(self, monkeypatch, journal_env):
        patch_setting(monkeypatch, "JOURNAL_ENABLED", False)

        position_manager.finalize_trade("BTCUSDT", _journalled_trade())

        assert not journal_env.exists()

    def test_a_broken_journal_never_blocks_the_trade_accounting(self, monkeypatch, journal_env):
        monkeypatch.setattr(journal, "JOURNAL_FILE", "/proc/definitely/not/writable.csv")

        pnl = position_manager.finalize_trade("BTCUSDT", _journalled_trade())

        assert pnl == pytest.approx(12.5)
        assert bot_state.session_realized_pnl == pytest.approx(12.5)


# ----------------------------------------------------------------
# Календар уншигдахгүй үе
#
# Live log дээр "News calendar error: Expecting value: line 1 column 1" гэж
# 30 секунд тутам гарч байсан: хайлт бүтэлгүйтэхэд next_news_time нь None
# хэвээр үлдэж, "хоосон бол дахин ав" нөхцөл мөчлөг тутамд дахин оролддог байв.
# ----------------------------------------------------------------

class TestNewsCalendarFailures:
    def _fail(self, monkeypatch, **kw):
        fake = _fake_requests(**kw)
        monkeypatch.setattr(news, "requests", fake)
        patch_setting(monkeypatch, "NEWS_CALENDAR_URL", "https://example.invalid/cal.json")
        return fake

    def test_html_instead_of_json_is_reported_with_what_arrived(self, monkeypatch):
        self._fail(monkeypatch, json_body=None, text="<!DOCTYPE html><title>403</title>",
                   content_type="text/html")

        with pytest.raises(news.NewsCalendarError) as excinfo:
            news.get_next_news_event()

        assert "text/html" in str(excinfo.value)
        assert "DOCTYPE" in str(excinfo.value)

    def test_error_status_is_reported_before_parsing(self, monkeypatch):
        self._fail(monkeypatch, json_body=[], status=403)

        with pytest.raises(news.NewsCalendarError, match="403"):
            news.get_next_news_event()

    def test_a_non_list_body_is_a_failure_not_an_empty_calendar(self, monkeypatch):
        self._fail(monkeypatch, json_body={"error": "rate limited"})

        with pytest.raises(news.NewsCalendarError):
            news.get_next_news_event()

    def test_a_browser_user_agent_is_sent(self, monkeypatch):
        # Анхдагч python-requests UA-г олон үйлчилгээ блоклодог
        fake = self._fail(monkeypatch, json_body=[])

        news.get_next_news_event()

        assert "Mozilla" in fake.calls[0]["headers"]["User-Agent"]

    def test_an_unset_url_is_reported_rather_than_read_as_no_events(self, monkeypatch):
        # Тохируулаагүйг "эвент байхгүй" гэж чимээгүй өнгөрөөвөл хэрэглэгч
        # зогсоолт ажиллаж байна гэж эндүүрнэ.
        fake = self._fail(monkeypatch, json_body=[])
        patch_setting(monkeypatch, "NEWS_CALENDAR_URL", "")

        with pytest.raises(news.NewsCalendarError):
            news.get_next_news_event()

        assert fake.calls == []

    def test_an_empty_calendar_is_not_an_error(self, monkeypatch):
        self._fail(monkeypatch, json_body=[])

        assert news.get_next_news_event() is None


class TestNewsLookupBackoff:
    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        patch_setting(monkeypatch, "NEWS_ENABLED", True)
        patch_setting(monkeypatch, "NEWS_CALENDAR_URL", "https://example.invalid/cal.json")

    def _count_lookups(self, monkeypatch, result=None):
        calls = []

        def lookup():
            calls.append(1)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(news, "get_next_news_event", lookup)
        return calls

    def test_a_failed_lookup_is_not_retried_every_cycle(self, monkeypatch):
        calls = self._count_lookups(monkeypatch, news.NewsCalendarError("HTTP 403"))

        for _ in range(20):          # ~10 минутын мониторингийн мөчлөгүүд
            news.check_news_status()

        assert len(calls) == 1

    def test_the_retry_gap_widens_with_each_failure(self, monkeypatch):
        calls = self._count_lookups(monkeypatch, news.NewsCalendarError("HTTP 403"))

        news.check_news_status()
        # 15 минутын дараа хоёр дахь оролдлого
        bot_state.last_news_check = datetime.now(pytz.UTC) - timedelta(minutes=16)
        news.check_news_status()
        # Хоёр алдааны дараа завсар 30 минут — 16 минут хангалтгүй
        bot_state.last_news_check = datetime.now(pytz.UTC) - timedelta(minutes=16)
        news.check_news_status()

        assert len(calls) == 2

    def test_a_successful_lookup_is_cached_for_an_hour(self, monkeypatch):
        calls = self._count_lookups(monkeypatch, _event_in(600))

        for _ in range(10):
            news.check_news_status()

        assert len(calls) == 1

    def test_repeated_failures_warn_the_user_once(self, monkeypatch, telegram_messages):
        self._count_lookups(monkeypatch, news.NewsCalendarError("HTTP 403"))

        for _ in range(6):
            bot_state.last_news_check = None      # цаг хүлээхгүйгээр дахин оролдуулна
            news.check_news_status()

        alerts = [m for m in telegram_messages if "МЭДЭЭНИЙ ЗОГСООЛТ" in m]
        assert len(alerts) == 1

    def test_recovery_resets_the_backoff(self, monkeypatch):
        self._count_lookups(monkeypatch, news.NewsCalendarError("HTTP 403"))
        news.check_news_status()
        assert bot_state.news_lookup_failures == 1

        calls = self._count_lookups(monkeypatch, _event_in(600))
        bot_state.last_news_check = None
        news.check_news_status()

        assert bot_state.news_lookup_failures == 0
        assert len(calls) == 1

    def test_a_broken_calendar_never_pauses_trading(self, monkeypatch):
        self._count_lookups(monkeypatch, news.NewsCalendarError("HTTP 403"))

        news.check_news_status()

        assert bot_state.news_mode_active is False


# ----------------------------------------------------------------
# Мэдээний цонх нь ЗӨВХӨН шинэ арилжааг зогсооно
#
# Өмнө нь үндсэн гогцоо мэдээний цонхон дээр `continue` хийдэг байсан нь
# drawdown circuit breaker болон target шалгалтыг хамтад нь алгасдаг байв —
# яг эвентийн үед, өөрөөр хэлбэл тэдгээр хамгаалалт хамгийн хэрэгтэй мөчид.
# ----------------------------------------------------------------

class TestNewsWindowScope:
    def test_new_trades_are_blocked_during_the_window(self, tradeable):
        bot_state.news_mode_active = True

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []
        assert "BTCUSDT" not in bot_state.active_trade_info

    def test_trades_resume_once_the_window_closes(self, tradeable):
        bot_state.news_mode_active = False

        execution.execute_trades([_coin()], total_balance=1000.0)

        assert len(tradeable) == 1

    def test_the_drawdown_breaker_still_runs_during_the_window(self, monkeypatch):
        bot_state.news_mode_active = True
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 10.0)
        bot_state.session_peak_balance = 1000.0
        monkeypatch.setattr(account, "get_usdt_balance", lambda: 850.0)

        risk.check_drawdown_circuit_breaker()

        assert bot_state.drawdown_halt is True

    def test_position_monitoring_still_runs_during_the_window(self, monkeypatch, monitor_env):
        bot_state.news_mode_active = True
        bot_state.active_trade_info["BTCUSDT"] = _trade_info()
        monkeypatch.setattr(account, "get_positions", lambda: [])

        position_manager.monitor_positions()

        assert monitor_env == ["BTCUSDT"]      # хаагдсаныг илрүүлж бүртгэсэн


# ----------------------------------------------------------------
# Backtest: биржийн биелэлтийн дуураймал
#
# Энэ бол хамгийн эмзэг хэсэг — лаан доторх замыг мэдэхгүй тул дүрэм нь
# консерватив байх ёстой. Эсрэг тохиолдолд backtest нь бодит бус сайхан
# тоо гаргаж, шийдвэрийг эндүүрүүлнэ.
# ----------------------------------------------------------------

import backtest


def _bar(o, h, l, c, t=0):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def _sim():
    return {"open": {}, "trades": [], "realized": 0.0, "total_gross": 0.0,
            "total_fees": 0.0, "total_funding": 0.0, "equity_curve": [], "pending": []}


def _bt_pos(side="BUY", entry=100.0, qty=10.0, sl=3.0, tp=4.5, partial=2.0, trail=3.0):
    sign = 1 if side == "BUY" else -1
    return {
        "symbol": "BTCUSDT", "strategy": "RSI_STRATEGY", "side": side,
        "entry": entry, "qty": qty, "margin": entry * qty / 5,
        "levels": {"sl": sl, "tp": tp, "partial": partial, "trail": trail},
        "score": 20.0, "regime": "TRENDING", "atr_pct": 1.5,
        "stop_price": entry * (1 - sign * sl / 100),
        "tp_price": entry * (1 + sign * tp / 100),
        "partial_price": entry * (1 + sign * partial / 100),
        "trail_activation": entry * (1 + sign * trail / 100),
        "partial_done": False, "breakeven_done": False,
        "trail_armed": False, "trail_extreme": entry, "pending_breakeven": None,
        "opened_ms": 0, "gross": 0.0, "fees": 0.0, "funding": 0.0,
    }


@pytest.fixture
def no_costs(monkeypatch):
    """Биелэлтийн үнийг цэвэр харахын тулд шимтгэл/slippage-г тэглэнэ."""
    patch_setting(monkeypatch, "BACKTEST_FEE_RATE", 0.0)
    patch_setting(monkeypatch, "BACKTEST_SLIPPAGE_RATE", 0.0)
    patch_setting(monkeypatch, "PARTIAL_TP_RATIO", 0.5)
    patch_setting(monkeypatch, "BREAKEVEN_OFFSET_PCT", 0.1)
    patch_setting(monkeypatch, "TRAILING_CALLBACK_RATE", 1.0)


class TestBacktestFills:
    def test_stop_fills_at_the_stop_when_the_bar_reaches_it(self, no_costs):
        sim, pos = _sim(), _bt_pos()
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 100.5, 96.5, 98), sim)

        assert sim["trades"][0]["exit_reason"] == "STOP_LOSS"
        assert sim["trades"][0]["gross"] == pytest.approx((97.0 - 100.0) * 10)

    def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(self, no_costs):
        # Цоорхойг үл тоовол backtest алдагдлаа бодит бусаар багасгана
        sim, pos = _sim(), _bt_pos()
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(94, 95, 93, 94.5), sim)

        assert sim["trades"][0]["gross"] == pytest.approx((94.0 - 100.0) * 10)

    def test_a_bar_touching_both_stop_and_target_resolves_as_the_stop(self, no_costs):
        sim, pos = _sim(), _bt_pos()
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 106, 96, 101), sim)

        assert sim["trades"][0]["exit_reason"] == "STOP_LOSS"

    def test_take_profit_fills_when_only_the_upside_is_touched(self, no_costs):
        sim, pos = _sim(), _bt_pos()
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 106, 99.5, 105), sim)

        # Эхлээд partial (102), дараа нь үлдсэн нь TP (104.5)
        assert sim["trades"][0]["exit_reason"] == "TAKE_PROFIT"
        assert sim["trades"][0]["gross"] == pytest.approx(5 * 2.0 + 5 * 4.5)

    def test_partial_fill_leaves_the_rest_open(self, no_costs):
        sim, pos = _sim(), _bt_pos()
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 102.5, 99.5, 102), sim)

        assert sim["trades"] == []
        assert pos["qty"] == pytest.approx(5.0)
        assert pos["partial_done"] is True

    def test_breakeven_takes_effect_only_on_the_next_bar(self, no_costs):
        # Амьд бот partial биелэлтийг дараагийн мониторингийн мөчлөгт л хардаг.
        # Тэр саатлыг үл тоовол backtest бодит бусаар олон удаа breakeven-д гарна.
        sim, pos = _sim(), _bt_pos()
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 102.5, 99.9, 100.5), sim)
        assert pos["stop_price"] == pytest.approx(97.0)     # хараахан зөөгөөгүй

        backtest.advance_position(pos, _bar(100.5, 101, 100.2, 100.4), sim)
        assert pos["stop_price"] == pytest.approx(100.1)

    def test_trailing_cannot_arm_and_fire_inside_one_bar(self, no_costs):
        sim, pos = _sim(), _bt_pos(partial=99.0)   # partial-ыг хүрэхгүй болгов
        pos["partial_price"] = None
        sim["open"]["BTCUSDT"] = pos

        # 103-т хүрч arm болох ч тэр дороо 102 руу буусан — гарах ёсгүй
        backtest.advance_position(pos, _bar(100, 103.2, 101.9, 102), sim)

        assert sim["trades"] == []
        assert pos["trail_armed"] is True

    def test_trailing_exits_from_the_extreme_on_a_later_bar(self, no_costs):
        sim, pos = _sim(), _bt_pos()
        pos["partial_price"] = None
        pos["trail_armed"] = True
        pos["trail_extreme"] = 104.0
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(103.5, 103.6, 102.0, 102.5), sim)

        assert sim["trades"][0]["exit_reason"] == "TRAILING"
        assert sim["trades"][0]["gross"] == pytest.approx((104.0 * 0.99 - 100.0) * 10)

    def test_the_nearer_stop_triggers_first_on_the_way_down(self, no_costs):
        # Trailing stop (102.96) нь breakeven stop (100.1)-ээс дээр тул
        # уналтад эхэлж тааралдана — таамаг биш, физик.
        sim, pos = _sim(), _bt_pos()
        pos["partial_price"] = None
        pos["stop_price"] = 100.1
        pos["breakeven_done"] = True
        pos["trail_armed"] = True
        pos["trail_extreme"] = 104.0
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(103.5, 103.6, 99.0, 99.5), sim)

        assert sim["trades"][0]["exit_reason"] == "TRAILING"

    def test_short_stop_sits_above_entry(self, no_costs):
        sim, pos = _sim(), _bt_pos(side="SELL")
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 103.5, 99.5, 103), sim)

        assert sim["trades"][0]["exit_reason"] == "STOP_LOSS"
        assert sim["trades"][0]["gross"] == pytest.approx((100.0 - 103.0) * 10)

    def test_short_take_profit_sits_below_entry(self, no_costs):
        sim, pos = _sim(), _bt_pos(side="SELL")
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 100.5, 94, 95), sim)

        assert sim["trades"][0]["exit_reason"] == "TAKE_PROFIT"
        assert sim["trades"][0]["gross"] == pytest.approx(5 * 2.0 + 5 * 4.5)


class TestBacktestCosts:
    def test_slippage_always_works_against_the_trade(self, monkeypatch):
        patch_setting(monkeypatch, "BACKTEST_SLIPPAGE_RATE", 0.001)
        patch_setting(monkeypatch, "BACKTEST_FEE_RATE", 0.0)
        sim, pos = _sim(), _bt_pos()
        pos["partial_price"] = None
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 100.2, 96.5, 97), sim)

        # Long гарахдаа 97.0 биш, түүнээс ДООГУУР биелнэ
        assert sim["trades"][0]["gross"] < (97.0 - 100.0) * 10

    def test_every_leg_pays_a_fee(self, monkeypatch):
        patch_setting(monkeypatch, "BACKTEST_FEE_RATE", 0.0004)
        patch_setting(monkeypatch, "BACKTEST_SLIPPAGE_RATE", 0.0)
        patch_setting(monkeypatch, "PARTIAL_TP_RATIO", 0.5)
        sim, pos = _sim(), _bt_pos()
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 106, 99.5, 105), sim)

        # partial (5 @ 102) + үлдсэн (5 @ 104.5)
        assert sim["trades"][0]["fees"] == pytest.approx((5 * 102 + 5 * 104.5) * 0.0004)

    def test_the_entry_leg_pays_its_own_fee(self, monkeypatch):
        # Орох шимтгэл нь нийт зардлын тал хувь — үүнийг мартвал backtest
        # системтэйгээр арилжаа тутамд 0.04% илүү ашигтай харагдана.
        patch_setting(monkeypatch, "BACKTEST_FEE_RATE", 0.0004)
        patch_setting(monkeypatch, "BACKTEST_SLIPPAGE_RATE", 0.0)
        patch_setting(monkeypatch, "ATR_RISK_SIZING_ENABLED", False)
        patch_setting(monkeypatch, "TRADE_ALLOCATION", 0.09)
        patch_setting(monkeypatch, "LEVERAGE", 5)
        sim = _sim()

        pos = backtest._open_position(sim, _coin(price=100.0), _bar(100, 101, 99, 100.5),
                                      equity=10_000.0, margin_used=0.0)

        # маржин 900 × 5 = notional 4500 → шимтгэл 4500 × 0.0004 = 1.80
        assert pos["fees"] == pytest.approx(1.80)
        assert sim["total_fees"] == pytest.approx(1.80)
        assert sim["realized"] == pytest.approx(-1.80)

    def test_entry_slippage_also_works_against_the_trade(self, monkeypatch):
        patch_setting(monkeypatch, "BACKTEST_SLIPPAGE_RATE", 0.001)
        patch_setting(monkeypatch, "BACKTEST_FEE_RATE", 0.0)

        long_pos = backtest._open_position(_sim(), _coin(signal="BUY"), _bar(100, 101, 99, 100.5),
                                           equity=10_000.0, margin_used=0.0)
        short_pos = backtest._open_position(_sim(), _coin(signal="SELL"), _bar(100, 101, 99, 100.5),
                                            equity=10_000.0, margin_used=0.0)

        assert long_pos["entry"] == pytest.approx(100.1)    # долгүй үнээр биш, илүү үнэтэй
        assert short_pos["entry"] == pytest.approx(99.9)    # short нь илүү хямдаар зарна

    def test_net_is_gross_minus_every_cost(self, monkeypatch):
        patch_setting(monkeypatch, "BACKTEST_FEE_RATE", 0.0004)
        sim, pos = _sim(), _bt_pos()
        pos["partial_price"] = None
        pos["funding"] = 1.25
        sim["open"]["BTCUSDT"] = pos

        backtest.advance_position(pos, _bar(100, 100.2, 96.5, 97), sim)

        trade = sim["trades"][0]
        assert trade["net"] == pytest.approx(trade["gross"] - trade["fees"] - trade["funding"])


# ----------------------------------------------------------------
# Backtest: портфелийн бүтэн гогцоо
#
# Синтетик өгөгдөл дээр эхнээс нь дуустал ажиллуулж, ботын дүрмүүд
# (слотын тоо, lookahead байхгүй, зардал) хэрэгжиж байгааг батална.
# ----------------------------------------------------------------

def _wave_frame(bars, base=100.0, period=24, amplitude=0.05, start_ms=0):
    """RSI-г хоёр туйл руу тогтмол хүргэдэг долгион."""
    import math
    rows = []
    price = base
    for i in range(bars):
        price = base * (1 + amplitude * math.sin(2 * math.pi * i / period))
        nxt = base * (1 + amplitude * math.sin(2 * math.pi * (i + 1) / period))
        o, c = price, nxt
        rows.append({
            "time": start_ms + i * 3_600_000,
            "open": o, "high": max(o, c) * 1.004, "low": min(o, c) * 0.996,
            "close": c, "volume": 1000.0 + i,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def synthetic_market(monkeypatch):
    monkeypatch.setattr(backtest, "SIGNAL_WINDOW", 220)
    patch_setting(monkeypatch, "MTF_ENABLED", False)
    patch_setting(monkeypatch, "CORRELATION_ENABLED", False)
    patch_setting(monkeypatch, "SELECTION_INTERVAL_MINUTES", 120)
    patch_setting(monkeypatch, "MAX_SELECTIONS", 2)
    patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 14.0)
    patch_setting(monkeypatch, "MAX_HOLD_HOURS", 24)
    patch_setting(monkeypatch, "TARGET_PROFIT", 1e9)     # зорилтод хүрэхгүй
    patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 0.0)
    data = {}
    for n, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT")):
        df = _wave_frame(320, base=100.0 * (n + 1), period=20 + n * 4)
        data[symbol] = {"signal": df, "exec": df, "exec_interval": "1h",
                        "funding": [], "close_by_time": {}}
    return data


class TestBacktestPortfolio:
    def test_the_engine_runs_end_to_end_and_trades(self, synthetic_market):
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert sim is not None
        assert len(sim["trades"]) > 0
        assert sim["cycles"] > 0

    def test_never_more_positions_than_the_slot_limit(self, synthetic_market):
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        events = []
        for trade in sim["trades"]:
            events.append((trade["opened_ms"], 1))
            events.append((trade["closed_ms"], -1))
        events.sort()
        concurrent = peak = 0
        for _, delta in events:
            concurrent += delta
            peak = max(peak, concurrent)

        assert peak <= 2

    def test_entries_use_the_next_bar_open_not_the_signal_bar_close(self, monkeypatch, synthetic_market):
        patch_setting(monkeypatch, "BACKTEST_SLIPPAGE_RATE", 0.0)

        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        for trade in sim["trades"][:10]:
            df = synthetic_market[trade["symbol"]]["signal"]
            row = df[df["time"] == trade["opened_ms"]]
            assert not row.empty
            assert trade["entry"] == pytest.approx(float(row["open"].iloc[0]))

    def test_signals_never_see_a_bar_that_has_not_closed(self, monkeypatch, synthetic_market):
        """Lookahead-ийн шууд шалгуур: шинжилгээнд өгсөн лааны сүүлийн цаг нь
        шийдвэрийн лаанаас ХЭТЭРЧ болохгүй. Хэтэрвэл backtest ирээдүйг хараад
        бодит бус сайхан үр дүн гаргана."""
        seen = []
        real = screening.analyze_frame

        def spy(symbol, df, mtf_signal, funding_rate):
            seen.append((symbol, int(df["time"].iloc[-1])))
            return real(symbol, df, mtf_signal, funding_rate)

        monkeypatch.setattr(screening, "analyze_frame", spy)
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert seen
        # Арилжаа нээгдсэн бар нь signal барын ДАРААХ бар байх ёстой
        opened = {t["opened_ms"] for t in sim["trades"]}
        analysed = {ts for _, ts in seen}
        assert opened
        assert all(ts + 3_600_000 in opened or ts not in opened for ts in analysed)

    def test_costs_are_charged_and_reduce_the_result(self, synthetic_market):
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert sim["total_fees"] > 0
        net = sim["final_balance"] - sim["start_balance"]
        assert net == pytest.approx(sim["total_gross"] - sim["total_fees"] - sim["total_funding"])

    def test_the_report_renders(self, synthetic_market):
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        report = backtest.format_report(sim, synthetic_market)

        assert "ЗАРДЛЫН ЗАДАРГАА" in report
        assert "СТРАТЕГИ ТУС БҮР" in report
        assert "ОНООНЫ БҮЛЭГ" in report

    def test_a_higher_score_threshold_trades_less(self, monkeypatch, synthetic_market):
        busy = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 60.0)
        picky = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert len(picky["trades"]) < len(busy["trades"])

    def test_atr_settings_reach_the_simulated_stops(self, monkeypatch, synthetic_market):
        """risk.exit_levels-ийг үнэхээр дуудаж байгаагийн баталгаа."""
        patch_setting(monkeypatch, "ATR_RISK_SIZING_ENABLED", True)
        patch_setting(monkeypatch, "ATR_SL_MIN_PCT", 4.0)
        patch_setting(monkeypatch, "ATR_SL_MAX_PCT", 4.0)

        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert sim["trades"]
        assert all(t["sl_pct"] == pytest.approx(4.0) for t in sim["trades"])

    def test_funding_is_charged_to_open_longs(self, monkeypatch, synthetic_market):
        for symbol, entry in synthetic_market.items():
            times = list(entry["signal"]["time"])
            entry["funding"] = [(t, 0.0005) for t in times[::8]]

        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        longs = [t for t in sim["trades"] if t["side"] == "BUY"]
        assert longs
        assert sum(t["funding"] for t in longs) > 0


class TestBacktestReportDelivery:
    """Тайланг Telegram руу бүтнээр нь хүргэх.

    send_telegram нь 4096 тэмдэгтээс хэтэрсэн мессежийг таслаад хаядаг тул
    хэсэглэхгүй бол тайлангийн сүүл (жишиг харьцуулалт, тэмдэглэл) алга болно.
    """

    def test_a_short_report_goes_in_one_message(self, telegram_messages):
        assert backtest.send_report_to_telegram("мөр 1\nмөр 2") == 1
        assert len(telegram_messages) == 1

    def test_a_long_report_is_split_and_nothing_is_lost(self, telegram_messages):
        report = "\n".join(f"мөр {i:04d} " + "x" * 60 for i in range(300))

        chunks = backtest.send_report_to_telegram(report)

        assert chunks > 1
        assert len(telegram_messages) == chunks
        assert all(len(m) <= 4096 for m in telegram_messages)
        assert "мөр 0000" in telegram_messages[0]
        assert "мөр 0299" in telegram_messages[-1]

    def test_no_markup_tags_are_sent_since_parse_mode_is_not_set(self, telegram_messages):
        backtest.send_report_to_telegram("тайлан")

        assert "<pre>" not in telegram_messages[0]


class TestBacktestDataSource:
    """Түүхэн өгөгдөл нь production эндпойнтоос ирэх ёстой.

    Бот demo дансан дээр ажилладаг ч demo-гийн лааны түүх богино/бодит бус
    байдаг тул backtest-ыг утгагүй болгоно.
    """

    def test_history_is_fetched_from_the_production_endpoint(self, monkeypatch):
        seen = []
        monkeypatch.setattr(binance_client, "BASE_URL", "https://demo-fapi.binance.com")
        monkeypatch.setattr(market_data, "get_klines_range",
                            lambda *a, **kw: seen.append(binance_client.BASE_URL) or pd.DataFrame())
        monkeypatch.setattr(market_data, "get_funding_history", lambda *a, **kw: [])
        monkeypatch.setattr(binance_client, "current_timestamp_ms", lambda: 1_700_000_000_000)

        backtest.load_history(["BTCUSDT"], days=5, progress=False,
                              data_url="https://fapi.binance.com")

        assert seen == ["https://fapi.binance.com"]

    def test_the_trading_endpoint_is_restored_afterwards(self, monkeypatch):
        monkeypatch.setattr(binance_client, "BASE_URL", "https://demo-fapi.binance.com")
        monkeypatch.setattr(market_data, "get_klines_range", lambda *a, **kw: pd.DataFrame())
        monkeypatch.setattr(market_data, "get_funding_history", lambda *a, **kw: [])
        monkeypatch.setattr(binance_client, "current_timestamp_ms", lambda: 1_700_000_000_000)

        backtest.load_history(["BTCUSDT"], days=5, progress=False,
                              data_url="https://fapi.binance.com")

        assert binance_client.BASE_URL == "https://demo-fapi.binance.com"

    def test_the_endpoint_is_restored_even_when_the_download_fails(self, monkeypatch):
        monkeypatch.setattr(binance_client, "BASE_URL", "https://demo-fapi.binance.com")
        monkeypatch.setattr(binance_client, "current_timestamp_ms", lambda: 1_700_000_000_000)

        def boom(*a, **kw):
            raise ValueError("сүлжээ унтарлаа")

        monkeypatch.setattr(market_data, "get_klines_range", boom)

        with pytest.raises(ValueError):
            backtest.load_history(["BTCUSDT"], days=5, progress=False,
                                  data_url="https://fapi.binance.com")

        assert binance_client.BASE_URL == "https://demo-fapi.binance.com"


class TestBacktestStateIsolation:
    """Симуляц ажиллаж буй ботын state-ийг хөндөж болохгүй.

    run_portfolio_backtest нь state.reset() дуудаж, стратегийн статистикийг
    өөрийнхөөрөө дүүргэдэг. bot.py эхлэхдээ backtest ажиллуулбал энэ нь
    sync_existing_positions-ийн сая барьсан нээлттэй арилжааны бүртгэл болон
    drawdown-ы оргилыг устгана — backtest ажиллаж буй ботоо сүйтгэнэ.
    """

    def test_open_trades_survive_a_simulation(self, synthetic_market):
        bot_state.active_trade_info["BTCUSDT"] = _trade_info(strategy="MACD_MOMENTUM")

        backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert "BTCUSDT" in bot_state.active_trade_info
        assert bot_state.active_trade_info["BTCUSDT"]["strategy"] == "MACD_MOMENTUM"

    def test_the_drawdown_high_water_mark_survives(self, synthetic_market):
        bot_state.session_peak_balance = 8_888.0
        bot_state.session_realized_pnl = 123.45

        backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert bot_state.session_peak_balance == pytest.approx(8_888.0)
        assert bot_state.session_realized_pnl == pytest.approx(123.45)

    def test_live_strategy_stats_are_not_polluted_by_simulated_trades(self, synthetic_market):
        bot_state.strategy_stats["RSI_STRATEGY"]["trades"] = 4
        bot_state.strategy_stats["RSI_STRATEGY"]["total_pnl"] = 55.0

        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert sim["trades"]                       # симуляц үнэхээр арилжаа хийсэн
        assert bot_state.strategy_stats["RSI_STRATEGY"]["trades"] == 4
        assert bot_state.strategy_stats["RSI_STRATEGY"]["total_pnl"] == pytest.approx(55.0)

    def test_a_safety_lock_is_not_left_behind_by_the_simulation(self, synthetic_market):
        backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert bot_state.safety_lock is False
        assert bot_state.drawdown_halt is False


class TestExecBarsConsistency:
    """Нарийн лаа нь 1h лаатай ижил үнийн цуваа мөн эсэх.

    Хоёр давтамжийн өгөгдөл зөрвөл (symbol-ийн 15м түүх хожуу эхэлсэн, эх
    сурвалж эвдэрсэн) entry нь 1h барын нээлтээр тогтоод гарц нь огт өөр үнэ
    дээр шийдэгдэж, арилжаа бүр цоорхойгоор stop цохисон мэт харагдана —
    чимээгүй боловч гамшигтай. Ийм үед 1h руу буцах нь бүдүүлэг ч ҮНЭН.
    """

    def _hourly(self, n=60, base=100.0, start=0):
        return pd.DataFrame({
            "time": [start + i * 3_600_000 for i in range(n)],
            "open": [base + i for i in range(n)], "high": [base + i + 1 for i in range(n)],
            "low": [base + i - 1 for i in range(n)], "close": [base + i + 0.5 for i in range(n)],
            "volume": [1.0] * n,
        })

    def _quarters(self, hourly, shift=0.0):
        rows = []
        for row in hourly.to_dict("records"):
            for q in range(4):
                rows.append({**row, "time": row["time"] + q * 900_000,
                             "open": row["open"] + shift + q * 0.01})
        return pd.DataFrame(rows)

    def test_matching_series_are_accepted(self):
        hourly = self._hourly()

        assert backtest.exec_bars_agree(hourly, self._quarters(hourly)) is True

    def test_a_diverging_series_is_rejected(self):
        hourly = self._hourly()

        assert backtest.exec_bars_agree(hourly, self._quarters(hourly, shift=40.0)) is False

    def test_missing_fine_bars_are_rejected(self):
        assert backtest.exec_bars_agree(self._hourly(), pd.DataFrame()) is False

    def test_barely_overlapping_history_is_rejected(self):
        hourly = self._hourly(n=60)
        # Нарийн лаа нь зөвхөн сүүлийн 5 цагийг хамарна
        short = self._quarters(hourly.iloc[-5:])

        assert backtest.exec_bars_agree(hourly, short) is False

    def test_a_few_outliers_do_not_reject_a_good_series(self):
        hourly = self._hourly()
        fine = self._quarters(hourly)
        fine.loc[0, "open"] = 9999.0        # ганц гажиг

        assert backtest.exec_bars_agree(hourly, fine) is True

    def test_load_history_falls_back_to_hourly_bars_on_a_mismatch(self, monkeypatch):
        hourly = self._hourly(n=700)
        monkeypatch.setattr(binance_client, "current_timestamp_ms", lambda: 1_700_000_000_000)
        monkeypatch.setattr(market_data, "get_funding_history", lambda *a, **kw: [])
        monkeypatch.setattr(backtest, "SIGNAL_WINDOW", 600)
        monkeypatch.setattr(market_data, "get_klines_range",
                            lambda symbol, interval, *a, **kw:
                                hourly if interval == "1h" else self._quarters(hourly, shift=40.0))

        data = backtest.load_history(["BTCUSDT"], days=5, progress=False, data_url=None)

        assert data["BTCUSDT"]["exec_interval"] == "1h"
        assert len(data["BTCUSDT"]["exec"]) == len(hourly)


class TestBacktestHaltedPeriod:
    """Эрт зогссон симуляцыг бүх өгөгдлийн хугацаагаар хэмжиж болохгүй.

    Drawdown halt нь 91 хоногийн 30 дахь өдөр дээр буудаг бол "сард X%" ба
    BTC-тэй харьцуулалт хоёулаа арилжаа хийгээгүй 61 хоногийг тоолж, ботыг
    байснаас нь дөрөв дахин дээр харагдуулна.
    """

    def _halting_market(self, monkeypatch, synthetic_market):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 0.5)   # шууд буудна
        return synthetic_market

    def test_the_measured_period_ends_where_trading_stopped(self, monkeypatch, synthetic_market):
        self._halting_market(monkeypatch, synthetic_market)

        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert sim["halted"] == "DRAWDOWN_HALT"
        assert sim["to_ms"] < sim["data_to_ms"]

    def test_a_completed_run_measures_the_whole_dataset(self, monkeypatch, synthetic_market):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 0.0)

        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        assert sim["halted"] is None
        assert sim["to_ms"] == pytest.approx(sim["data_to_ms"], abs=3_600_000)

    def test_the_report_says_the_run_was_cut_short(self, monkeypatch, synthetic_market):
        self._halting_market(monkeypatch, synthetic_market)
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)

        report = backtest.format_report(sim, synthetic_market)

        assert "ЗОГССОН" in report
        assert "дахь өдөр дээр ЗОГССОН" in report

    def test_the_benchmark_covers_the_same_window_as_the_bot(self, monkeypatch, synthetic_market):
        self._halting_market(monkeypatch, synthetic_market)
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)
        captured = {}
        real = backtest._buy_and_hold

        def spy(data, from_ms, to_ms, symbol="BTCUSDT"):
            captured["to_ms"] = to_ms
            return real(data, from_ms, to_ms, symbol)

        monkeypatch.setattr(backtest, "_buy_and_hold", spy)
        backtest.format_report(sim, synthetic_market)

        assert captured["to_ms"] == sim["to_ms"]        # өгөгдлийн төгсгөл БИШ


class TestBacktestStrategyDisable:
    """Тодорхой стратегийг унтрааж, үлдсэн нь дангаараа ямар үр дүн өгөхийг харах."""

    def test_a_disabled_strategy_never_trades(self, synthetic_market):
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                              disabled=["RSI_STRATEGY"])

        assert all(t["strategy"] != "RSI_STRATEGY" for t in sim["trades"])

    def test_disabling_changes_which_trades_are_taken(self, synthetic_market):
        full = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)
        cut = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                              disabled=["RSI_STRATEGY"])

        assert any(t["strategy"] == "RSI_STRATEGY" for t in full["trades"])
        assert len(cut["trades"]) < len(full["trades"])

    def test_a_cooldown_cannot_reactivate_a_disabled_strategy(self, synthetic_market):
        # update_strategy_cooldowns нь paused_cycles > 0 үед л дахин асаадаг.
        # Унтраахдаа paused_cycles-ыг хөндөхгүй тул мөчлөгийн туршид унтарсан хэвээр.
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                              disabled=["RSI_STRATEGY", "MACD_MOMENTUM"])

        assert all(t["strategy"] not in ("RSI_STRATEGY", "MACD_MOMENTUM") for t in sim["trades"])

    def test_the_report_names_what_was_disabled(self, synthetic_market):
        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                              disabled=["RSI_STRATEGY"])

        assert "Унтраасан стратеги: RSI_STRATEGY" in backtest.format_report(sim, synthetic_market)

    def test_live_strategy_stats_are_still_untouched(self, synthetic_market):
        backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                        disabled=["RSI_STRATEGY"])

        assert bot_state.strategy_stats["RSI_STRATEGY"]["active"] is True


class TestBacktestCli:
    """CLI-ийн аргументууд симуляц хүртэл үнэхээр хүрч байгаа эсэх.

    Хөдөлгүүр зөв ажиллаж байхад CLI давхарга дээр тасарвал хэрэглэгч
    тохиргоогоо өөрчиллөө гэж бодоод, үнэндээ хуучин үр дүнгээ дахин авна.
    """

    @pytest.fixture
    def cli(self, monkeypatch):
        seen = {}

        def fake_run(**kwargs):
            seen.update(kwargs)
            return {"trades": []}, "тайлан"

        monkeypatch.setattr(backtest, "run", fake_run)
        monkeypatch.setattr(backtest, "setup_logging", lambda *a, **kw: None)
        monkeypatch.setattr(binance_client, "sync_server_time", lambda: None)
        return seen

    def test_disable_reaches_the_simulation(self, cli):
        backtest.main(["--days", "90", "--disable", "MACD_MOMENTUM,BREAKOUT"])

        assert cli["disabled"] == ["MACD_MOMENTUM", "BREAKOUT"]

    def test_disable_is_case_insensitive_and_trims_spaces(self, cli):
        backtest.main(["--disable", " macd_momentum , breakout "])

        assert cli["disabled"] == ["MACD_MOMENTUM", "BREAKOUT"]

    def test_no_disable_flag_means_every_strategy_runs(self, cli):
        backtest.main(["--days", "30"])

        assert cli["disabled"] is None

    def test_an_unknown_strategy_name_is_refused(self, cli):
        # Алдаатай нэрийг чимээгүй алгасвал хэрэглэгч унтраасан гэж бодоод
        # үнэндээ бүрэн ажиллагааны үр дүн авна
        with pytest.raises(SystemExit):
            backtest.main(["--disable", "MACD_MOMENTM"])

    def test_days_and_balance_reach_the_simulation(self, cli):
        backtest.main(["--days", "45", "--balance", "6356"])

        assert cli["days"] == 45
        assert cli["start_balance"] == pytest.approx(6356.0)


class TestBacktestNoHalt:
    """Drawdown breaker-ыг унтраан бүтэн хугацааг хэмжих.

    Одоогийн тохиргоогоор бот эхний долоо хоногт 15%-ийн хязгаартаа хүрч
    зогсдог. Тэр үед `--days 90` ажиллуулсан ч зөвхөн тэр долоо хоногийг л
    хэмждэг — үлдсэн 85 хоног дэмий татагдана. Урт хугацааны зан төлөвийг
    харахын тулд breaker-ыг унтраах хэрэгтэй.
    """

    @pytest.fixture
    def tight_limit(self, monkeypatch, synthetic_market):
        # synthetic_market нь хязгаарыг 0 болгодог тул түүнээс ХОЙШ тавина —
        # эс тэгвээс fixture-ийн дараалал энэ тохиргоог дарж, breaker огт
        # буудахгүй атлаа тестүүд "буудсан" гэж шалгах болно.
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 0.5)
        return synthetic_market

    def test_with_the_breaker_on_the_run_stops_early(self, tight_limit):
        sim = backtest.run_portfolio_backtest(tight_limit, 10_000.0, progress=False)

        assert sim["halted"] == "DRAWDOWN_HALT"
        assert sim["to_ms"] < sim["data_to_ms"]

    def test_with_the_breaker_off_the_whole_period_is_measured(self, tight_limit):
        sim = backtest.run_portfolio_backtest(tight_limit, 10_000.0, progress=False,
                                              halt_on_drawdown=False)

        assert sim["halted"] is None
        assert sim["to_ms"] == pytest.approx(sim["data_to_ms"], abs=3_600_000)

    def test_disabling_the_breaker_yields_more_trades(self, tight_limit):
        stopped = backtest.run_portfolio_backtest(tight_limit, 10_000.0, progress=False)
        full = backtest.run_portfolio_backtest(tight_limit, 10_000.0, progress=False,
                                               halt_on_drawdown=False)

        assert len(full["trades"]) > len(stopped["trades"])

    def test_the_report_says_where_the_real_bot_would_have_stopped(self, tight_limit):
        sim = backtest.run_portfolio_backtest(tight_limit, 10_000.0, progress=False,
                                              halt_on_drawdown=False)

        report = backtest.format_report(sim, tight_limit)

        assert "Drawdown breaker УНТРААЛТТАЙ" in report
        assert sim["breach_ms"] is not None
        assert "дахь өдөр) зогсох байсан" in report

    def test_the_live_drawdown_limit_is_restored_afterwards(self, tight_limit):
        backtest.run_portfolio_backtest(tight_limit, 10_000.0, progress=False,
                                        halt_on_drawdown=False)

        assert risk.MAX_SESSION_DRAWDOWN_PCT == pytest.approx(0.5)

    def test_a_run_that_never_breaches_says_so(self, monkeypatch, synthetic_market):
        patch_setting(monkeypatch, "MAX_SESSION_DRAWDOWN_PCT", 99.0)

        sim = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                              halt_on_drawdown=False)

        assert sim["breach_ms"] is None
        assert "Хязгаарт хүрээгүй" in backtest.format_report(sim, synthetic_market)

    def test_breach_is_measured_on_realized_balance_like_the_live_breaker(self):
        # Амьд breaker нь walletBalance (realized) хардаг, mark-to-market биш.
        # (ts, mark_to_market, realized)
        curve = [(0, 1000.0, 1000.0), (1, 700.0, 990.0), (2, 900.0, 800.0)]

        assert backtest._drawdown_breach_ms(curve, 15.0) == 2      # 700 биш, 800 дээр
        assert backtest._max_drawdown(curve) == pytest.approx(30.0)   # mark-to-market


class TestBacktestNoHaltCli:
    @pytest.fixture
    def cli(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(backtest, "run", lambda **kw: (seen.update(kw), ({"trades": []}, "тайлан"))[1])
        monkeypatch.setattr(backtest, "setup_logging", lambda *a, **kw: None)
        monkeypatch.setattr(binance_client, "sync_server_time", lambda: None)
        return seen

    def test_no_halt_flag_turns_the_breaker_off(self, cli):
        backtest.main(["--days", "90", "--no-halt"])

        assert cli["halt_on_drawdown"] is False

    def test_without_the_flag_the_breaker_stays_on(self, cli):
        backtest.main(["--days", "90"])

        assert cli["halt_on_drawdown"] is True


class TestBacktestRunPlumbing:
    """CLI → run() → run_portfolio_backtest гинжин холбоос.

    Хоёр захыг нь тестлээд дундах давхаргыг орхивол тохиргоо чимээгүй
    алдагдаж, тайлан нь өөрчлөлт хийгээгүй хуучин үр дүнг зөв мэт харуулна.
    """

    @pytest.fixture
    def plumbing(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(backtest, "load_history", lambda *a, **kw: {"BTCUSDT": {}})
        monkeypatch.setattr(backtest, "format_report", lambda sim, data=None: "тайлан")
        monkeypatch.setattr(backtest, "run_portfolio_backtest",
                            lambda data, balance, **kw: seen.update(kw) or {"trades": []})
        return seen

    def test_the_drawdown_flag_survives_the_middle_layer(self, plumbing):
        backtest.run(days=30, start_balance=1000.0, halt_on_drawdown=False, progress=False)

        assert plumbing["halt_on_drawdown"] is False

    def test_the_drawdown_flag_defaults_to_on(self, plumbing):
        backtest.run(days=30, start_balance=1000.0, progress=False)

        assert plumbing["halt_on_drawdown"] is True

    def test_the_disabled_list_survives_the_middle_layer(self, plumbing):
        backtest.run(days=30, start_balance=1000.0, disabled=["BREAKOUT"], progress=False)

        assert plumbing["disabled"] == ["BREAKOUT"]

    def test_missing_data_is_reported_rather_than_crashing(self, monkeypatch):
        monkeypatch.setattr(backtest, "load_history", lambda *a, **kw: {})

        sim, report = backtest.run(days=30, start_balance=1000.0, progress=False)

        assert sim is None
        assert "Өгөгдөл татагдсангүй" in report


class TestBacktestSweep:
    """Хэд хэдэн тохиргоог нэг шинжилгээний дамжлагаар харьцуулах.

    Хамгийн чухал шалгуур: кэш ашигласан үр дүн нь кэшгүй ажиллуулсантай
    ЯГ ижил байх. Эс тэгвээс хурдны төлөө үнэнээ алдана.
    """

    def test_a_cached_run_matches_an_uncached_one(self, synthetic_market):
        plain = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)
        cache = backtest.build_analysis_cache(synthetic_market, progress=False)
        cached = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                                 analysis_cache=cache)

        assert len(cached["trades"]) == len(plain["trades"])
        assert cached["final_balance"] == pytest.approx(plain["final_balance"])
        assert [t["symbol"] for t in cached["trades"]] == [t["symbol"] for t in plain["trades"]]

    def test_the_cache_survives_a_stricter_threshold(self, monkeypatch, synthetic_market):
        # Кэш нь MIN_SIGNAL_SCORE=0-оор баригддаг; жинхэнэ босго нь
        # pick_candidates дотор тавигдана. Хоёр зам ижил үр дүн өгөх ёстой.
        cache = backtest.build_analysis_cache(synthetic_market, progress=False)
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 22.0)

        plain = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)
        cached = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                                 analysis_cache=cache)

        assert len(cached["trades"]) == len(plain["trades"])
        assert cached["final_balance"] == pytest.approx(plain["final_balance"])

    def test_the_cache_survives_a_disabled_strategy(self, synthetic_market):
        cache = backtest.build_analysis_cache(synthetic_market, progress=False)

        plain = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                                disabled=["RSI_STRATEGY"])
        cached = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                                 disabled=["RSI_STRATEGY"], analysis_cache=cache)

        assert len(cached["trades"]) == len(plain["trades"])
        assert cached["final_balance"] == pytest.approx(plain["final_balance"])

    def test_building_the_cache_leaves_the_live_threshold_alone(self, monkeypatch, synthetic_market):
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 17.0)

        backtest.build_analysis_cache(synthetic_market, progress=False)

        assert screening.MIN_SIGNAL_SCORE == pytest.approx(17.0)

    def test_each_configuration_produces_its_own_result(self, synthetic_market):
        configs = [
            {"name": "жишиг"},
            {"name": "өндөр босго", "min_score": 60.0},
        ]

        results = backtest.run_sweep(synthetic_market, 10_000.0, configs=configs, progress=False)

        assert [r["name"] for r in results] == ["жишиг", "өндөр босго"]
        assert len(results[1]["trades"]) < len(results[0]["trades"])

    def test_a_regime_filter_only_keeps_that_regime(self, synthetic_market):
        results = backtest.run_sweep(
            synthetic_market, 10_000.0, progress=False,
            configs=[{"name": "зөвхөн TRANSITION", "regimes": ["TRANSITION"]}])

        assert all(t["regime"] == "TRANSITION" for t in results[0]["trades"])

    def test_overrides_are_restored_after_the_sweep(self, monkeypatch, synthetic_market):
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 14.0)
        patch_setting(monkeypatch, "PARTIAL_TP_ENABLED", True)

        backtest.run_sweep(synthetic_market, 10_000.0, progress=False,
                           configs=[{"name": "x", "min_score": 99.0, "partial_tp": False,
                                     "regimes": ["STRONG_TREND"]}])

        assert screening.MIN_SIGNAL_SCORE == pytest.approx(14.0)
        assert screening.ALLOWED_REGIMES == []
        assert backtest.PARTIAL_TP_ENABLED is True

    def test_the_comparison_report_ranks_the_configurations(self, synthetic_market):
        results = backtest.run_sweep(
            synthetic_market, 10_000.0, progress=False,
            configs=[{"name": "жишиг"}, {"name": "өндөр босго", "min_score": 60.0}])

        report = backtest.format_sweep_report(results, synthetic_market)

        assert "ХУВИЛБАРУУДЫН ХАРЬЦУУЛАЛТ" in report
        assert "🥇 Хамгийн сайн" in report
        assert "ЖИНХЭНЭ БОТ ХЭЗЭЭ ЗОГСОХ БАЙСАН" in report
        assert "ӨӨР хугацаан дээр заавал" in report      # overfitting сануулга


class TestBacktestSweepCacheFidelity:
    """Кэш нь ямар ч тохиргоонд тохирох ёстой.

    analyze_frame нь босгоос доош онооны signal-ыг HOLD болгож, идэвхгүй
    стратегиудыг алгасдаг. Хэрэв кэшийг тухайн үеийн амьд тохиргоогоор
    барьвал, түүнээс СУЛ тохиргоо туршихад арилжаанууд чимээгүй алга болж,
    "энэ хувилбар муу" гэсэн худал дүгнэлт гарна.
    """

    def test_a_cache_built_under_a_strict_threshold_still_serves_a_loose_one(
        self, monkeypatch, synthetic_market
    ):
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 40.0)     # хатуу үед барина
        cache = backtest.build_analysis_cache(synthetic_market, progress=False)
        patch_setting(monkeypatch, "MIN_SIGNAL_SCORE", 5.0)      # сул үед ашиглана

        plain = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False)
        cached = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                                 analysis_cache=cache)

        assert len(plain["trades"]) > 0
        assert len(cached["trades"]) == len(plain["trades"])
        assert cached["final_balance"] == pytest.approx(plain["final_balance"])

    def test_a_strategy_paused_live_is_still_in_the_cache(self, synthetic_market):
        # Амьд бот дараалсан алдагдлын дараа стратегийг унтраадаг. Тэр мөчид
        # backtest ажиллуулбал кэш түүнгүйгээр баригдаж, дараагийн хувилбарууд
        # тэр стратегийг хэзээ ч харахгүй.
        bot_state.strategy_stats["RSI_STRATEGY"]["active"] = False
        cache = backtest.build_analysis_cache(synthetic_market, progress=False)

        cached = backtest.run_portfolio_backtest(synthetic_market, 10_000.0, progress=False,
                                                 analysis_cache=cache)

        assert any(t["strategy"] == "RSI_STRATEGY" for t in cached["trades"])

    def test_building_the_cache_leaves_paused_strategies_paused(self, synthetic_market):
        bot_state.strategy_stats["RSI_STRATEGY"]["active"] = False

        backtest.build_analysis_cache(synthetic_market, progress=False)

        assert bot_state.strategy_stats["RSI_STRATEGY"]["active"] is False


class TestBacktestSweepPartialTp:
    """partial_tp хувилбар үнэхээр зан төлөвийг өөрчилж байгаа эсэх."""

    def test_turning_partial_tp_off_removes_the_partial_fills(self, synthetic_market):
        results = backtest.run_sweep(
            synthetic_market, 10_000.0, progress=False,
            configs=[{"name": "on", "partial_tp": True},
                     {"name": "off", "partial_tp": False}])
        on, off = results

        assert any(t["partial_hit"] for t in on["trades"])
        assert not any(t["partial_hit"] for t in off["trades"])

    def test_turning_partial_tp_off_removes_breakeven_exits(self, synthetic_market):
        results = backtest.run_sweep(
            synthetic_market, 10_000.0, progress=False,
            configs=[{"name": "on", "partial_tp": True},
                     {"name": "off", "partial_tp": False}])
        on, off = results

        assert any(t["exit_reason"] == "BREAKEVEN" for t in on["trades"])
        assert not any(t["exit_reason"] == "BREAKEVEN" for t in off["trades"])


class TestBacktestOffsetWindow:
    """Ижил тохиргоог өөр хугацаанд шалгах.

    Бүх ажиллагаа сүүлийн үеийг хэмжвэл шилдэг хувилбар үнэхээр ажилладаг уу,
    эсвэл тэр хугацаанд таарсан уу гэдгийг ялгах боломжгүй — 6 хувилбараас
    нэгийг сонгох нь өөрөө overfitting.
    """

    NOW = 1_800_000_000_000

    @pytest.fixture
    def window_spy(self, monkeypatch):
        seen = {}

        def fake_range(symbol, interval, start_ms, end_ms, **kw):
            seen.setdefault(interval, []).append((start_ms, end_ms))
            # Хангалттай урт байх ёстой — эс тэвэл symbol алгасагдаж,
            # 15m ба funding огт татагдахгүй тул тест юу ч шалгахгүй
            step = market_data.INTERVAL_MS[interval]
            n = 700
            return pd.DataFrame({
                "time": [start_ms + i * step for i in range(n)],
                "open": [100.0] * n, "high": [101.0] * n,
                "low": [99.0] * n, "close": [100.0] * n, "volume": [1.0] * n,
            })

        monkeypatch.setattr(binance_client, "current_timestamp_ms", lambda: self.NOW)
        monkeypatch.setattr(market_data, "get_klines_range", fake_range)
        monkeypatch.setattr(market_data, "get_funding_history",
                            lambda symbol, start_ms, end_ms: seen.setdefault("funding", []).append((start_ms, end_ms)) or [])
        return seen

    def test_no_offset_ends_at_now(self, window_spy):
        backtest.load_history(["BTCUSDT"], days=30, progress=False)

        _, end_ms = window_spy["1h"][0]
        assert end_ms == self.NOW

    def test_an_offset_moves_the_window_back(self, window_spy):
        backtest.load_history(["BTCUSDT"], days=30, progress=False, offset_days=91)

        _, end_ms = window_spy["1h"][0]
        assert end_ms == self.NOW - 91 * 24 * 3_600_000

    def test_the_start_moves_with_the_end(self, window_spy):
        backtest.load_history(["BTCUSDT"], days=30, progress=False, offset_days=91)

        start_ms, end_ms = window_spy["1h"][0]
        span_hours = (end_ms - start_ms) / 3_600_000
        assert span_hours == pytest.approx(30 * 24 + backtest.SIGNAL_WINDOW + 24)

    def test_two_offsets_do_not_overlap(self, window_spy):
        backtest.load_history(["BTCUSDT"], days=91, progress=False, offset_days=0)
        recent_start, recent_end = window_spy["1h"][0]
        window_spy["1h"].clear()

        backtest.load_history(["BTCUSDT"], days=91, progress=False, offset_days=91)
        older_start, older_end = window_spy["1h"][0]

        # Warmup давхцаж болно, харин ТУРШИХ хэсэг нь давхцах ёсгүй
        assert older_end <= recent_start + (backtest.SIGNAL_WINDOW + 24) * 3_600_000

    def test_every_data_series_uses_the_same_window(self, window_spy):
        backtest.load_history(["BTCUSDT"], days=30, progress=False, offset_days=45)

        expected_end = self.NOW - 45 * 24 * 3_600_000
        assert window_spy["1h"][0][1] == expected_end
        assert window_spy["15m"][0][1] == expected_end
        assert window_spy["funding"][0][1] == expected_end


class TestBacktestOffsetCli:
    @pytest.fixture
    def cli(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(backtest, "run", lambda **kw: (seen.update(kw), ({"trades": []}, "т"))[1])
        monkeypatch.setattr(backtest, "run_sweep_cli", lambda **kw: (seen.update(kw), ([{}], "т"))[1])
        monkeypatch.setattr(backtest, "setup_logging", lambda *a, **kw: None)
        monkeypatch.setattr(binance_client, "sync_server_time", lambda: None)
        return seen

    def test_offset_reaches_a_single_run(self, cli):
        backtest.main(["--days", "91", "--offset-days", "91"])

        assert cli["offset_days"] == 91

    def test_offset_reaches_a_sweep(self, cli):
        backtest.main(["--days", "91", "--offset-days", "182", "--sweep"])

        assert cli["offset_days"] == 182

    def test_the_default_offset_is_zero(self, cli):
        backtest.main(["--days", "30"])

        assert cli["offset_days"] == 0

    def test_a_negative_offset_is_refused(self, cli):
        with pytest.raises(SystemExit):
            backtest.main(["--offset-days", "-5"])


class TestSweepCliPlumbing:
    """CLI → run_sweep_cli → load_history гинжин холбоос.

    Хоёр захыг нь тестлээд дундахыг орхивол тохиргоо чимээгүй алдагдана —
    sweep нь өөр хугацаа хэмжиж байгаа мэт харагдаад үнэндээ ижил сүүлийн
    үеийг дахин хэмжинэ, out-of-sample шалгалт утгагүй болно.
    """

    @pytest.fixture
    def plumbing(self, monkeypatch):
        seen = {}

        def fake_load(symbols, days, **kw):
            seen.update(kw)
            seen["days"] = days
            return {"BTCUSDT": {}}

        def fake_sweep(data, balance, **kw):
            seen["sweep_kwargs"] = kw
            return [{"trades": []}]

        monkeypatch.setattr(backtest, "load_history", fake_load)
        monkeypatch.setattr(backtest, "run_sweep", fake_sweep)
        monkeypatch.setattr(backtest, "format_sweep_report", lambda results, data=None: "тайлан")
        return seen

    def test_the_offset_reaches_the_data_loader(self, plumbing):
        backtest.run_sweep_cli(days=91, start_balance=1000.0, offset_days=182, progress=False)

        assert plumbing["offset_days"] == 182

    def test_the_default_offset_is_zero(self, plumbing):
        backtest.run_sweep_cli(days=91, start_balance=1000.0, progress=False)

        assert plumbing["offset_days"] == 0

    def test_the_disabled_list_reaches_the_sweep(self, plumbing):
        backtest.run_sweep_cli(days=91, start_balance=1000.0, progress=False,
                               disabled=["MACD_MOMENTUM", "BREAKOUT"])

        assert plumbing["sweep_kwargs"]["disabled"] == ["MACD_MOMENTUM", "BREAKOUT"]

    def test_the_breaker_stays_off_for_a_sweep(self, plumbing):
        # Хувилбарууд өөр өдөр зогсвол өөр хугацаа хэмжинэ — харьцуулалт унана
        backtest.run_sweep_cli(days=91, start_balance=1000.0, progress=False)

        assert plumbing["sweep_kwargs"]["halt_on_drawdown"] is False

    def test_the_data_url_reaches_the_loader(self, plumbing):
        backtest.run_sweep_cli(days=30, start_balance=1000.0, progress=False,
                               data_url="https://fapi.binance.com")

        assert plumbing["data_url"] == "https://fapi.binance.com"

    def test_missing_data_is_reported_rather_than_crashing(self, monkeypatch):
        monkeypatch.setattr(backtest, "load_history", lambda *a, **kw: {})

        results, report = backtest.run_sweep_cli(days=30, start_balance=1000.0, progress=False)

        assert results is None
        assert "Өгөгдөл татагдсангүй" in report


class TestSweepWinnerExcludesBreaches:
    """Хязгаарт хүрэх хувилбарыг "хамгийн сайн" гэж сонгож болохгүй.

    Тэдний өгөөж нь breaker унтраалттай байсны үр дүн — жинхэнэ бот тэр өдөр
    зогсоод цаашид арилжаа хийхгүй тул тэр тоонд хэзээ ч хүрэхгүй. Live
    тайлан дээр яг ийм хувилбар "🥇 хамгийн сайн" болж гарсан.
    """

    def _sim(self, name, final, breach_ms=None, trades=3):
        curve = [(0, 10_000.0, 10_000.0), (3_600_000, final, final)]
        return {
            "name": name, "start_balance": 10_000.0, "final_balance": final,
            "trades": [{"net": 1.0} for _ in range(trades)],
            "equity_curve": curve, "breach_ms": breach_ms,
            "from_ms": 0, "to_ms": 3_600_000, "data_to_ms": 3_600_000,
            "symbols": ["BTCUSDT"], "disabled": [], "total_gross": 1.0,
            "total_fees": 0.0, "total_funding": 0.0, "halted": None,
        }

    def test_a_breaching_configuration_cannot_win(self):
        results = [self._sim("зогссон", 12_000.0, breach_ms=2_000_000),
                   self._sim("тогтвортой", 10_500.0)]

        report = backtest.format_sweep_report(results)

        assert "🥇 Хамгийн сайн (хүрэх боломжтой): тогтвортой" in report
        assert "зогссон" not in report.split("🥇")[1].split("Хязгаарт хүрсэн")[0]

    def test_the_excluded_configurations_are_named(self):
        results = [self._sim("зогссон", 12_000.0, breach_ms=2_000_000),
                   self._sim("тогтвортой", 10_500.0)]

        report = backtest.format_sweep_report(results)

        assert "Хязгаарт хүрсэн тул тооцоогүй: зогссон" in report

    def test_the_table_marks_which_ones_would_stop(self):
        results = [self._sim("зогссон", 12_000.0, breach_ms=2_000_000),
                   self._sim("тогтвортой", 10_500.0)]

        report = backtest.format_sweep_report(results)
        row = next(line for line in report.split("\n") if line.strip().startswith("зогссон"))

        assert "❌" in row
        assert "❌" not in next(line for line in report.split("\n")
                               if line.strip().startswith("тогтвортой"))

    def test_when_every_configuration_breaches_none_is_crowned(self):
        results = [self._sim("a", 12_000.0, breach_ms=2_000_000),
                   self._sim("b", 11_000.0, breach_ms=3_000_000)]

        report = backtest.format_sweep_report(results)

        assert "🥇" not in report
        assert "БҮХ хувилбар" in report

    def test_the_best_of_several_clean_runs_still_wins(self):
        results = [self._sim("сул", 10_200.0),
                   self._sim("хүчтэй", 10_900.0),
                   self._sim("зогссон", 20_000.0, breach_ms=2_000_000)]

        report = backtest.format_sweep_report(results)

        assert "🥇 Хамгийн сайн (хүрэх боломжтой): хүчтэй" in report


class TestWalkForward:
    """Олон давхцаагүй цонхон дээр давтах.

    Нэг цонхны үр дүн чимээнд живсэн байдаг (167 арилжаа × $52 хазайлт →
    нийлбэрийн хазайлт нь дансны ~10%). Тогтвортой байдлыг зөвхөн давталтаар
    хэмжинэ.
    """

    @pytest.fixture
    def windows_spy(self, monkeypatch, synthetic_market):
        offsets = []

        def fake_load(symbols, days, **kw):
            offsets.append(kw.get("offset_days"))
            return synthetic_market

        monkeypatch.setattr(backtest, "load_history", fake_load)
        return offsets

    def test_windows_do_not_overlap(self, windows_spy):
        backtest.run_walk_forward(days=30, windows=4, start_balance=10_000.0,
                                  configs=[{"name": "x"}], progress=False)

        assert windows_spy == [0, 30, 60, 90]

    def test_an_offset_shifts_every_window(self, windows_spy):
        backtest.run_walk_forward(days=30, windows=3, start_balance=10_000.0,
                                  offset_days=91, configs=[{"name": "x"}], progress=False)

        assert windows_spy == [91, 121, 151]

    def test_a_window_without_data_is_skipped_not_fatal(self, monkeypatch, synthetic_market):
        calls = {"n": 0}

        def flaky(symbols, days, **kw):
            calls["n"] += 1
            return {} if calls["n"] == 2 else synthetic_market

        monkeypatch.setattr(backtest, "load_history", flaky)

        collected = backtest.run_walk_forward(days=30, windows=3, start_balance=10_000.0,
                                              configs=[{"name": "x"}], progress=False)

        assert len(collected) == 2

    def test_each_window_records_every_configuration(self, windows_spy):
        collected = backtest.run_walk_forward(
            days=30, windows=2, start_balance=10_000.0, progress=False,
            configs=[{"name": "a"}, {"name": "b", "min_score": 60.0}])

        assert len(collected) == 2
        for window in collected:
            assert [cfg["name"] for cfg in window["configs"]] == ["a", "b"]

    def test_only_summaries_are_kept_not_the_price_data(self, windows_spy):
        # Цонх бүрийн өгөгдөл ~200 MB — хуримтлуулбал санах ой дүүрнэ
        collected = backtest.run_walk_forward(days=30, windows=2, start_balance=10_000.0,
                                              configs=[{"name": "x"}], progress=False)

        assert "data" not in collected[0]
        # trades нь ТООЛОЛТ, арилжааны жагсаалт биш — талбарын жагсаалт үүнийг барина
        assert set(collected[0]["configs"][0]) == {
            "name", "return_pct", "trades", "win_rate", "breached", "max_dd"}


class TestWalkForwardReport:
    def _window(self, returns, breaches=None, from_ms=0, benchmark=5.0):
        breaches = breaches or {}
        return {
            "offset": 0, "from_ms": from_ms, "to_ms": from_ms + 30 * 24 * 3_600_000,
            "symbols": 15, "benchmark": benchmark,
            "configs": [{"name": name, "return_pct": value, "trades": 20,
                         "win_rate": 60.0, "breached": breaches.get(name, False),
                         "max_dd": 5.0}
                        for name, value in returns.items()],
        }

    def test_a_configuration_positive_everywhere_is_flagged(self):
        collected = [self._window({"тогтвортой": 5.0, "хэлбэлзэх": 9.0}),
                     self._window({"тогтвортой": 2.0, "хэлбэлзэх": -4.0})]

        report = backtest.format_walk_forward_report(collected, 30, 10_000.0)

        assert "✅ БҮХ цонхонд эерэг" in report
        assert "тогтвортой" in report.split("✅")[1]
        assert "хэлбэлзэх" not in report.split("✅")[1]

    def test_a_breaching_window_disqualifies_a_configuration(self):
        collected = [self._window({"a": 5.0}, breaches={"a": True}),
                     self._window({"a": 3.0})]

        report = backtest.format_walk_forward_report(collected, 30, 10_000.0)

        assert "Бүх цонхонд эерэг байсан хувилбар АЛГА" in report

    def test_when_nothing_survives_it_says_so(self):
        collected = [self._window({"a": 5.0, "b": -1.0}),
                     self._window({"a": -2.0, "b": 3.0})]

        report = backtest.format_walk_forward_report(collected, 30, 10_000.0)

        assert "АЛГА" in report
        assert "✅" not in report

    def test_the_per_window_grid_marks_breaches(self):
        collected = [self._window({"a": 5.0}, breaches={"a": True}),
                     self._window({"a": 3.0})]

        report = backtest.format_walk_forward_report(collected, 30, 10_000.0)
        grid = report.split("ЦОНХ ТУС БҮРИЙН ӨГӨӨЖ")[1]
        row = next(line for line in grid.split("\n") if line.strip().startswith("a "))

        assert "❌" in row
        assert row.count("❌") == 1        # зөвхөн эхний цонхонд

    def test_the_noise_warning_scales_with_the_window_count(self):
        two = backtest.format_walk_forward_report(
            [self._window({"a": 1.0}), self._window({"a": 1.0})], 30, 10_000.0)
        four = backtest.format_walk_forward_report(
            [self._window({"a": 1.0}) for _ in range(4)], 30, 10_000.0)

        assert "25.0%" in two      # 1/2^2
        assert "6.2%" in four      # 1/2^4

    def test_no_windows_is_reported_rather_than_crashing(self):
        report = backtest.format_walk_forward_report([], 30, 10_000.0)

        assert "Ямар ч цонх ажиллаагүй" in report
