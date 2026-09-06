"""
test_sports_bot.py
Спортын Value Bet Scanner-ийн тест: de-vig математик, Kelly stake,
handicap line таарах шаардлага, EV босго, давхардал таслах dedupe.
"""
import pytest

import devig
import kelly
import notifications
import odds_api
import settings
import sports_bot
import sports_journal
import sports_state
import value_scanner


# conftest.py-ийн autouse no_telegram fixture нь notifications.send_telegram-ийг
# бүрмөсөн mock болгодог тул жинхэнэ token-routing логикыг шалгахын тулд энэ
# файл import хийгдэх (fixture ажиллахаас өмнөх) үеийн жинхэнэ функцийг хадгална.
_real_send_telegram = notifications.send_telegram


@pytest.fixture(autouse=True)
def isolated_sports_files(monkeypatch, tmp_path):
    """Sports state/journal файлууд tmp директорт бичигдэнэ."""
    monkeypatch.setattr(sports_state, "SPORTS_SEEN_FILE", str(tmp_path / "sports_seen.json"))
    monkeypatch.setattr(sports_journal, "SPORTS_JOURNAL_FILE", str(tmp_path / "sports_bets.csv"))
    monkeypatch.setattr(sports_journal, "SPORTS_JOURNAL_ENABLED", True)


# ---------------------------------------------------------------------------
# De-vig
# ---------------------------------------------------------------------------

def test_devig_two_way_removes_vig_symmetric():
    # -110/-110 америк ханш ≈ 1.909 decimal хоёул тал — тэгш overround.
    probs = devig.devig_multiplicative([1.909, 1.909])
    assert probs == pytest.approx([0.5, 0.5], abs=1e-3)
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)


def test_devig_three_way_soccer_sums_to_one():
    probs = devig.devig_multiplicative([2.0, 3.5, 4.0])
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    # Хамгийн бага ханш (хамгийн их магадлал) хожих тал хамгийн өндөр prob-той байх ёстой
    assert probs[0] > probs[1] > probs[2]


def test_devig_invalid_odds_returns_none():
    assert devig.devig_multiplicative([1.909, 0]) is None
    assert devig.devig_multiplicative([1.909, None]) is None


# ---------------------------------------------------------------------------
# Kelly
# ---------------------------------------------------------------------------

def test_kelly_quarter_stake_within_cap():
    # true_prob=0.55, odds=2.0 → full Kelly f*=0.10 → 1/4 Kelly=2.5%
    pct, amount = kelly.stake_amount(1000.0, 0.55, 2.0, kelly_multiplier=0.25, max_pct=0.03)
    assert pct == pytest.approx(0.025, abs=1e-6)
    assert amount == pytest.approx(25.0, abs=1e-6)


def test_kelly_caps_at_max_pct():
    # Том edge-тэй тохиолдолд ч max_pct-аас хэтрэхгүй
    pct, amount = kelly.stake_amount(1000.0, 0.9, 3.0, kelly_multiplier=0.25, max_pct=0.03)
    assert pct == pytest.approx(0.03, abs=1e-9)
    assert amount == pytest.approx(30.0, abs=1e-9)


def test_kelly_negative_edge_returns_zero_stake():
    # true_prob=0.4 харин ханш 2.0 (implied 50%) — сөрөг edge
    pct, amount = kelly.stake_amount(1000.0, 0.4, 2.0)
    assert pct == 0.0
    assert amount == 0.0


# ---------------------------------------------------------------------------
# value_scanner: h2h
# ---------------------------------------------------------------------------

def _event_h2h(pinnacle_odds, other_book_odds, book_key="fanduel"):
    return {
        "id": "evt1",
        "commence_time": "2026-09-07T00:00:00Z",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "bookmakers": [
            {
                "key": "pinnacle", "title": "Pinnacle",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Lakers", "price": pinnacle_odds[0]},
                    {"name": "Celtics", "price": pinnacle_odds[1]},
                ]}],
            },
            {
                "key": book_key, "title": book_key.title(),
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Lakers", "price": other_book_odds[0]},
                    {"name": "Celtics", "price": other_book_odds[1]},
                ]}],
            },
        ],
    }


