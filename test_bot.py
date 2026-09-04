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

    def test_neutral_mtf_penalises_trend_strategies(self):
        base_kwargs = dict(adx=35, rsi=55, atr_pct=1.0, volume_ratio=2.0,
                            ema_slope=1.0, sentiment=0.2, regime="TRENDING", chop=30)
        with_mtf = bot.calculate_strategy_score("SUPERTREND", mtf_signal="BULLISH", **base_kwargs)
        without_mtf = bot.calculate_strategy_score("SUPERTREND", mtf_signal="NEUTRAL", **base_kwargs)
        assert without_mtf == pytest.approx(with_mtf - 5)

    def test_unknown_strategy_scores_zero(self):
        score = bot.calculate_strategy_score(
            "NOT_A_STRATEGY", adx=30, rsi=50, atr_pct=1.0, volume_ratio=1.0,
            ema_slope=1.0, sentiment=0.0, regime="TRENDING", chop=30, mtf_signal="BULLISH",
        )
        assert score == 0


# ----------------------------------------------------------------
# Signal generation
# ----------------------------------------------------------------

class TestGenerateStrategySignal:
    @pytest.mark.parametrize("strategy", bot.STRATEGY_NAMES)
    def test_returns_valid_signal_for_every_strategy(self, strategy):
        df = noisy_uptrend_df()
        signal = bot.generate_strategy_signal(strategy, df, sentiment=0.0, regime="TRENDING")
        assert signal in ("BUY", "SELL", "HOLD")

    def test_rsi_strategy_buys_when_oversold(self):
        df = downtrend_df(n=260, start=500.0, step=1.0)
        signal = bot.generate_strategy_signal("RSI_STRATEGY", df, sentiment=0.0, regime="RANGE")
        assert signal == "BUY"

    def test_rsi_strategy_holds_when_sentiment_opposes(self):
        # rsi < 30 боловч sentiment -0.6-аас доош бол BUY өгөх ёсгүй
        df = downtrend_df(n=260, start=500.0, step=1.0)
        signal = bot.generate_strategy_signal("RSI_STRATEGY", df, sentiment=-0.9, regime="RANGE")
        assert signal == "HOLD"

    def test_trend_following_needs_trend_regime_alignment(self):
        df = noisy_uptrend_df(n=260, step=2.0)
        buy = bot.generate_strategy_signal("TREND_FOLLOWING", df, sentiment=0.0, regime="STRONG_TREND")
        # sentiment хэт сөрөг бол ижил өгөгдөл дээр ч BUY гарахгүй
        blocked = bot.generate_strategy_signal("TREND_FOLLOWING", df, sentiment=-0.9, regime="STRONG_TREND")
        assert buy in ("BUY", "HOLD")
        if buy == "BUY":
            assert blocked == "HOLD"

    def test_grid_trading_only_trades_in_range_regime(self):
        df = noisy_uptrend_df()
        assert bot.generate_strategy_signal("GRID_TRADING", df, sentiment=0.0, regime="STRONG_TREND") == "HOLD"

    def test_bollinger_mean_reversion_holds_in_trend_regime(self):
        df = noisy_uptrend_df()
        assert bot.generate_strategy_signal(
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
        assert bot.decimals_from_step(step) == expected

    def test_invalid_step_falls_back_to_8(self):
        assert bot.decimals_from_step(0) == 8
        assert bot.decimals_from_step(None) == 8


class TestRounding:
    def test_quantity_rounds_down_to_step(self, fake_symbol_info):
        assert bot.round_quantity("BTCUSDT", 0.0019) == 0.001

    def test_price_rounds_down_to_tick(self, fake_symbol_info):
        assert bot.round_price("BTCUSDT", 100.19) == pytest.approx(100.1)

    def test_integer_step_truncates_fraction(self, fake_symbol_info):
        assert bot.round_quantity("DOGEUSDT", 15.9) == 15.0

    def test_unknown_symbol_returns_none(self, fake_symbol_info):
        assert bot.round_quantity("FAKEUSDT", 1.0) is None
        assert bot.round_price("FAKEUSDT", 1.0) is None


class TestFormatting:
    def test_price_never_uses_scientific_notation(self, fake_symbol_info):
        # str(0.00001) нь '1e-05' болдог — Binance үүнийг татгалзана
        formatted = bot.format_price("DOGEUSDT", 0.00001)
        assert "e" not in formatted
        assert formatted == "0.00001"

    def test_qty_uses_step_precision(self, fake_symbol_info):
        assert bot.format_qty("BTCUSDT", 0.5) == "0.500"

    def test_unknown_symbol_falls_back_to_8_decimals(self, fake_symbol_info):
        assert bot.format_qty("FAKEUSDT", 1.5) == "1.50000000"


class TestCheckMinNotional:
    def test_rejects_below_min_notional(self, fake_symbol_info):
        # 50000 * 0.001 = $50 < $100 minNotional
        assert bot.check_min_notional("BTCUSDT", 50000, 0.001) is False

    def test_accepts_at_or_above_min_notional(self, fake_symbol_info):
        assert bot.check_min_notional("BTCUSDT", 50000, 0.01) is True

    def test_unknown_symbol_passes_through(self, fake_symbol_info):
        assert bot.check_min_notional("FAKEUSDT", 1, 1) is True


# ----------------------------------------------------------------
# Rate limit backoff
# ----------------------------------------------------------------

class TestRateLimitWait:
    def test_honours_retry_after_header(self):
        assert bot._rate_limit_wait(FakeResponse({"Retry-After": "42"}), attempt=0) == 42

    def test_exponential_backoff_without_header(self):
        assert bot._rate_limit_wait(FakeResponse(), attempt=0) == 2
        assert bot._rate_limit_wait(FakeResponse(), attempt=3) == 16

    def test_backoff_capped_at_60s(self):
        assert bot._rate_limit_wait(FakeResponse(), attempt=20) == 60

    def test_malformed_retry_after_ignored(self):
        assert bot._rate_limit_wait(FakeResponse({"Retry-After": "soon"}), attempt=0) == 2


# ----------------------------------------------------------------
# Drawdown circuit breaker (риск удирдлагын хамгийн чухал хэсэг)
# ----------------------------------------------------------------

class TestDrawdownCircuitBreaker:
    def test_halts_when_drawdown_exceeds_limit(self, monkeypatch, telegram_messages):
        monkeypatch.setattr(bot, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot, "get_usdt_balance", lambda: 800.0)  # -20%

        bot.check_drawdown_circuit_breaker()

        assert bot.drawdown_halt is True
        assert bot.safety_lock is True
        assert any("DRAWDOWN" in m for m in telegram_messages)

    def test_does_not_halt_below_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot, "get_usdt_balance", lambda: 950.0)  # -5%

        bot.check_drawdown_circuit_breaker()

        assert bot.drawdown_halt is False
        assert bot.safety_lock is False

    def test_new_high_updates_peak_and_clears_lock(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot, "drawdown_lock_active", True)
        monkeypatch.setattr(bot, "get_usdt_balance", lambda: 1200.0)

        bot.check_drawdown_circuit_breaker()

        assert bot.session_peak_balance == 1200.0
        assert bot.drawdown_lock_active is False

    def test_disabled_when_limit_is_zero(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_SESSION_DRAWDOWN_PCT", 0.0)
        monkeypatch.setattr(bot, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot, "get_usdt_balance", lambda: 1.0)  # -99.9%

        bot.check_drawdown_circuit_breaker()

        assert bot.drawdown_halt is False

    def test_zero_balance_does_not_trigger_false_halt(self, monkeypatch):
        # Баланс уншиж чадаагүй (0.0 буцсан) тохиолдолд halt хийх ёсгүй
        monkeypatch.setattr(bot, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot, "get_usdt_balance", lambda: 0.0)

        bot.check_drawdown_circuit_breaker()

        assert bot.drawdown_halt is False

    def test_does_not_re_trigger_while_safety_locked(self, monkeypatch, telegram_messages):
        monkeypatch.setattr(bot, "MAX_SESSION_DRAWDOWN_PCT", 15.0)
        monkeypatch.setattr(bot, "session_peak_balance", 1000.0)
        monkeypatch.setattr(bot, "safety_lock", True)
        monkeypatch.setattr(bot, "get_usdt_balance", lambda: 500.0)

        bot.check_drawdown_circuit_breaker()

        assert telegram_messages == []


# ----------------------------------------------------------------
# Strategy performance / cooldown
# ----------------------------------------------------------------

class TestUpdateStrategyPerformance:
    def test_win_increments_wins_and_resets_streak(self, monkeypatch):
        bot.strategy_stats["RSI_STRATEGY"]["consecutive_losses"] = 2
        bot.update_strategy_performance("RSI_STRATEGY", 25.0)

        stats = bot.strategy_stats["RSI_STRATEGY"]
        assert stats["trades"] == 1
        assert stats["wins"] == 1
        assert stats["consecutive_losses"] == 0
        assert stats["total_pnl"] == 25.0

    def test_loss_increments_streak(self, monkeypatch):
        monkeypatch.setattr(bot, "CONSECUTIVE_LOSS_LIMIT", 3)
        bot.update_strategy_performance("RSI_STRATEGY", -10.0)

        stats = bot.strategy_stats["RSI_STRATEGY"]
        assert stats["losses"] == 1
        assert stats["consecutive_losses"] == 1
        assert stats["active"] is True

    def test_pauses_strategy_after_loss_limit(self, monkeypatch, telegram_messages):
        monkeypatch.setattr(bot, "ADAPTIVE_STRATEGY", True)
        monkeypatch.setattr(bot, "CONSECUTIVE_LOSS_LIMIT", 3)
        monkeypatch.setattr(bot, "STRATEGY_COOLDOWN_CYCLES", 2)

        for _ in range(3):
            bot.update_strategy_performance("RSI_STRATEGY", -10.0)

        stats = bot.strategy_stats["RSI_STRATEGY"]
        assert stats["active"] is False
        assert stats["paused_cycles"] == 2
        assert any("PAUSED" in m for m in telegram_messages)

    def test_adaptive_disabled_never_pauses(self, monkeypatch):
        monkeypatch.setattr(bot, "ADAPTIVE_STRATEGY", False)
        monkeypatch.setattr(bot, "CONSECUTIVE_LOSS_LIMIT", 2)

        for _ in range(5):
            bot.update_strategy_performance("RSI_STRATEGY", -10.0)

        assert bot.strategy_stats["RSI_STRATEGY"]["active"] is True

    def test_unknown_strategy_is_ignored(self):
        bot.update_strategy_performance("NOT_A_STRATEGY", -10.0)
        assert "NOT_A_STRATEGY" not in bot.strategy_stats

    def test_session_pnl_accumulates(self):
        bot.update_strategy_performance("RSI_STRATEGY", 10.0)
        bot.update_strategy_performance("MACD_MOMENTUM", -4.0)
        assert bot.session_realized_pnl == pytest.approx(6.0)


class TestStrategyCooldowns:
    def test_paused_cycles_count_down(self):
        bot.strategy_stats["RSI_STRATEGY"].update(active=False, paused_cycles=2)
        bot.update_strategy_cooldowns()

        assert bot.strategy_stats["RSI_STRATEGY"]["paused_cycles"] == 1
        assert bot.strategy_stats["RSI_STRATEGY"]["active"] is False

    def test_reactivates_when_cooldown_finishes(self, telegram_messages):
        bot.strategy_stats["RSI_STRATEGY"].update(
            active=False, paused_cycles=1, consecutive_losses=3
        )
        bot.update_strategy_cooldowns()

        stats = bot.strategy_stats["RSI_STRATEGY"]
        assert stats["active"] is True
        assert stats["consecutive_losses"] == 0
        assert any("REACTIVATED" in m for m in telegram_messages)

    def test_active_strategies_excludes_paused(self):
        bot.strategy_stats["RSI_STRATEGY"]["active"] = False
        active = bot.get_active_strategies()

        assert "RSI_STRATEGY" not in active
        assert "SUPERTREND" in active


# ----------------------------------------------------------------
# State persistence
# ----------------------------------------------------------------

class TestStatePersistence:
    def test_strategy_state_roundtrip(self):
        bot.strategy_stats["RSI_STRATEGY"].update(trades=7, wins=4, total_pnl=123.45)
        bot.save_strategy_state()

        bot.strategy_stats["RSI_STRATEGY"].update(trades=0, wins=0, total_pnl=0.0)
        bot.load_strategy_state()

        stats = bot.strategy_stats["RSI_STRATEGY"]
        assert stats["trades"] == 7
        assert stats["wins"] == 4
        assert stats["total_pnl"] == pytest.approx(123.45)

    def test_missing_strategy_file_is_noop(self):
        bot.strategy_stats["RSI_STRATEGY"]["trades"] = 3
        bot.load_strategy_state()  # файл байхгүй
        assert bot.strategy_stats["RSI_STRATEGY"]["trades"] == 3

    def test_corrupt_strategy_file_does_not_crash(self, isolated_state_files):
        (isolated_state_files / "strategy_state.json").write_text("{ энэ бол JSON биш")
        bot.strategy_stats["RSI_STRATEGY"]["trades"] = 5

        bot.load_strategy_state()  # алдаа шидэх ёсгүй

        assert bot.strategy_stats["RSI_STRATEGY"]["trades"] == 5

    def test_session_state_roundtrip(self, monkeypatch):
        monkeypatch.setattr(bot, "session_peak_balance", 1500.0)
        monkeypatch.setattr(bot, "session_start_balance", 1000.0)
        bot.save_session_state()

        data = bot.load_session_state()

        assert data["session_peak_balance"] == 1500.0
        assert data["session_start_balance"] == 1000.0
        assert "saved_at" in data

    def test_missing_session_file_returns_none(self):
        assert bot.load_session_state() is None

    def test_non_dict_session_file_returns_none(self, isolated_state_files):
        (isolated_state_files / "session_state.json").write_text(json.dumps([1, 2, 3]))
        assert bot.load_session_state() is None


# ----------------------------------------------------------------
# Trailing activation
# ----------------------------------------------------------------

class TestTrailingActivation:
    def test_buy_activation_above_entry(self, monkeypatch, fake_symbol_info):
        monkeypatch.setattr(bot, "get_positions", lambda: [])
        monkeypatch.setattr(bot, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = bot.calculate_trailing_activation("BTCUSDT", "BUY", 100.0)

        assert activation > 100.0

    def test_sell_activation_below_entry(self, monkeypatch, fake_symbol_info):
        monkeypatch.setattr(bot, "get_positions", lambda: [])
        monkeypatch.setattr(bot, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = bot.calculate_trailing_activation("BTCUSDT", "SELL", 100.0)

        assert activation < 100.0

    def test_buy_activation_stays_above_mark_price(self, monkeypatch, fake_symbol_info):
        # Mark price аль хэдийн entry-ээс дээш яваад байвал activation түүнээс дээш байх ёстой
        monkeypatch.setattr(bot, "get_positions", lambda: [
            {"symbol": "BTCUSDT", "markPrice": 110.0}
        ])
        monkeypatch.setattr(bot, "TRAILING_ACTIVATION_PCT", 1.0)

        activation = bot.calculate_trailing_activation("BTCUSDT", "BUY", 100.0)

        assert activation > 110.0


# ----------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------

class TestCorrelation:
    def test_identical_series_correlate_to_one(self, monkeypatch):
        df = noisy_uptrend_df(n=60)
        monkeypatch.setattr(bot, "get_klines", lambda symbol, interval="1h", limit=200: df.copy())

        corr = bot.calculate_correlation("BTCUSDT", "ETHUSDT", lookback=50)

        assert corr == pytest.approx(1.0)

    def test_inverse_series_correlate_negatively(self, monkeypatch):
        up = noisy_uptrend_df(n=60)
        down = make_df([300.0 - c for c in up["close"]])

        def fake_klines(symbol, interval="1h", limit=200):
            return up.copy() if symbol == "BTCUSDT" else down.copy()

        monkeypatch.setattr(bot, "get_klines", fake_klines)

        corr = bot.calculate_correlation("BTCUSDT", "ETHUSDT", lookback=50)

        # Толин тусгал үнэ — pct_change суурь өөр тул яг -1.0 болохгүй ч
        # хүчтэй сөрөг correlation байх ёстой
        assert corr < -0.9

    def test_short_history_returns_zero(self, monkeypatch):
        short = noisy_uptrend_df(n=5)
        monkeypatch.setattr(bot, "get_klines", lambda symbol, interval="1h", limit=200: short.copy())

        assert bot.calculate_correlation("BTCUSDT", "ETHUSDT", lookback=50) == 0.0

    def test_api_failure_returns_zero(self, monkeypatch):
        def boom(symbol, interval="1h", limit=200):
            raise ValueError("Kline error")

        monkeypatch.setattr(bot, "get_klines", boom)

        assert bot.calculate_correlation("BTCUSDT", "ETHUSDT") == 0.0

    def test_cache_avoids_recomputation(self, monkeypatch):
        calls = []

        def counted(symbol1, symbol2, lookback=50):
            calls.append((symbol1, symbol2))
            return 0.5

        monkeypatch.setattr(bot, "calculate_correlation", counted)
        monkeypatch.setattr(bot, "CORRELATION_CACHE_TTL", 3600)

        bot.calculate_correlation_cached("BTCUSDT", "ETHUSDT")
        bot.calculate_correlation_cached("BTCUSDT", "ETHUSDT")
        # эсрэг дараалал ч ижил кэшийг ашиглах ёстой
        bot.calculate_correlation_cached("ETHUSDT", "BTCUSDT")

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

    monkeypatch.setattr(bot, "place_market_order", fake_order)
    monkeypatch.setattr(bot, "cancel_all_symbol_orders", lambda symbol: None)
    monkeypatch.setattr(bot, "ensure_leverage", lambda symbol, leverage=None: True)
    monkeypatch.setattr(bot, "get_actual_leverage", lambda symbol: 5)
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
    monkeypatch.setattr(bot, "get_positions", lambda: [])
    monkeypatch.setattr(bot, "get_position_mode", lambda: False)
    monkeypatch.setattr(bot, "current_timestamp_ms", lambda: 1_700_000_000_000)
    monkeypatch.setattr(bot, "rebuild_protection_orders",
                        lambda symbol, side, qty, entry, pos_side: (True, 103.0, 101.0))
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)
    return order_spy


class TestExecuteTradesHappyPath:
    """Positive control: хамгаалалтын тестүүд утгагүй pass болохгүйг батална."""

    def test_valid_signal_places_order(self, tradeable):
        bot.execute_trades([_coin()], total_balance=1000.0)

        assert len(tradeable) == 1
        assert tradeable[0]["symbol"] == "BTCUSDT"
        assert tradeable[0]["side"] == "BUY"

    def test_sell_signal_places_sell_order(self, tradeable):
        bot.execute_trades([_coin(signal="SELL")], total_balance=1000.0)

        assert tradeable[0]["side"] == "SELL"

    def test_position_size_follows_allocation_and_leverage(self, monkeypatch, tradeable):
        monkeypatch.setattr(bot, "TRADE_ALLOCATION", 0.09)
        monkeypatch.setattr(bot, "LEVERAGE", 5)

        bot.execute_trades([_coin(price=100.0)], total_balance=1000.0)

        # margin = 1000 * 0.09 = 90, notional = 90 * 5 = 450, qty = 450 / 100 = 4.5
        assert tradeable[0]["quantity"] == pytest.approx(4.5)

    def test_opened_trade_is_tracked(self, tradeable):
        bot.execute_trades([_coin()], total_balance=1000.0)

        assert "BTCUSDT" in bot.active_trade_info
        assert bot.active_trade_info["BTCUSDT"]["strategy"] == "RSI_STRATEGY"

    def test_failed_protection_closes_position_immediately(self, monkeypatch, tradeable, telegram_messages):
        monkeypatch.setattr(bot, "rebuild_protection_orders",
                            lambda symbol, side, qty, entry, pos_side: (False, None, None))
        monkeypatch.setattr(bot, "get_trade_realized_pnl", lambda symbol, opened_at_ms: -1.5)

        bot.execute_trades([_coin()], total_balance=1000.0)

        # нээх + яаралтай хаах = 2 захиалга, позиц хөтлөгдөж үлдэх ёсгүй
        assert len(tradeable) == 2
        assert tradeable[1]["side"] == "SELL"
        assert "BTCUSDT" not in bot.active_trade_info
        assert any("EMERGENCY CLOSED" in m for m in telegram_messages)

    def test_unfilled_order_does_not_create_phantom_position(self, monkeypatch, tradeable, telegram_messages):
        monkeypatch.setattr(bot, "place_market_order",
                            lambda *a, **kw: {"status": "EXPIRED", "executedQty": 0, "avgPrice": 0})

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert "BTCUSDT" not in bot.active_trade_info
        assert any("NOT FILLED" in m for m in telegram_messages)


class TestExecuteTradesGuards:
    """
    Тест бүр `tradeable` fixture дээр суурилна — өөрөөр хэлбэл захиалга гарах
    бүх нөхцөл бүрдсэн байх бөгөөд ЗӨВХӨН шалгаж буй хамгаалалт нь захиалгыг
    зогсоох ёстой. Ингэснээр хамаагүй өөр шалтгаанаар "pass" болохгүй.
    """

    def test_safety_lock_blocks_all_trades(self, monkeypatch, tradeable):
        monkeypatch.setattr(bot, "safety_lock", True)

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_empty_selection_places_no_orders(self, tradeable):
        bot.execute_trades([], total_balance=1000.0)

        assert tradeable == []

    def test_hold_signal_is_skipped(self, tradeable):
        bot.execute_trades([_coin(signal="HOLD")], total_balance=1000.0)

        assert tradeable == []

    def test_symbol_with_existing_position_is_skipped(self, monkeypatch, tradeable):
        monkeypatch.setattr(bot, "get_positions", lambda: [
            {"symbol": "BTCUSDT", "positionAmt": 0.5, "entryPrice": 100.0}
        ])

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_low_balance_stops_trading(self, monkeypatch, tradeable, telegram_messages):
        monkeypatch.setattr(bot, "MIN_BALANCE_USDT", 10.0)

        bot.execute_trades([_coin()], total_balance=5.0)

        assert tradeable == []
        assert any("БАЛАНС" in m for m in telegram_messages)

    def test_margin_cap_blocks_new_trade(self, monkeypatch, tradeable):
        # Байгаа позиц: 30 * $100 / 5x = $600 margin.
        # Дээд хязгаар: $1000 * 0.55 = $550 — шинэ арилжаа багтахгүй.
        monkeypatch.setattr(bot, "get_positions", lambda: [
            {"symbol": "ETHUSDT", "positionAmt": 30.0, "entryPrice": 100.0}
        ])
        monkeypatch.setattr(bot, "MAX_TOTAL_MARGIN_USAGE", 0.55)
        monkeypatch.setattr(bot, "TRADE_ALLOCATION", 0.09)

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_margin_cap_allows_trade_within_limit(self, monkeypatch, tradeable):
        # Байгаа позиц: 1 * $100 / 5x = $20 margin — хязгаарт багтана
        monkeypatch.setattr(bot, "get_positions", lambda: [
            {"symbol": "ETHUSDT", "positionAmt": 1.0, "entryPrice": 100.0}
        ])
        monkeypatch.setattr(bot, "MAX_TOTAL_MARGIN_USAGE", 0.55)
        monkeypatch.setattr(bot, "TRADE_ALLOCATION", 0.09)

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert len(tradeable) == 1

    def test_unprotected_symbol_is_skipped(self, monkeypatch, tradeable):
        monkeypatch.setattr(bot, "unprotected_symbols", {"BTCUSDT"})

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_below_min_notional_is_skipped(self, tradeable, fake_symbol_info):
        fake_symbol_info["BTCUSDT"]["minNotional"] = 100_000.0

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_below_min_qty_is_skipped(self, tradeable, fake_symbol_info):
        fake_symbol_info["BTCUSDT"]["minQty"] = 1000.0

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_leverage_failure_skips_symbol(self, monkeypatch, tradeable):
        monkeypatch.setattr(bot, "ensure_leverage", lambda symbol, leverage=None: False)

        bot.execute_trades([_coin()], total_balance=1000.0)

        assert tradeable == []

    def test_unknown_symbol_without_exchange_info_is_skipped(self, tradeable):
        bot.execute_trades([_coin(symbol="FAKEUSDT")], total_balance=1000.0)

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

    monkeypatch.setattr(bot, "finalize_trade",
                        lambda symbol, trade_data: finalized.append(symbol) or 12.5)
    monkeypatch.setattr(bot, "cancel_all_symbol_orders", lambda symbol: None)
    monkeypatch.setattr(bot, "manage_dca", lambda: None)
    monkeypatch.setattr(bot, "get_usdt_balance", lambda: 1000.0)
    monkeypatch.setattr(bot, "last_telegram_report_time", 0.0)
    return finalized


class TestMonitorPositions:
    def test_closed_position_is_finalized_and_untracked(self, monkeypatch, monitor_env):
        # Bot нь BTCUSDT-г хөтөлж байсан ч биржид байхгүй болсон = хаагдсан
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "get_positions", lambda: [])

        bot.monitor_positions()

        assert monitor_env == ["BTCUSDT"]
        assert "BTCUSDT" not in bot.active_trade_info

    def test_open_position_is_not_finalized(self, monkeypatch, monitor_env):
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "get_positions", lambda: [_position()])

        bot.monitor_positions()

        assert monitor_env == []
        assert "BTCUSDT" in bot.active_trade_info

    def test_only_the_closed_symbol_is_finalized(self, monkeypatch, monitor_env):
        monkeypatch.setattr(bot, "active_trade_info", {
            "BTCUSDT": _trade_info(),
            "ETHUSDT": _trade_info(),
        })
        monkeypatch.setattr(bot, "get_positions", lambda: [_position("ETHUSDT")])

        bot.monitor_positions()

        assert monitor_env == ["BTCUSDT"]
        assert set(bot.active_trade_info) == {"ETHUSDT"}

    def test_cancels_leftover_orders_of_closed_position(self, monkeypatch, monitor_env):
        cancelled = []
        monkeypatch.setattr(bot, "cancel_all_symbol_orders", lambda symbol: cancelled.append(symbol))
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "get_positions", lambda: [])

        bot.monitor_positions()

        assert cancelled == ["BTCUSDT"]

    def test_cancel_failure_does_not_break_monitoring(self, monkeypatch, monitor_env):
        def boom(symbol):
            raise RuntimeError("API down")

        monkeypatch.setattr(bot, "cancel_all_symbol_orders", boom)
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "get_positions", lambda: [])

        bot.monitor_positions()  # алдаа шидэх ёсгүй

        assert monitor_env == ["BTCUSDT"]

    def test_report_sent_when_interval_elapsed(self, monkeypatch, monitor_env, telegram_messages):
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "get_positions", lambda: [_position()])
        monkeypatch.setattr(bot, "TELEGRAM_REPORT_INTERVAL_SEC", 0)

        bot.monitor_positions()

        assert any("МОНИТОР" in m for m in telegram_messages)
        assert bot.last_telegram_report_time > 0

    def test_report_throttled_within_interval(self, monkeypatch, monitor_env, telegram_messages):
        import time as _time

        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "get_positions", lambda: [_position()])
        monkeypatch.setattr(bot, "TELEGRAM_REPORT_INTERVAL_SEC", 300)
        monkeypatch.setattr(bot, "last_telegram_report_time", _time.time())

        bot.monitor_positions()

        assert telegram_messages == []

    def test_no_positions_sends_no_report(self, monkeypatch, monitor_env, telegram_messages):
        monkeypatch.setattr(bot, "active_trade_info", {})
        monkeypatch.setattr(bot, "get_positions", lambda: [])
        monkeypatch.setattr(bot, "TELEGRAM_REPORT_INTERVAL_SEC", 0)

        bot.monitor_positions()

        assert telegram_messages == []


