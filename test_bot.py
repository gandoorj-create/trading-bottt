"""
bot.py-ийн тестүүд.

Сүлжээ рүү огт хандахгүй: conftest.py дэх autouse fixture-үүд requests-ийг
блоклож, Telegram илгээлтийг мок болгож, state файлуудыг tmp директорт чиглүүлж,
global state-ийг тест бүрийн өмнө цэвэрлэдэг.

Ажиллуулах:
    pip install -r requirements-dev.txt
    pytest -v
"""
import json

import pandas as pd
import pytest

import time

import account
import binance_client
import execution
import indicators
import market_data
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
        "SUPERTREND", "MACD_MOMENTUM", "GRID_TRADING",
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

    def test_grid_trading_prefers_range_regime(self):
        base_kwargs = dict(adx=10, rsi=50, atr_pct=1.0, volume_ratio=1.0,
                            ema_slope=0.0, sentiment=0.0, chop=70, mtf_signal="NEUTRAL")
        range_score = strategies.calculate_strategy_score("GRID_TRADING", regime="RANGE", **base_kwargs)
        trending_score = strategies.calculate_strategy_score("GRID_TRADING", regime="TRENDING", **base_kwargs)
        assert range_score > trending_score

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

    def test_rsi_strategy_buys_when_oversold(self):
        df = downtrend_df(n=260, start=500.0, step=1.0)
        signal = strategies.generate_strategy_signal("RSI_STRATEGY", df, sentiment=0.0, regime="RANGE")
        assert signal == "BUY"

    def test_rsi_strategy_holds_when_sentiment_opposes(self):
        # rsi < 30 боловч sentiment -0.6-аас доош бол BUY өгөх ёсгүй
        df = downtrend_df(n=260, start=500.0, step=1.0)
        signal = strategies.generate_strategy_signal("RSI_STRATEGY", df, sentiment=-0.9, regime="RANGE")
        assert signal == "HOLD"

    def test_trend_following_needs_trend_regime_alignment(self):
        df = noisy_uptrend_df(n=260, step=2.0)
        buy = strategies.generate_strategy_signal("TREND_FOLLOWING", df, sentiment=0.0, regime="STRONG_TREND")
        # sentiment хэт сөрөг бол ижил өгөгдөл дээр ч BUY гарахгүй
        blocked = strategies.generate_strategy_signal("TREND_FOLLOWING", df, sentiment=-0.9, regime="STRONG_TREND")
        assert buy in ("BUY", "HOLD")
        if buy == "BUY":
            assert blocked == "HOLD"

    def test_grid_trading_only_trades_in_range_regime(self):
        df = noisy_uptrend_df()
        assert strategies.generate_strategy_signal("GRID_TRADING", df, sentiment=0.0, regime="STRONG_TREND") == "HOLD"

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

        assert activation > 100.0

    def test_sell_activation_below_entry(self, monkeypatch, fake_symbol_info):
        monkeypatch.setattr(account, "get_positions", lambda: [])
        patch_setting(monkeypatch, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = position_manager.calculate_trailing_activation("BTCUSDT", "SELL", 100.0)

        assert activation < 100.0

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
                        lambda symbol, side, qty, entry, pos_side: (True, 103.0, 101.0))
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
                            lambda symbol, side, qty, entry, pos_side: (False, None, None))
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
    monkeypatch.setattr(position_manager, "manage_dca", lambda: None)
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
    patch_setting(monkeypatch, "CHART_SEND_ON_SIGNAL", False)
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
                            lambda symbol, side, qty, entry, pos_side:
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

    def test_dca_info_cleared_on_close(self, monkeypatch):
        monkeypatch.setattr(account, "get_trade_realized_pnl", lambda symbol, opened_at_ms: 5.0)
        bot_state.dca_info["BNBUSDT"] = {"level": 1}

        position_manager.finalize_trade("BNBUSDT", _trade_info(strategy="RECOVERED"))

        assert "BNBUSDT" not in bot_state.dca_info


class TestStrategyRestoreAcrossRestart:
    """Нээлттэй арилжааны symbol → стратеги холбоос дискэнд хадгалагдаж,
    дахин асахад сэргэх ёстой."""

    def _live_position(self, monkeypatch, symbol="BTCUSDT", amt=1.0):
        monkeypatch.setattr(account, "get_positions", lambda: [_position(symbol, amt=amt)])
        monkeypatch.setattr(binance_client, "send_signed_request", lambda *a, **kw: [])
        monkeypatch.setattr(position_manager, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side: (True, 103.0, 101.0))

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
                            lambda symbol, side, qty, entry, pos_side:
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
