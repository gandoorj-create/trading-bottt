"""
state.py
Ботын ажиллаж байх үед өөрчлөгддөг бүх утга нэг дор.

Яагаад тусад нь файл болгосон бэ:
Өмнө нь эдгээр утгууд bot.py дотор module-level global байсан бөгөөд 16 өөр
функц `global safety_lock` гэж зарлаад өөрчилдөг байв. Кодыг олон модуль болгож
хуваахад `from state import safety_lock` гээд `safety_lock = True` бичих нь
зөвхөн тухайн модулийн локал нэрийг л сольдог тул бусад модуль хуучин утгыг
хараад үлддэг — арилжаа зогсох ёстой газраа зогсохгүй байх аюултай алдаа.

Тиймээс бүх утгыг ганц `state` объектын атрибут болгов. `state.safety_lock = True`
гэж бичихэд ижил объектыг харж буй бүх модульд шууд тусна.
"""
import time

STRATEGY_NAMES = [
    "SUPERTREND",
    "MACD_MOMENTUM",
    "GRID_TRADING",
    "BOLLINGER_MEAN_REVERSION",
    "RSI_STRATEGY",
    "TREND_FOLLOWING",
]


def new_strategy_stats():
    return {
        strategy: {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "consecutive_losses": 0,
            "active": True,
            "paused_cycles": 0,
        }
        for strategy in STRATEGY_NAMES
    }


class BotState:
    """Ботын runtime state. Тестэд `state.reset()` дуудахад цэвэр болно."""

    def __init__(self):
        self.reset()

    def reset(self):
        # ---- Стратегийн гүйцэтгэл ----
        self.strategy_stats = new_strategy_stats()

        # ---- Session / cycle баланс ----
        self.session_start_balance = 0.0
        self.session_realized_pnl = 0.0
        self.session_peak_balance = 0.0
        self.cycle_start_balance = 0.0
        self.cycle_start_time = time.time()
        self.last_cycle_balance = 0.0

        # ---- Эрсдэлийн түгжээ ----
        self.safety_lock = False
        self.drawdown_lock_active = False
        self.drawdown_halt = False
        self.unprotected_symbols = set()

        # ---- Нээлттэй арилжаа ----
        self.active_trade_info = {}

        # ---- Cache ----
        self.leverage_cache = {}
        self.symbol_info_cache = {}
        self.correlation_cache = {}
        self.correlation_cache_time = {}
        self.position_mode_cache = None
        self.server_time_offset_ms = 0
        self.last_telegram_report_time = 0
        # Algo (conditional) захиалга жагсаах endpoint: None = хараахан хайгаагүй,
        # "" = хайсан боловч ажиллах хувилбар олдсонгүй
        self.algo_list_endpoint = None

        # ---- News trading ----
        self.news_mode_active = False
        self.news_trade_done = False
        self.last_news_check = None
        self.next_news_time = None


# Бүх модуль ижил объектыг хуваалцана
state = BotState()
