"""
settings.py
Нууц зүйлийг .env-ээс, тохиргоог config.json-оос уншина.
"""
import os
import json
from dotenv import load_dotenv

load_dotenv()

# ---- Нууц (secrets) ----
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://demo-fapi.binance.com")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API_ROOT = os.environ.get("TELEGRAM_API_ROOT", "https://api.telegram.org")

# ---- State хадгалах директор ----
# Railway дээр deploy болгонд контейнер шинээр үүсдэг тул кодын хавтас руу
# бичсэн файл алга болдог. Volume mount хийгээд STATE_DIR-ийг түүн рүү
# (жишээ нь /data) заавал бот restart хийсний дараа ч drawdown-ы оргил утга,
# нээлттэй арилжааны стратегиэ санана.
STATE_DIR = os.environ.get("STATE_DIR") or os.path.dirname(__file__)
STATE_DIR_IS_PERSISTENT = bool(os.environ.get("STATE_DIR"))

# ---- Тохиргоо (config.json) ----
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
    _cfg = json.load(f)

# ---- Үндсэн тохиргоо ----
SYMBOLS_POOL = _cfg["symbols_pool"]
SELECTION_INTERVAL_MINUTES = _cfg["selection_interval_minutes"]
MONITOR_INTERVAL_SEC = _cfg["monitor_interval_sec"]
TELEGRAM_REPORT_INTERVAL_SEC = _cfg["telegram_report_interval_sec"]
MAX_SELECTIONS = _cfg["max_selections"]
MAX_CANDIDATES_PER_STRATEGY = _cfg.get("max_candidates_per_strategy", 1)

TRADE_ALLOCATION = _cfg["trade_allocation"]
LEVERAGE = _cfg["leverage"]

# ---- ATR-аар эрсдэлээ тэнцүүлсэн хэмжээ ба гарц ----
# Өмнө нь coin болгон балансын ижил 9%-ийг авдаг байсан: 0.4% ATR-тай BTC ба
# 2.5% ATR-тай DOGE хоёр ижил хэмжээтэй байхад бодит эрсдэл нь 6 дахин зөрдөг.
# Одоо stop нь ATR-аас хамаарч сунадаг ба хэмжээ нь "stop цохиход балансын
# RISK_PER_TRADE_PCT-ийг алдана" гэдгээс урвуугаар тооцогдоно.
ATR_RISK_SIZING_ENABLED = _cfg.get("atr_risk_sizing_enabled", False)
RISK_PER_TRADE_PCT = _cfg.get("risk_per_trade_pct", 1.25)
ATR_SL_MULTIPLIER = _cfg.get("atr_sl_multiplier", 2.0)
ATR_SL_MIN_PCT = _cfg.get("atr_sl_min_pct", 2.0)
ATR_SL_MAX_PCT = _cfg.get("atr_sl_max_pct", 5.0)
# Эрсдэлийн тооцоо ганц coin руу хэт их хөрөнгө хийхээс сэргийлсэн хашлага —
# 6 позиц зэрэг барих боломж хадгалагдана.
MIN_TRADE_ALLOCATION = _cfg.get("min_trade_allocation", 0.05)
MAX_TRADE_ALLOCATION = _cfg.get("max_trade_allocation", 0.13)

# ---- Хугацааны stop ----
# Чиглэлээ өгөөгүй позиц слот, маржин, funding идсээр байдаг. Тодорхой хугацаа
# өнгөрөөд TIME_STOP_FLAT_PCT дотор хэвтсэн хэвээр байвал хааж, дараагийн
# signal-д зай гаргана. Ашигтай яваа позицыг хөндөхгүй.
MAX_HOLD_HOURS = _cfg.get("max_hold_hours", 0)
TIME_STOP_FLAT_PCT = _cfg.get("time_stop_flat_pct", 1.0)

TRAILING_CALLBACK_RATE = _cfg["trailing_callback_rate"]
TRAILING_ACTIVATION_PCT = _cfg["trailing_activation_pct"]
TAKE_PROFIT_PCT = _cfg["take_profit_pct"]
EMERGENCY_SL_PCT = _cfg["emergency_sl_pct"]

