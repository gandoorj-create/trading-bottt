"""
Тестийн нийтлэг fixture-үүд.

Гол зорилго: тест ажиллах үед ямар ч тохиолдолд жинхэнэ Binance/Telegram руу
хүсэлт явахгүй, репод байгаа state файлууд бохирдохгүй байх.
"""
import pytest


import account
import backtest
import binance_client
import execution
import indicators
import market_data
import news
import notifications
import order_api
import persistence
import position_manager
import reports
import risk
import screening
import strategies
import utils
from state import state as bot_state


# Тохиргооны тогтмолууд `from settings import *`-аар модуль бүрд хуулбарлагддаг тул
# нэгийг нь солиход бусад нь хуучин утгаараа үлддэг. Тиймээс тухайн нэрийг агуулсан
# бүх модульд нэгэн зэрэг солино.
_SETTING_MODULES = (
    account, backtest, binance_client, execution, indicators, market_data, news,
    notifications, order_api, persistence, position_manager, reports, risk,
    screening, strategies, utils,
)


def patch_setting(monkeypatch, name, value):
    """Тохиргооны тогтмолыг ашиглаж буй бүх модульд солино."""
    touched = 0
    for module in _SETTING_MODULES:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            touched += 1
    assert touched, f"{name} аль ч модульд олдсонгүй"


class _BlockedNetwork:
    """Аль ч тест санамсаргүйгээр сүлжээ рүү гарвал шууд унана."""

    @staticmethod
    def _blocked(*args, **kwargs):
        raise AssertionError(
            "Тест сүлжээ рүү хандах гэж оролдлоо — mock хийх шаардлагатай"
        )

    get = _blocked
    post = _blocked
    put = _blocked
    delete = _blocked
    request = _blocked


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # requests-ийг импортолсон модуль бүрд блоклоно
    for module in (binance_client, notifications, news):
        monkeypatch.setattr(module, "requests", _BlockedNetwork)


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Telegram илгээлтийг барьж аваад дуудлагыг нь жагсаана."""
    sent = []
    monkeypatch.setattr(notifications, "send_telegram", lambda text, pin=False: sent.append(text))
    monkeypatch.setattr(notifications, "send_telegram_photo", lambda photo_bytes, caption="": sent.append(caption))
    return sent


@pytest.fixture
def telegram_messages(no_telegram):
    """no_telegram-ийн барьсан мессежүүдийг тестэд ил гаргана."""
    return no_telegram


@pytest.fixture(autouse=True)
def isolated_state_files(monkeypatch, tmp_path):
    """State файлууд tmp директорт бичигдэнэ — репо доторх файл хөндөгдөхгүй."""
    monkeypatch.setattr(persistence, "STRATEGY_STATE_FILE", str(tmp_path / "strategy_state.json"))
    monkeypatch.setattr(persistence, "SESSION_STATE_FILE", str(tmp_path / "session_state.json"))
    return tmp_path


@pytest.fixture(autouse=True)
def clean_state():
    """Runtime state-ийг тест бүрийн өмнө болон дараа цэвэр байдалд буцаана."""
    bot_state.reset()
    yield bot_state
    bot_state.reset()


@pytest.fixture
def fake_symbol_info(monkeypatch):
    """Exchange info-г мок болгоно (жинхэнэ /exchangeInfo дуудалгүйгээр)."""
    info = {
        "BTCUSDT": {
            "stepSize": 0.001,
            "tickSize": 0.1,
            "minQty": 0.001,
            "minNotional": 100.0,
            "quantityPrecision": 3,
            "pricePrecision": 1,
        },
        "DOGEUSDT": {
            "stepSize": 1.0,
            "tickSize": 0.00001,
            "minQty": 1.0,
            "minNotional": 5.0,
            "quantityPrecision": 0,
            "pricePrecision": 5,
        },
    }
    bot_state.symbol_info_cache = info
    monkeypatch.setattr(market_data, "load_exchange_info", lambda: None)
    return info
