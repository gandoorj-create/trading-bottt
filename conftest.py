"""
Тестийн нийтлэг fixture-үүд.

Гол зорилго: тест ажиллах үед ямар ч тохиолдолд жинхэнэ Binance/Telegram руу
хүсэлт явахгүй, репод байгаа state файлууд бохирдохгүй байх.
"""
import pytest

import bot


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
    monkeypatch.setattr(bot, "requests", _BlockedNetwork)


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Telegram илгээлтийг барьж аваад дуудлагыг нь жагсаана."""
    sent = []
    monkeypatch.setattr(bot, "send_telegram", lambda text, pin=False: sent.append(text))
    monkeypatch.setattr(bot, "send_telegram_photo", lambda photo_bytes, caption="": sent.append(caption))
    return sent


@pytest.fixture
def telegram_messages(no_telegram):
    """no_telegram-ийн барьсан мессежүүдийг тестэд ил гаргана."""
    return no_telegram


@pytest.fixture(autouse=True)
def isolated_state_files(monkeypatch, tmp_path):
    """State файлууд tmp директорт бичигдэнэ — репо доторх файл хөндөгдөхгүй."""
    monkeypatch.setattr(bot, "STRATEGY_STATE_FILE", str(tmp_path / "strategy_state.json"))
    monkeypatch.setattr(bot, "SESSION_STATE_FILE", str(tmp_path / "session_state.json"))
    return tmp_path


@pytest.fixture(autouse=True)
def clean_state():
    """Runtime state-ийг тест бүрийн өмнө болон дараа цэвэр байдалд буцаана."""
    bot.state.reset()
    yield bot.state
    bot.state.reset()


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
    bot.state.symbol_info_cache = info
    monkeypatch.setattr(bot, "load_exchange_info", lambda: None)
    return info
