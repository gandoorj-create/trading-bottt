"""
sports_bot.py
Спортын Value Bet Scanner — оруулах цэг.

NBA/NFL/MLB/хөлбөмбөгийн h2h (хэн хожих) болон handicap (spreads) ханшийг
The Odds API-с татаж, Pinnacle-ийн ханшнаас vig цэвэрлээд бодит магадлал
гаргана. Бусад bookmaker дээр min_edge_pct-аас дээш edge гарвал value bet
гэж тэмдэглэж, EV-ээр эрэмбэлсэн топ N-ийг Telegram руу явуулна.

Бооцоог АВТОМАТААР ТАВИХГҮЙ — зөвхөн санал болгоно, чи гараар тавина.
"""
import time
import traceback
from datetime import datetime

import kelly
import notifications
import sports_journal
import sports_state
from settings import (
    STATE_DIR, STATE_DIR_IS_PERSISTENT,
    SPORTS_LIST, SPORTS_REGIONS, SPORTS_SCAN_INTERVAL_MINUTES, SPORTS_MIN_EDGE_PCT,
    SPORTS_TOP_N, SPORTS_BANKROLL_USD, SPORTS_KELLY_MULTIPLIER, SPORTS_MAX_STAKE_PCT,
    SPORTS_BOT_TOKEN, SPORTS_CHAT_ID,
    validate_sports_config,
)
from value_scanner import scan_sport, make_key
from logging_setup import get_logger, setup_logging

log = get_logger(__name__)

SPORT_LABELS = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "baseball_mlb": "MLB",
}


def _sport_label(sport_key):
    if sport_key in SPORT_LABELS:
        return SPORT_LABELS[sport_key]
    if sport_key.startswith("soccer_"):
        return "FOOTBALL/" + sport_key[len("soccer_"):].replace("_", " ").upper()
    return sport_key


def _format_bet_line(rank, bet, kelly_pct, stake):
    point_str = f" {bet['point']:+g}" if bet.get("point") is not None else ""
    return (
        f"{rank}. [{bet['sport']}] {bet['matchup']}\n"
        f"   {bet['market'].upper()}: {bet['selection']}{point_str} @ {bet['book']} — ханш {bet['odds']}\n"
        f"   Pinnacle fair: {bet['pinnacle_fair_odds']} | Edge: +{bet['ev_pct']:.2f}%\n"
        f"   Stake: bankroll-ийн {kelly_pct * 100:.2f}% (~${stake:,.2f}, 1/4 Kelly)"
    )


def run_scan_cycle():
    seen = sports_state.load_seen()
    all_bets = []
    for sport_key in SPORTS_LIST:
        label = _sport_label(sport_key)
        try:
            bets = scan_sport(sport_key, label, SPORTS_REGIONS, SPORTS_MIN_EDGE_PCT)
        except Exception as e:
            log.warning(f"⚠️ {label} скан алдаа: {e}")
            continue
        all_bets.extend(bets)

    now = time.time()
    fresh = [(make_key(bet), bet) for bet in all_bets]
    fresh = [(key, bet) for key, bet in fresh if key not in seen]
    fresh.sort(key=lambda kb: kb[1]["ev_pct"], reverse=True)
    top = fresh[:SPORTS_TOP_N]

    if not top:
        log.info(f"📡 {datetime.now().strftime('%H:%M:%S')} | Шинэ value bet олдсонгүй (нийт {len(all_bets)} эдж)")
        sports_state.save_seen(seen)
        return

    lines = [f"VALUE BET SCAN — топ {len(top)} (нийт {len(all_bets)} эдж олдсон)\n"]
    for i, (key, bet) in enumerate(top, 1):
        kelly_pct, stake = kelly.stake_amount(
            SPORTS_BANKROLL_USD, bet["pinnacle_true_prob"], bet["odds"],
            kelly_multiplier=SPORTS_KELLY_MULTIPLIER, max_pct=SPORTS_MAX_STAKE_PCT,
        )
        lines.append(_format_bet_line(i, bet, kelly_pct, stake))
        sports_journal.record_alert(bet, kelly_pct, stake)
        seen[key] = now

    notifications.send_telegram("\n\n".join(lines), bot_token=SPORTS_BOT_TOKEN, chat_id=SPORTS_CHAT_ID)
    sports_state.save_seen(seen)
    log.info(f"📡 {datetime.now().strftime('%H:%M:%S')} | {len(top)} value bet Telegram-руу илгээв (нийт {len(all_bets)} эдж)")


def main():
    setup_logging(STATE_DIR, STATE_DIR_IS_PERSISTENT)
    log.info("=" * 70)
    log.info("🏀🏈⚾⚽ SPORTS VALUE BET SCANNER")
    log.info(f"Sports: {', '.join(SPORTS_LIST)}")
    log.info(f"Min edge: {SPORTS_MIN_EDGE_PCT}% | Scan interval: {SPORTS_SCAN_INTERVAL_MINUTES} min")
    log.info("⚠️ Бооцоог автоматаар ТАВИХГҮЙ — зөвхөн санал болгоно, чи гараар тавина")
    log.info("=" * 70)

    try:
        validate_sports_config()
    except Exception as e:
        log.error(f"❌ CONFIG ERROR: {e}")
        return

    while True:
        try:
            run_scan_cycle()
        except Exception:
            error = traceback.format_exc()
            log.error(f"❌ SCAN ERROR\n{error}")
            try:
                notifications.send_telegram(
                    f"❌ Sports scanner алдаа:\n{error[:500]}",
                    bot_token=SPORTS_BOT_TOKEN, chat_id=SPORTS_CHAT_ID,
                )
            except Exception:
                pass
        time.sleep(SPORTS_SCAN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