# ----------------------------------------------------------------
# handle_target_reached — ашгийн зорилтод хүрэхэд бүх позиц хаагдах ёстой
# ----------------------------------------------------------------

@pytest.fixture
def target_env(monkeypatch):
    monkeypatch.setattr(bot, "get_usdt_balance", lambda: 1300.0)
    monkeypatch.setattr(bot, "finalize_trade", lambda symbol, trade_data: 150.0)
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bot, "TARGET_PROFIT", 300.0)


class TestHandleTargetReached:
    def test_successful_close_returns_true_and_clears_tracking(self, monkeypatch, target_env, telegram_messages):
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "close_all_positions_and_verify", lambda: True)
        monkeypatch.setattr(bot, "get_positions", lambda: [])

        assert bot.handle_target_reached(310.0) is True
        assert bot.active_trade_info == {}
        assert any("TARGET REALIZED" in m for m in telegram_messages)

    def test_safety_lock_engaged_before_closing(self, monkeypatch, target_env):
        seen = {}

        def fake_close():
            seen["locked_during_close"] = bot.safety_lock
            return True

        monkeypatch.setattr(bot, "active_trade_info", {})
        monkeypatch.setattr(bot, "close_all_positions_and_verify", fake_close)
        monkeypatch.setattr(bot, "get_positions", lambda: [])

        bot.handle_target_reached(310.0)

        # Хаах явцад шинэ арилжаа нээгдэхээс сэргийлж түгжээ тавьсан байх ёстой
        assert seen["locked_during_close"] is True

    def test_failed_close_returns_false_and_keeps_lock(self, monkeypatch, target_env, telegram_messages):
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "close_all_positions_and_verify", lambda: False)
        monkeypatch.setattr(bot, "get_positions", lambda: [_position()])

        assert bot.handle_target_reached(310.0) is False
        assert bot.safety_lock is True
        assert any("CLOSE INCOMPLETE" in m for m in telegram_messages)

    def test_leftover_position_after_close_fails_safety_check(self, monkeypatch, target_env, telegram_messages):
        # close_all нь True гэж мэдээлсэн ч бодит байдал дээр позиц үлдсэн
        monkeypatch.setattr(bot, "active_trade_info", {"BTCUSDT": _trade_info()})
        monkeypatch.setattr(bot, "close_all_positions_and_verify", lambda: True)
        monkeypatch.setattr(bot, "get_positions", lambda: [_position()])

        assert bot.handle_target_reached(310.0) is False
        assert bot.safety_lock is True
        assert any("FINAL SAFETY CHECK FAILED" in m for m in telegram_messages)

    def test_all_tracked_trades_are_finalized(self, monkeypatch, target_env):
        finalized = []
        monkeypatch.setattr(bot, "finalize_trade",
                            lambda symbol, trade_data: finalized.append(symbol) or 100.0)
        monkeypatch.setattr(bot, "active_trade_info", {
            "BTCUSDT": _trade_info(), "ETHUSDT": _trade_info(),
        })
        monkeypatch.setattr(bot, "close_all_positions_and_verify", lambda: True)
        monkeypatch.setattr(bot, "get_positions", lambda: [])

        bot.handle_target_reached(310.0)

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
    monkeypatch.setattr(bot, "get_positions", lambda: [])
    monkeypatch.setattr(bot, "get_usdt_balance", lambda: 1000.0)
    monkeypatch.setattr(bot, "get_actual_leverage", lambda symbol: 5)
    monkeypatch.setattr(bot, "send_selection_report",
                        lambda selected, all_candidates=None, skipped_reasons=None: None)
    monkeypatch.setattr(bot, "CHART_SEND_ON_SIGNAL", False)
    monkeypatch.setattr(bot, "CORRELATION_ENABLED", False)
    monkeypatch.setattr(bot, "MIN_SIGNAL_SCORE", 20.0)
    monkeypatch.setattr(bot, "MAX_SELECTIONS", 6)