def test_scan_event_h2h_flags_value_bet_above_threshold():
    # Pinnacle: Lakers true prob ~0.5238 (fair odds ~1.909). Fanduel Lakers @ 2.10 → EV = 0.5238*2.10-1 ≈ +9.9%
    event = _event_h2h(pinnacle_odds=[1.909, 1.909], other_book_odds=[2.10, 1.80])
    bets = value_scanner.scan_event(event, "NBA", min_edge_pct=3.0)
    lakers_bets = [b for b in bets if b["selection"] == "Lakers"]
    assert len(lakers_bets) == 1
    assert lakers_bets[0]["ev_pct"] > 3.0
    assert lakers_bets[0]["book"] == "Fanduel"


def test_scan_event_h2h_filters_below_min_edge():
    # Fanduel-ийн ханш Pinnacle-тай бараг тэнцүү → edge босгыг давахгүй
    event = _event_h2h(pinnacle_odds=[1.909, 1.909], other_book_odds=[1.92, 1.90])
    bets = value_scanner.scan_event(event, "NBA", min_edge_pct=3.0)
    assert bets == []


# ---------------------------------------------------------------------------
# value_scanner: spreads (handicap line яг таарах шаардлага)
# ---------------------------------------------------------------------------

def _event_spreads(pinnacle_point, other_point, pinnacle_odds=(1.909, 1.909), other_odds=(2.10, 1.80)):
    return {
        "id": "evt2",
        "commence_time": "2026-09-07T00:00:00Z",
        "home_team": "Chiefs",
        "away_team": "Bills",
        "bookmakers": [
            {
                "key": "pinnacle", "title": "Pinnacle",
                "markets": [{"key": "spreads", "outcomes": [
                    {"name": "Chiefs", "price": pinnacle_odds[0], "point": -pinnacle_point},
                    {"name": "Bills", "price": pinnacle_odds[1], "point": pinnacle_point},
                ]}],
            },
            {
                "key": "draftkings", "title": "DraftKings",
                "markets": [{"key": "spreads", "outcomes": [
                    {"name": "Chiefs", "price": other_odds[0], "point": -other_point},
                    {"name": "Bills", "price": other_odds[1], "point": other_point},
                ]}],
            },
        ],
    }


def test_scan_event_spreads_matches_exact_line():
    event = _event_spreads(pinnacle_point=5.5, other_point=5.5)
    bets = value_scanner.scan_event(event, "NFL", min_edge_pct=3.0)
    chiefs_bets = [b for b in bets if b["selection"] == "Chiefs"]
    assert len(chiefs_bets) == 1
    assert chiefs_bets[0]["point"] == -5.5


def test_scan_event_spreads_rejects_mismatched_line():
    # DraftKings-ийн line өөр (-6 vs -5.5) — таарахгүй тул edge тооцохгүй
    event = _event_spreads(pinnacle_point=5.5, other_point=6.0)
    bets = value_scanner.scan_event(event, "NFL", min_edge_pct=3.0)
    assert bets == []


# ---------------------------------------------------------------------------
# Dedupe key
# ---------------------------------------------------------------------------

def test_make_key_stable_and_distinguishes_line():
    event_a = _event_spreads(pinnacle_point=5.5, other_point=5.5)
    event_b = _event_spreads(pinnacle_point=6.0, other_point=6.0)
    bets_a = value_scanner.scan_event(event_a, "NFL", min_edge_pct=3.0)
    bets_b = value_scanner.scan_event(event_b, "NFL", min_edge_pct=3.0)
    key_a = value_scanner.make_key(bets_a[0])
    key_b = value_scanner.make_key(bets_b[0])
    assert key_a != key_b
    # Ижил bet-ийг дахин scan хийхэд key өөрчлөгдөхгүй
    assert value_scanner.make_key(bets_a[0]) == key_a