# ---- Хэсэгчилсэн take-profit (scale-out) ----
# Позицын нэг хэсгийг эрт (PARTIAL_TP_PCT дээр) тасалж аваад, үлдсэн хэсгийн
# stop-ыг breakeven руу зөөнө. Ингэснээр "+2% хүрээд буцаж SL цохисон" арилжаа
# бүтэн алдагдал байхаа болиод бага зэрэг ашигтай хаагдана. Хариуд нь бүтэн
# 4.5% хүрсэн арилжааны ашиг талаараа багасна — энэ бол ухамсартай солилцоо.
PARTIAL_TP_ENABLED = _cfg.get("partial_tp_enabled", False)
PARTIAL_TP_PCT = _cfg.get("partial_tp_pct", 2.0)
PARTIAL_TP_RATIO = _cfg.get("partial_tp_ratio", 0.5)
# Breakeven нь яг entry биш: орох/гарах шимтгэл (~0.08% notional) -ыг нөхөх
# бага зэргийн зайтай байх ёстой, эс тэгвээс "breakeven" нь бодитоор алдагдал.
BREAKEVEN_OFFSET_PCT = _cfg.get("breakeven_offset_pct", 0.1)

TARGET_PROFIT = _cfg["target_profit_usdt"]
TARGET_COOLDOWN_SEC = _cfg["target_cooldown_sec"]

CLOSE_VERIFY_ATTEMPTS = _cfg["close_verify_attempts"]
CLOSE_VERIFY_DELAY_SEC = _cfg["close_verify_delay_sec"]

MIN_SIGNAL_SCORE = _cfg["min_signal_score"]
MIN_BALANCE_USDT = _cfg["min_balance_usdt"]
MAX_TOTAL_MARGIN_USAGE = _cfg["max_total_margin_usage"]
MAX_SESSION_DRAWDOWN_PCT = _cfg.get("max_session_drawdown_pct", 0.0)
REQUEST_TIMEOUT = _cfg["request_timeout"]
PNL_LOOKBACK_LIMIT = _cfg["pnl_lookback_limit"]

ADAPTIVE_STRATEGY = _cfg["adaptive_strategy"]
STRATEGY_PERFORMANCE_TRACKING = _cfg["strategy_performance_tracking"]
CONSECUTIVE_LOSS_LIMIT = _cfg["consecutive_loss_limit"]
STRATEGY_COOLDOWN_CYCLES = _cfg["strategy_cooldown_cycles"]

# ---- Correlation ----
CORRELATION_ENABLED = _cfg["correlation_enabled"]
CORRELATION_THRESHOLD = _cfg["correlation_threshold"]
CORRELATION_LOOKBACK = _cfg["correlation_lookback"]
CORRELATION_CACHE_TTL = _cfg.get("correlation_cache_ttl", 3600)

# ---- Backtesting ----
BACKTEST_ENABLED = _cfg.get("backtest_enabled", False)
BACKTEST_DAYS = _cfg.get("backtest_days", 30)
BACKTEST_INTERVAL = _cfg.get("backtest_interval", "1h")
# Түүхэн лаа/funding-ыг үргэлж production-оос авна: demo/testnet эндпойнт нь
# бодит бус эсвэл дутуу түүхтэй байдаг тул backtest утгагүй болно. Эдгээр нь
# нээлттэй өгөгдөл — API түлхүүр шаардахгүй, арилжаа нь BASE_URL дээрээ хэвээр.
BACKTEST_DATA_URL = _cfg.get("backtest_data_url", "https://fapi.binance.com")
BACKTEST_FEE_RATE = _cfg.get("backtest_fee_rate", 0.0004)
BACKTEST_SLIPPAGE_RATE = _cfg.get("backtest_slippage_rate", 0.0002)
# ---- Арилжааны бүртгэл (CSV) ----
JOURNAL_ENABLED = _cfg.get("journal_enabled", False)
JOURNAL_FILE = os.path.join(STATE_DIR, _cfg.get("journal_file", "trades.csv"))