def _use_analyses(monkeypatch, analyses):
    by_symbol = {a["symbol"]: a for a in analyses}
    monkeypatch.setattr(bot, "SYMBOLS_POOL", list(by_symbol))
    monkeypatch.setattr(
        bot, "analyze_coin",
        lambda symbol, check_correlation=True, active_symbols=None: by_symbol.get(symbol),
    )


class TestScreenCoins:
    def test_selects_signal_above_min_score(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 50.0)})])

        selected = bot.screen_coins()

        assert [c["symbol"] for c in selected] == ["BTCUSDT"]

    def test_low_score_signal_is_filtered_out(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 5.0)})])

        assert bot.screen_coins() == []

    def test_hold_signal_is_ignored(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("HOLD", 90.0)})])

        assert bot.screen_coins() == []

    def test_paused_strategy_is_ignored(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)})])
        bot.strategy_stats["RSI_STRATEGY"]["active"] = False

        assert bot.screen_coins() == []

    def test_duplicate_symbol_keeps_highest_score(self, monkeypatch, screen_env):
        _use_analyses(monkeypatch, [_analysis("BTCUSDT", {
            "RSI_STRATEGY": ("BUY", 40.0),
            "SUPERTREND": ("BUY", 80.0),
        })])

        selected = bot.screen_coins()

        assert len(selected) == 1
        assert selected[0]["strategy"] == "SUPERTREND"

    def test_respects_max_selections(self, monkeypatch, screen_env):
        monkeypatch.setattr(bot, "MAX_SELECTIONS", 2)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"SUPERTREND": ("BUY", 80.0)}),
            _analysis("SOLUSDT", {"MACD_MOMENTUM": ("BUY", 70.0)}),
        ])

        assert len(bot.screen_coins()) == 2

    def test_correlated_symbol_is_removed(self, monkeypatch, screen_env):
        monkeypatch.setattr(bot, "CORRELATION_ENABLED", True)
        monkeypatch.setattr(bot, "CORRELATION_THRESHOLD", 0.65)
        monkeypatch.setattr(bot, "calculate_correlation_cached",
                            lambda s1, s2, lookback=50: 0.95)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"SUPERTREND": ("BUY", 80.0)}),
        ])

        selected = bot.screen_coins()

        assert len(selected) == 1

    def test_uncorrelated_symbols_both_kept(self, monkeypatch, screen_env):
        monkeypatch.setattr(bot, "CORRELATION_ENABLED", True)
        monkeypatch.setattr(bot, "CORRELATION_THRESHOLD", 0.65)
        monkeypatch.setattr(bot, "calculate_correlation_cached",
                            lambda s1, s2, lookback=50: 0.1)
        _use_analyses(monkeypatch, [
            _analysis("BTCUSDT", {"RSI_STRATEGY": ("BUY", 90.0)}),
            _analysis("ETHUSDT", {"SUPERTREND": ("BUY", 80.0)}),
        ])

        assert len(bot.screen_coins()) == 2

    def test_failed_analysis_is_skipped(self, monkeypatch, screen_env):
        # analyze_coin алдаа гарвал None буцаадаг — энэ нь бүх screening-ийг унагаах ёсгүй
        monkeypatch.setattr(bot, "SYMBOLS_POOL", ["BTCUSDT", "ETHUSDT"])
        monkeypatch.setattr(
            bot, "analyze_coin",
            lambda symbol, check_correlation=True, active_symbols=None:
                None if symbol == "BTCUSDT" else _analysis("ETHUSDT", {"RSI_STRATEGY": ("BUY", 50.0)}),
        )

        selected = bot.screen_coins()

        assert [c["symbol"] for c in selected] == ["ETHUSDT"]