def test_run_scan_cycle_skips_already_seen_bet(monkeypatch, telegram_messages):
    event = _event_h2h(pinnacle_odds=[1.909, 1.909], other_book_odds=[2.10, 1.80])
    bets = value_scanner.scan_event(event, "NBA", min_edge_pct=3.0)
    assert bets

    monkeypatch.setattr(sports_bot, "SPORTS_LIST", ["basketball_nba"])
    monkeypatch.setattr(sports_bot, "scan_sport", lambda *a, **k: list(bets))

    sports_bot.run_scan_cycle()
    assert len(telegram_messages) == 1

    # Хоёр дахь цикл ижил bet-ийг дахин олоод ч давхардсан key-г алгасна
    sports_bot.run_scan_cycle()
    assert len(telegram_messages) == 1


# ---------------------------------------------------------------------------
# odds_api
# ---------------------------------------------------------------------------

def test_fetch_odds_raises_without_api_key(monkeypatch):
    monkeypatch.setattr(odds_api, "ODDS_API_KEY", None)
    with pytest.raises(odds_api.OddsAPIError):
        odds_api.fetch_odds("basketball_nba", ["eu"], ["h2h"])


def test_fetch_odds_parses_successful_response(monkeypatch):
    monkeypatch.setattr(odds_api, "ODDS_API_KEY", "fake-key")

    class FakeResponse:
        status_code = 200
        headers = {"x-requests-remaining": "499"}

        @staticmethod
        def json():
            return [{"id": "evt1", "bookmakers": []}]

    class FakeRequests:
        @staticmethod
        def get(url, params=None, timeout=None):
            assert "basketball_nba" in url
            assert params["apiKey"] == "fake-key"
            return FakeResponse()

    monkeypatch.setattr(odds_api, "requests", FakeRequests)
    data = odds_api.fetch_odds("basketball_nba", ["eu"], ["h2h", "spreads"])
    assert data == [{"id": "evt1", "bookmakers": []}]


def test_fetch_odds_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(odds_api, "ODDS_API_KEY", "fake-key")

    class FakeResponse:
        status_code = 401
        headers = {}
        text = "Unauthorized"

    class FakeRequests:
        @staticmethod
        def get(url, params=None, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(odds_api, "requests", FakeRequests)
    with pytest.raises(odds_api.OddsAPIError):
        odds_api.fetch_odds("basketball_nba", ["eu"], ["h2h"])


# ---------------------------------------------------------------------------
# Тусдаа Telegram bot (crypto ботоос ялгаатай)
# ---------------------------------------------------------------------------

def test_send_telegram_uses_explicit_sports_token(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None):
            captured["url"] = url
            captured["chat_id"] = json["chat_id"]
            return FakeResponse()

    monkeypatch.setattr(notifications, "requests", FakeRequests)
    monkeypatch.setattr(notifications, "BOT_TOKEN", "crypto-token")
    monkeypatch.setattr(notifications, "CHAT_ID", "crypto-chat")

    ok = _real_send_telegram("hello", bot_token="sports-token", chat_id="sports-chat")
    assert ok is True
    assert "sports-token" in captured["url"]
    assert captured["chat_id"] == "sports-chat"


def test_send_telegram_falls_back_to_crypto_token_when_not_given(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(notifications, "requests", FakeRequests)
    monkeypatch.setattr(notifications, "BOT_TOKEN", "crypto-token")
    monkeypatch.setattr(notifications, "CHAT_ID", "crypto-chat")

    _real_send_telegram("hello")
    assert "crypto-token" in captured["url"]


def test_validate_sports_config_requires_sports_bot_token(monkeypatch):
    monkeypatch.setattr(settings, "ODDS_API_KEY", "k")
    monkeypatch.setattr(settings, "SPORTS_BOT_TOKEN", None)
    monkeypatch.setattr(settings, "SPORTS_CHAT_ID", "c")
    with pytest.raises(RuntimeError, match="SPORTS_TELEGRAM_BOT_TOKEN"):
        settings.validate_sports_config()