STRATEGY_STATE_FILE = os.path.join(STATE_DIR, _cfg.get("strategy_state_file", "strategy_state.json"))
SESSION_STATE_FILE = os.path.join(STATE_DIR, _cfg.get("session_state_file", "session_state.json"))

# ---- ШИНЭ: CHOP, Supertrend, MTF, VWAP, Funding Rate ----
CHOP_PERIOD = _cfg.get("chop_period", 14)
SUPERTREND_PERIOD = _cfg.get("supertrend_period", 10)
SUPERTREND_MULTIPLIER = _cfg.get("supertrend_multiplier", 3)
MTF_ENABLED = _cfg.get("mtf_enabled", True)
VWAP_ENABLED = _cfg.get("vwap_enabled", True)
FUNDING_ENABLED = _cfg.get("funding_enabled", True)
BREAKOUT_VOLUME_RATIO = _cfg.get("breakout_volume_ratio", 1.5)
MACD_MIN_VOLUME_RATIO = _cfg.get("macd_min_volume_ratio", 1.0)

# ---- TREND_FOLLOWING: өндөр давтамжийн (4h) макро тренд ----
# slope босго 1h-ийн 0.5%-аас өндөр: 5 барын налуу 4h дээр 20 цагийг хамардаг
# (1h дээр 5 цаг) тул байгалиасаа ~3 дахин том утга гардаг.
TREND_HTF_FACTOR = _cfg.get("trend_htf_factor", 4)
TREND_HTF_FAST_EMA = _cfg.get("trend_htf_fast_ema", 20)
TREND_HTF_MID_EMA = _cfg.get("trend_htf_mid_ema", 50)
TREND_HTF_SLOW_EMA = _cfg.get("trend_htf_slow_ema", 100)
TREND_HTF_MIN_ADX = _cfg.get("trend_htf_min_adx", 30)
TREND_HTF_MIN_SLOPE = _cfg.get("trend_htf_min_slope", 1.0)

# ---- ШИНЭ: News Trading ----
NEWS_TRADING = _cfg.get("news_trading", {})
NEWS_ENABLED = NEWS_TRADING.get("enabled", False)
# Эвентийн өмнөх түр зогсоолт (NEWS_ENABLED) ба эвентийн дараах арилжаа хоёр
# тусдаа флаг. Зогсоолт нь эрсдэл бууруулдаг тул дангаараа асахад утгатай;
# дараах арилжаа нь хөдөлгөөн рүү нь үсрэх бөгөөд батлагдаагүй тул анхдагчаар унтарсан.
NEWS_POST_TRADE_ENABLED = NEWS_TRADING.get("post_news_trade", False)
NEWS_EVENT_KEYWORDS = NEWS_TRADING.get("event_keywords", ["CPI", "FOMC"])
NEWS_CALENDAR_URL = NEWS_TRADING.get("calendar_url", "")
NEWS_PAUSE_BEFORE = NEWS_TRADING.get("pause_before_minutes", 30)
NEWS_WAIT_AFTER = NEWS_TRADING.get("wait_after_minutes", 15)
NEWS_MAX_POSITIONS = NEWS_TRADING.get("max_positions", 1)
NEWS_LEVERAGE = NEWS_TRADING.get("leverage", 2)
NEWS_ALLOCATION = NEWS_TRADING.get("allocation", 0.05)
NEWS_TP_PCT = NEWS_TRADING.get("tp_pct", 3.0)
NEWS_SL_PCT = NEWS_TRADING.get("sl_pct", 1.0)
NEWS_MIN_MOVE = NEWS_TRADING.get("min_move_pct", 0.5)
NEWS_SYMBOLS = NEWS_TRADING.get("symbols", ["BTCUSDT"])


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
            ".env дотор дараах утга дутуу байна: " + ", ".join(missing)
        )
