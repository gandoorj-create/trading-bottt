"""
settings.py
Нууц зүйлийг .env-ээс, тохиргоог config.json-оос уншина.
Хэрэглэх: from settings import *  эсвэл  import settings
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()  # .env файлыг уншиж os.environ-д ачаална

# ---- Нууц (secrets) ----
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://demo-fapi.binance.com")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API_ROOT = os.environ.get("TELEGRAM_API_ROOT", "https://api.telegram.org")

# ---- Тохиргоо (config.json) ----
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
    _cfg = json.load(f)

SYMBOLS_POOL = _cfg["symbols_pool"]

SELECTION_INTERVAL_MINUTES = _cfg["selection_interval_minutes"]
MONITOR_INTERVAL_SEC = _cfg["monitor_interval_sec"]
TELEGRAM_REPORT_INTERVAL_SEC = _cfg["telegram_report_interval_sec"]
MAX_SELECTIONS = _cfg["max_selections"]

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
REQUEST_TIMEOUT = _cfg["request_timeout"]
PNL_LOOKBACK_LIMIT = _cfg["pnl_lookback_limit"]

ADAPTIVE_STRATEGY = _cfg["adaptive_strategy"]
STRATEGY_PERFORMANCE_TRACKING = _cfg["strategy_performance_tracking"]
CONSECUTIVE_LOSS_LIMIT = _cfg["consecutive_loss_limit"]
STRATEGY_COOLDOWN_CYCLES = _cfg["strategy_cooldown_cycles"]

DCA_ENABLED = _cfg["dca_enabled"]
DCA_LEVELS = _cfg["dca_levels"]
DCA_TRIGGER_PCT = _cfg["dca_trigger_pct"]
DCA_MULTIPLIER = _cfg["dca_multiplier"]

CORRELATION_ENABLED = _cfg["correlation_enabled"]
CORRELATION_THRESHOLD = _cfg["correlation_threshold"]
CORRELATION_LOOKBACK = _cfg["correlation_lookback"]

BACKTEST_ENABLED = _cfg["backtest_enabled"]
BACKTEST_DAYS = _cfg["backtest_days"]
BACKTEST_INTERVAL = _cfg["backtest_interval"]


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
