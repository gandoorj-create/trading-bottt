"""
sports_config.py
Спортын Value Bet Scanner-ийн бие даасан тохиргоо.

Санаатайгаар crypto ботын settings.py/config.json-той ЯМАР Ч ХАМААРАЛГҮЙ —
энэ folder дангаараа бие даасан скрипт, өөрийн .env утга, өөрийн JSON
тохиргоотой. Ганц зүйл хуваалцдаг нь repo-гийн үндсэн `.env` файл (өөр өөр
ПРЕФИКСтэй key-үүд тул зөрчилдөхгүй).
"""
import os
import json
from dotenv import load_dotenv

load_dotenv()

_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- Нууц (.env) ----
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_BASE_URL = os.environ.get("ODDS_API_BASE_URL", "https://api.the-odds-api.com")

SPORTS_BOT_TOKEN = os.environ.get("SPORTS_TELEGRAM_BOT_TOKEN")
SPORTS_CHAT_ID = os.environ.get("SPORTS_TELEGRAM_CHAT_ID")
TELEGRAM_API_ROOT = os.environ.get("TELEGRAM_API_ROOT", "https://api.telegram.org")

# Crypto ботын STATE_DIR-тэй огт өөр env var — хоёр бот нэг сервер дээр зэрэг
# ажиллах үед ч file/лог давхцахгүй байх зорилготой.
STATE_DIR = os.environ.get("SPORTS_STATE_DIR") or _DIR

# ---- Тохиргоо (sports_config.json) ----
with open(os.path.join(_DIR, "sports_config.json"), "r", encoding="utf-8") as f:
    _cfg = json.load(f)

SPORTS_LIST = _cfg["sports"]
SPORTS_REGIONS = _cfg["regions"]
SPORTS_SCAN_INTERVAL_MINUTES = _cfg["scan_interval_minutes"]
SPORTS_MIN_EDGE_PCT = _cfg["min_edge_pct"]
SPORTS_TOP_N = _cfg["top_n"]
SPORTS_BANKROLL_USD = _cfg["bankroll_usd"]
SPORTS_KELLY_MULTIPLIER = _cfg["kelly_multiplier"]
SPORTS_MAX_STAKE_PCT = _cfg["max_stake_pct"] / 100.0
SPORTS_SEEN_TTL_HOURS = _cfg["seen_ttl_hours"]
SPORTS_JOURNAL_ENABLED = _cfg["journal_enabled"]
SPORTS_JOURNAL_FILE = os.path.join(STATE_DIR, _cfg["journal_file"])
SPORTS_SEEN_FILE = os.path.join(STATE_DIR, _cfg["seen_file"])
REQUEST_TIMEOUT = _cfg["request_timeout"]


def validate_sports_config():
    missing = []
    if not ODDS_API_KEY:
        missing.append("ODDS_API_KEY")
    if not SPORTS_BOT_TOKEN:
        missing.append("SPORTS_TELEGRAM_BOT_TOKEN")
    if not SPORTS_CHAT_ID:
        missing.append("SPORTS_TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(
            ".env дотор дараах утга дутуу байна: " + ", ".join(missing)
        )
