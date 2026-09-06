"""
value_scanner.py
The Odds API-с татсан ханшийг Pinnacle-тэй харьцуулж, vig цэвэрлээд бусад
bookmaker дээрх edge (EV%)-ийг тооцно.

Зөвхөн h2h (хэн хожих) болон spreads (гандикап) зах зээл. Props-ийг
одоохондоо орхисон. Гандикап дээр Pinnacle-ийн line өөр bookmaker-ийн
яг ижил line-тай (жишээ нь -5.5 нь -5.5-тай) тохирч байж л EV тооцно —
эс тэгвэл өөр тохиолдлын магадлалыг өөр тохиолдлын ханштай харьцуулсан
хуурмаг edge гарна.
"""
from odds_api import fetch_odds, OddsAPIError
from devig import devig_multiplicative
from logging_setup import get_logger

log = get_logger(__name__)

PINNACLE_KEY = "pinnacle"
MARKETS = ("h2h", "spreads")


def _pinnacle_book(event):
    for book in event.get("bookmakers", []):
        if book.get("key") == PINNACLE_KEY:
            return book
    return None


def _market(book, market_key):
    for m in book.get("markets", []):
        if m.get("key") == market_key:
            return m
    return None


def _true_probs_h2h(pinnacle_market):
    outcomes = pinnacle_market.get("outcomes", [])
    odds = [o.get("price") for o in outcomes]
    probs = devig_multiplicative(odds)
    if probs is None:
        return None
    return {o["name"]: p for o, p in zip(outcomes, probs)}


def _true_probs_spreads(pinnacle_market):
    """Гандикапын хос тал (нэг л line) хамтдаа нэг 2 талт зах зээл гэж de-vig хийнэ."""
    outcomes = pinnacle_market.get("outcomes", [])
    if len(outcomes) != 2:
        return None
    odds = [o.get("price") for o in outcomes]
    probs = devig_multiplicative(odds)
    if probs is None:
        return None
    return {(o["name"], o.get("point")): p for o, p in zip(outcomes, probs)}


def _make_bet(sport_label, event_id, matchup, commence, market, name, point, book, price, true_p):
    ev_pct = (true_p * price - 1.0) * 100
    return {
        "sport": sport_label,
        "event_id": event_id,
        "matchup": matchup,
        "commence_time": commence,
        "market": market,
        "selection": name,
        "point": point,
        "book": book.get("title", book.get("key")),
        "book_key": book.get("key"),
        "odds": price,
        "pinnacle_true_prob": true_p,
        "pinnacle_fair_odds": round(1.0 / true_p, 3) if true_p else None,
        "ev_pct": round(ev_pct, 2),
    }


def scan_event(event, sport_label, min_edge_pct):
    """Нэг event-ийн бүх bookmaker-ийг Pinnacle-тэй харьцуулж value bet-үүдийг буцаана."""
    results = []
    pinnacle = _pinnacle_book(event)
    if not pinnacle:
        return results

    event_id = event.get("id")
    commence = event.get("commence_time")
    matchup = f"{event.get('away_team', '?')} @ {event.get('home_team', '?')}"

    pin_h2h = _market(pinnacle, "h2h")
    pin_spreads = _market(pinnacle, "spreads")
    true_h2h = _true_probs_h2h(pin_h2h) if pin_h2h else None
    true_spreads = _true_probs_spreads(pin_spreads) if pin_spreads else None

    for book in event.get("bookmakers", []):
        if book.get("key") == PINNACLE_KEY:
            continue

        if true_h2h:
            m = _market(book, "h2h")
            if m:
                for outcome in m.get("outcomes", []):
                    name = outcome.get("name")
                    price = outcome.get("price")
                    true_p = true_h2h.get(name)
                    if true_p is None or not price or price <= 1.0:
                        continue
                    bet = _make_bet(sport_label, event_id, matchup, commence, "h2h", name, None, book, price, true_p)
                    if bet["ev_pct"] >= min_edge_pct:
                        results.append(bet)

        if true_spreads:
            m = _market(book, "spreads")
            if m:
                for outcome in m.get("outcomes", []):
                    name = outcome.get("name")
                    point = outcome.get("point")
                    price = outcome.get("price")
                    true_p = true_spreads.get((name, point))
                    if true_p is None or not price or price <= 1.0:
                        continue  # line яг таарахгүй бол алгасна
                    bet = _make_bet(sport_label, event_id, matchup, commence, "spreads", name, point, book, price, true_p)
                    if bet["ev_pct"] >= min_edge_pct:
                        results.append(bet)

    return results


def make_key(bet):
    """event+market+line+book — давхардал таслах түлхүүр."""
    return f"{bet['event_id']}:{bet['market']}:{bet['point']}:{bet['book_key']}:{bet['selection']}"


def scan_sport(sport_key, sport_label, regions, min_edge_pct):
    try:
        events = fetch_odds(sport_key, regions, MARKETS)
    except OddsAPIError as e:
        log.warning(f"⚠️ {sport_label} ханш татахад алдаа: {e}")
        return []
    all_bets = []
    for event in events:
        all_bets.extend(scan_event(event, sport_label, min_edge_pct))
    return all_bets
