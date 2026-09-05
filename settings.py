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

TRAILING_CALLBACK_RATE = _cfg["trailing_callback_rate"]
TRAILING_ACTIVATION_PCT = _cfg["trailing_activation_pct"]
TAKE_PROFIT_PCT = _cfg["take_profit_pct"]
EMERGENCY_SL_PCT = _cfg["emergency_sl_pct"]

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
BACKTEST_FEE_RATE = _cfg.get("backtest_fee_rate", 0.0004)
BACKTEST_SLIPPAGE_RATE = _cfg.get("backtest_slippage_rate", 0.0002)
STRATEGY_STATE_FILE = os.path.join(STATE_DIR, _cfg.get("strategy_state_file", "strategy_state.json"))
SESSION_STATE_FILE = os.path.join(STATE_DIR, _cfg.get("session_state_file", "session_state.json"))

# ---- ШИНЭ: CHOP, Supertrend, MTF, VWAP, Funding Rate ----
CHOP_PERIOD = _cfg.get("chop_period", 14)
SUPERTREND_PERIOD = _cfg.get("supertrend_period", 10)
SUPERTREND_MULTIPLIER = _cfg.get("supertrend_multiplier", 3)
MTF_ENABLED = _cfg.get("mtf_enabled", True)
VWAP_ENABLED = _cfg.get("vwap_enabled", True)
FUNDING_ENABLED = _cfg.get("funding_enabled", True)

# ---- ШИНЭ: News Trading ----
NEWS_TRADING = _cfg.get("news_trading", {})
NEWS_ENABLED = NEWS_TRADING.get("enabled", False)
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
